import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .config import ROOT
from .models import ModelGateway, ModelUnavailable
from .retrieval import Retriever
from .schemas import ClarifyInput, ConfirmInput, FeedbackInput, StartInput
from .store import ConflictError, Store, now
from .topology import Topology


class GraphState(TypedDict, total=False):
    id: str
    revision: int
    created_at: str
    updated_at: str
    device_ids: list[str]
    symptom: str
    status: str
    question: str | None
    candidates: list[dict]
    evidence: list[dict]
    topology: list[dict]
    observations: list[dict]
    checks: list[dict]
    current_check: dict | None
    summary: str
    warnings: list[str]
    trace: list[dict]
    skill: dict
    retrieval_route: str
    model_explanation: dict | None
    confirmation: dict | None
    mode: str


class Workflow:
    def __init__(self, store: Store, retriever: Retriever, gateway: ModelGateway):
        self.store, self.retriever, self.gateway = store, retriever, gateway
        self.topology = Topology(store)
        self.skill = json.loads((ROOT / "skills" / "air-supply.json").read_text(encoding="utf-8"))
        builder = StateGraph(GraphState)
        builder.add_node("identify", self.identify)
        builder.add_node("collect", self.collect)
        builder.add_node("plan", self.plan)
        builder.add_edge(START, "identify")
        builder.add_conditional_edges("identify", lambda s: "stop" if s["status"] in {"needs_info", "handoff"} else "collect", {"stop": END, "collect": "collect"})
        builder.add_edge("collect", "plan")
        builder.add_edge("plan", END)
        self.graph = builder.compile()

    def check_for(self, state, index):
        check = dict(self.skill["checks"][index])
        if index == 0:
            check["target_ids"] = [c["node"]["id"] for c in state["topology"] if c["node"]["kind"] == "source"]
        elif index == 1:
            nearest = state["topology"][0]
            if nearest["node"]["kind"] == "branch":
                check["target_ids"] = [nearest["node"]["id"]]
            else:
                check["target_ids"] = list(dict.fromkeys(n for p in nearest["paths"] for n in p[1:-1]))
                check["title"] = "核对各设备供气支路压力"
                check["instruction"] = "这些设备跨越不同支路。在总管已确认正常后，分别记录各支路现有压力表读数并对照适用手册；无共同支路时不能将异常归因于同一支路。"
                check["abnormal_candidate"] = "至少一条设备供气支路异常，需逐支路核查"
        else:
            check["target_ids"] = state["device_ids"]
        return check

    def identify(self, state):
        if not state["device_ids"]:
            return {"status": "needs_info", "question": "请先选择发生告警的设备，再继续获取依据。", "summary": "设备信息不足"}
        self.topology.common(state["device_ids"])
        if not any(word in state["symptom"].lower() for word in ("气", "压力", "供气", "e101", "e201", "air", "pressure")):
            return {"status": "handoff", "question": None, "summary": "当前技能仅覆盖供气压力异常，其他故障请人工接管。"}
        return {"status": "collecting", "question": None}

    def collect(self, state):
        nodes = {n["id"]: n for n in self.store.nodes()}
        common = self.topology.common(state["device_ids"])
        model_names = list({nodes[d]["model"] for d in state["device_ids"]} | {"AIR-SYSTEM"})
        result = self.retriever.search(state["symptom"], models=model_names, limit=8)
        ids = list(dict.fromkeys(state["device_ids"] + [c["node"]["id"] for c in common]))
        observations = self.store.measurements(ids)
        warnings = list(result["warnings"])
        if not common:
            warnings.append("设备间未找到共同供气节点，需核验连接关系")
        if not observations:
            warnings.append("无测点数据，不能据此判断压力状态")
        elif any(o.get("is_demo") for o in observations):
            warnings.append("测点为模拟快照，不能代替现场读数")
        warnings.append("测点是已存储快照，需核对时间；知识依据保留文档版本，请核对现场适用版本")
        trace = list(state.get("trace", [])) + [
            {"node": "identify", "at": now(), "detail": "设备已在台账核对"},
            {"node": "topology", "at": now(), "detail": f"共同上游 {len(common)} 个"},
            {"node": "observations", "at": now(), "detail": f"测点快照 {len(observations)} 条"},
            {"node": "retrieval", "at": now(), "detail": f"{result['route']} / 依据 {len(result['items'])} 条"}]
        return {"topology": common, "observations": observations, "evidence": result["items"], "warnings": warnings,
                "retrieval_route": result["route"], "trace": trace}

    def plan(self, state):
        if not state["evidence"] or not state["topology"] or not any(c["node"]["kind"] == "source" for c in state["topology"]):
            return {"status": "handoff", "summary": "适用资料或供气关系不足，已转人工核验。", "current_check": None}
        candidate = state["topology"][0]["node"]
        update = {"status": "checking", "current_check": self.check_for(state, 0),
                  "summary": f"共同上游候选为 {candidate['name']}，连接关系不能单独证明根因。请从总管压力开始核验。",
                  "candidates": [{"name": self.check_for(state, i)["abnormal_candidate"], "state": "pending", "basis": "技能检查分支，尚未核验"} for i in range(len(self.skill["checks"]))],
                  "trace": state["trace"] + [{"node": "plan", "at": now(), "detail": f"加载技能 {self.skill['id']}@{self.skill['version']}"}]}
        if self.gateway.settings.llm_base_url:
            try:
                explanation = self.gateway.explain({"symptom": state["symptom"], "topology": state["topology"], "observations": state["observations"], "evidence": state["evidence"]})
                update.update(model_explanation=explanation, mode="model_assisted")
            except ModelUnavailable as exc:
                update.update(warnings=state["warnings"] + [str(exc) + "，保留规则检查流程"], mode="rule_demo")
        return update

    def start(self, request: StartInput):
        state = self.graph.invoke({"device_ids": request.device_ids, "symptom": request.symptom,
            "status": "new", "question": None, "candidates": [], "evidence": [], "topology": [],
            "observations": [], "checks": [], "current_check": None, "summary": "", "warnings": [], "trace": [],
            "skill": {"id": self.skill["id"], "version": self.skill["version"]}, "retrieval_route": "none",
            "model_explanation": None, "confirmation": None, "mode": "rule_demo"})
        return self.store.new_session(state)

    def load_current(self, session_id, expected_revision):
        state = self.store.session(session_id)
        if state["revision"] != expected_revision:
            raise ConflictError("会话版本已变化，请重新载入")
        return state

    def clarify(self, session_id, request: ClarifyInput):
        state = self.load_current(session_id, request.expected_revision)
        if state["status"] != "needs_info":
            raise ValueError("当前会话不等待设备信息")
        state["device_ids"] = list(dict.fromkeys(request.device_ids))
        state = self.graph.invoke(state)
        return self.store.save_session(state, request.expected_revision, {"type": "clarified", "input": request.model_dump()})

    def feedback(self, session_id, request: FeedbackInput):
        state = self.load_current(session_id, request.expected_revision)
        if state["status"] != "checking" or not state["current_check"]:
            raise ValueError("当前会话不等待检查反馈")
        current = state["current_check"]
        if request.check_id != current["id"]:
            raise ValueError("只能提交当前检查步骤；已完成检查不能重复提交")
        state["checks"].append({**current, **request.model_dump(exclude={"expected_revision"}), "at": now()})
        state["trace"].append({"node": "feedback", "at": now(), "detail": f"{current['id']}={request.result}"})
        index = next(i for i, c in enumerate(self.skill["checks"]) if c["id"] == request.check_id)
        state["candidates"][index]["state"] = {"normal": "not_supported", "abnormal": "supported", "unknown": "unverified"}[request.result]
        state["candidates"][index]["basis"] = request.note or "人工提交的检查结果"
        if request.result == "unknown":
            state.update(status="handoff", current_check=None, summary="现场检查无法核验，保留已有证据并转人工接管。")
        elif request.result == "abnormal":
            state.update(status="awaiting_confirmation", current_check=None, summary=f"检查支持候选：{current['abnormal_candidate']}。仍需人工核验，不自动认定根因。")
        elif index + 1 < len(self.skill["checks"]):
            next_check = self.check_for(state, index + 1)
            if not next_check["target_ids"]:
                state.update(status="handoff", current_check=None, summary="台账缺少下一步所需支路节点，请人工核验连接关系。")
            else:
                state.update(current_check=next_check, summary="本步骤反馈正常，继续下一项检查；已记录步骤不再重复。")
        else:
            state.update(status="handoff", current_check=None, summary="预设压力检查均正常，尚不能解释故障，请人工扩展排查范围。")
        return self.store.save_session(state, request.expected_revision, {"type": "feedback", "input": request.model_dump()})

    def confirm(self, session_id, request: ConfirmInput):
        state = self.load_current(session_id, request.expected_revision)
        if state["status"] not in {"checking", "awaiting_confirmation", "handoff", "needs_info"}:
            raise ValueError("会话已经结束")
        if request.decision == "confirmed" and state["status"] != "awaiting_confirmation":
            raise ValueError("仅待人工确认的候选可以确认；其他状态可转人工接管")
        state.update(status="resolved" if request.decision == "confirmed" else "handed_off", current_check=None,
                     confirmation={**request.model_dump(exclude={"expected_revision"}), "at": now()},
                     summary="人工已记录确认结论。" if request.decision == "confirmed" else "已记录人工接管与交接说明。")
        state["trace"].append({"node": "human_confirmation", "at": now(), "detail": request.decision})
        return self.store.save_session(state, request.expected_revision, {"type": "confirmation", "input": request.model_dump()})
