import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from filelock import FileLock, Timeout

from .config import ROOT
from .models import ModelGateway, ModelUnavailable
from .retrieval import Retriever
from .schemas import ClarifyInput, ConfirmInput, FeedbackInput, StartInput, RepairInput, VerifyInput
from .store import ConflictError, Store, now
from .topology import Topology
from .operations import Operations


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
    workflow_version: int
    repairs: list[dict]
    verifications: list[dict]
    alarm_events: list[dict]
    device_models: dict[str, str]
    created_by: dict | None
    _action: dict | None
    _event: dict


class Workflow:
    def __init__(self, store: Store, retriever: Retriever, gateway: ModelGateway):
        self.store, self.retriever, self.gateway = store, retriever, gateway
        self.topology = Topology(store)
        self.operations = Operations(store)
        self.skill = json.loads((ROOT / "skills" / "air-supply.json").read_text(encoding="utf-8"))
        self.lock = FileLock(str(store.path) + ".workflow.lock", timeout=30)
        self.checkpoint_connection = sqlite3.connect(str(store.path) + ".checkpoints", check_same_thread=False, timeout=10)
        self.saver = SqliteSaver(self.checkpoint_connection)
        self.saver.setup()
        builder = StateGraph(GraphState)
        builder.add_node("entry", lambda state: {})
        builder.add_node("identify", self.identify)
        builder.add_node("collect", self.collect)
        builder.add_node("plan", self.plan)
        builder.add_node("await_input", self.await_input)
        for kind in ("clarify", "feedback", "confirm", "repair", "verify"):
            builder.add_node(kind, self.action_node(kind))
        builder.add_edge(START, "entry")
        builder.add_conditional_edges("entry", lambda s: "identify" if s["status"] == "new" else self.next_node(s), {"identify": "identify", "wait": "await_input", "end": END})
        builder.add_conditional_edges("identify", lambda s: "wait" if s["status"] in {"needs_info", "handoff"} else "collect", {"wait": "await_input", "collect": "collect"})
        builder.add_edge("collect", "plan")
        builder.add_edge("plan", "await_input")
        builder.add_conditional_edges("await_input", lambda s: s["_action"]["kind"], {k: k for k in ("clarify", "feedback", "confirm", "repair", "verify")})
        builder.add_edge("clarify", "identify")
        for kind in ("feedback", "confirm", "repair", "verify"):
            builder.add_conditional_edges(kind, self.next_node, {"wait": "await_input", "end": END})
        self.graph = builder.compile(checkpointer=self.saver)

    def close(self):
        self.checkpoint_connection.close()

    @staticmethod
    def next_node(state):
        return "end" if state["status"] in {"resolved", "handed_off"} else "wait"

    def await_input(self, state):
        action = interrupt({"phase": state["status"], "check": state.get("current_check"), "revision": state["revision"]})
        return {"_action": action}

    def action_node(self, kind):
        def apply(state):
            state = deepcopy(state)
            payload = state["_action"]["input"]
            actor = state["_action"].get("actor")
            getattr(self, "apply_" + kind)(state, payload)
            state.update(revision=state["revision"] + 1, updated_at=now(), _action=None,
                         _event={"type": {"clarify": "clarified", "confirm": "confirmation"}.get(kind, kind), "input": payload, "actor": actor, "at": now()})
            return state
        return apply

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
                "retrieval_route": result["route"], "trace": trace,
                "device_models": {d: nodes[d]["model"] for d in state["device_ids"]}}

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

    def start(self, request: StartInput, actor=None):
        with self.lock:
            selected = self.operations.selected_alarms(request.alarm_ids) if request.alarm_ids else []
            if selected:
                alarm_devices = {a["device_id"] for a in selected}
                if not request.device_ids:
                    request = request.model_copy(update={"device_ids":sorted(alarm_devices)})
                elif not alarm_devices.issubset(request.device_ids):
                    raise ValueError("所选告警设备必须包含在排障设备中")
            if request.device_ids:
                self.topology.common(request.device_ids)
            state = self.store.new_session({"device_ids": request.device_ids, "symptom": request.symptom,
            "status": "new", "question": None, "candidates": [], "evidence": [], "topology": [],
            "observations": [], "checks": [], "current_check": None, "summary": "", "warnings": [], "trace": [],
            "skill": {"id": self.skill["id"], "version": self.skill["version"]}, "retrieval_route": "none",
                "model_explanation": None, "confirmation": None, "mode": "rule_demo", "workflow_version": 3,
                "repairs": [], "verifications": [], "device_models": {}, "alarm_events": selected, "created_by": actor})
            self.graph.invoke(state, config=self.config(state["id"]))
            return self.sync(state["id"])

    @staticmethod
    def config(session_id):
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 100}

    def sync(self, session_id):
        state = self.graph.get_state(self.config(session_id)).values
        return self.store.project_session(state)

    def get_unlocked(self, session_id):
        state = self.store.session(session_id)
        config = self.config(session_id)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            # Older API snapshots are seeded at entry without replaying completed checks.
            state.update(workflow_version=3, repairs=state.get("repairs", []), verifications=state.get("verifications", []),
                         device_models=state.get("device_models", {n["id"]: n["model"] for n in self.store.nodes() if n["id"] in state["device_ids"]}))
            if state["status"] == "resolved" and not state["verifications"]:
                state.update(status="repairing", summary="旧版人工确认已保留；尚无维修后复测记录，需补充维修与核验后才能关闭。")
            event = self.store.events(session_id)[-1]
            state["_event"] = {k: v for k, v in event.items() if k != "state"}
            self.graph.invoke(state, config=config)
        else:
            # Pending non-interrupt nodes resume from their durable checkpoint after a crash.
            if snapshot.next and (any(t.error for t in snapshot.tasks) or not any(t.interrupts for t in snapshot.tasks)):
                self.graph.invoke(None, config=config)
        return self.sync(session_id)

    def session(self, session_id):
        with self.lock:
            return self.get_unlocked(session_id)

    def sessions(self):
        with self.lock:
            return [self.get_unlocked(s["id"]) for s in self.store.sessions()]

    def load_current(self, session_id, expected_revision):
        state = self.get_unlocked(session_id)
        if state["revision"] != expected_revision:
            raise ConflictError("会话版本已变化，请重新载入")
        return state

    def clarify(self, session_id, request: ClarifyInput, actor=None):
        return self.resume(session_id, "clarify", request, actor)

    def feedback(self, session_id, request: FeedbackInput, actor=None):
        return self.resume(session_id, "feedback", request, actor)

    def apply_clarify(self, state, payload):
        state["device_ids"] = list(dict.fromkeys(payload["device_ids"]))
        state["status"] = "new"

    def apply_feedback(self, state, payload):
        request = FeedbackInput.model_validate(payload)
        current = state["current_check"]
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

    def confirm(self, session_id, request: ConfirmInput, actor=None):
        return self.resume(session_id, "confirm", request, actor)

    def apply_confirm(self, state, payload):
        request = ConfirmInput.model_validate(payload)
        state.update(status="repairing" if request.decision == "confirmed" else "handed_off", current_check=None,
                     confirmation={**request.model_dump(exclude={"expected_revision"}), "at": now()},
                     summary="原因确认已记录；请补充授权人员完成的维修记录，随后进行复测。" if request.decision == "confirmed" else "已记录人工接管与交接说明。")
        state["trace"].append({"node": "human_confirmation", "at": now(), "detail": request.decision})

    def repair(self, session_id, request: RepairInput, actor=None):
        return self.resume(session_id, "repair", request, actor)

    def apply_repair(self, state, payload):
        state["repairs"].append({**payload, "at": now()})
        state.update(status="awaiting_verification", summary="维修记录已保存；请复测所有告警设备并核验告警是否消除。")
        state["trace"].append({"node": "repair", "at": now(), "detail": "人工维修完成，等待复测"})

    def verify(self, session_id, request: VerifyInput, actor=None):
        return self.resume(session_id, "verify", request, actor)

    def apply_verify(self, state, payload):
        if payload["result"] == "passed":
            payload["alarm_resolution"] = self.operations.cleared_for(state.get("alarm_events",[]),state["repairs"][-1]["completed_at"])
        for reading in payload["readings"]:
            if reading.get("standard_id"):
                reading["standard_snapshot"] = self.operations.standard(reading["standard_id"])
        state["verifications"].append({**payload, "repair_number": len(state["repairs"]), "at": now()})
        status = {"passed": "resolved", "failed": "repairing", "unknown": "handoff"}[payload["result"]]
        summary = {"passed": "全部告警设备复测在人工提供的适用范围内，告警消除已由人工核验，故障闭环完成。",
                   "failed": "维修后复测未通过，故障尚未关闭；请重新核查并记录后续维修或转人工接管。",
                   "unknown": "维修后结果无法核验，已转人工接管，故障未关闭。"}[payload["result"]]
        state.update(status=status, summary=summary)
        state["trace"].append({"node": "verify", "at": now(), "detail": payload["result"]})

    def validate_action(self, state, kind, request):
        status = state["status"]
        if kind == "clarify":
            if status != "needs_info":
                raise ValueError("当前会话不等待设备信息")
            self.topology.common(request.device_ids)
        elif kind == "feedback":
            if status != "checking" or not state["current_check"]:
                raise ValueError("当前会话不等待检查反馈")
            if request.check_id != state["current_check"]["id"]:
                raise ValueError("只能提交当前检查步骤；已完成检查不能重复提交")
        elif kind == "confirm":
            if status in {"resolved", "handed_off"}:
                raise ValueError("会话已经结束")
            if request.decision == "confirmed" and status != "awaiting_confirmation":
                raise ValueError("仅待人工确认的候选可以确认；其他状态可转人工接管")
        elif kind == "repair":
            if status != "repairing":
                raise ValueError("当前会话不等待维修记录")
            stamp = datetime.fromisoformat(request.completed_at)
            if stamp < datetime.fromisoformat(state["confirmation"]["at"]) or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("维修完成时间须在原因确认之后，且不能晚于当前时间超过五分钟")
        elif kind == "verify":
            if status != "awaiting_verification":
                raise ValueError("须先记录维修完成再提交复测")
            if request.result == "passed" and (not request.alarms_cleared or {r.device_id for r in request.readings} != set(state["device_ids"]) or len(request.readings) != len(state["device_ids"])):
                raise ValueError("通过复测须覆盖全部告警设备且确认告警已消除")
            if request.result == "passed":
                self.operations.cleared_for(state.get("alarm_events",[]),state["repairs"][-1]["completed_at"])
            if len({r.device_id for r in request.readings}) != len(request.readings):
                raise ValueError("复测设备不能重复")
            for reading in request.readings:
                if request.result == "passed" or reading.standard_id:
                    self.operations.validate_reading(reading,state["device_models"][reading.device_id] if reading.device_id in state["device_models"] else "")
                if reading.device_id not in state["device_ids"]:
                    raise ValueError("复测设备不属于当前会话")
                stamp = datetime.fromisoformat(reading.observed_at)
                if stamp < datetime.fromisoformat(state["repairs"][-1]["completed_at"]) or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                    raise ValueError("复测时间须在本次维修完成之后，且不能使用未来读数")
                document = self.store.document(reading.reference_document_id)
                if not document["active"] or document["model"] not in {state["device_models"][reading.device_id], "AIR-SYSTEM"}:
                    raise ValueError("复测依据必须为有效且型号适用的资料")
                if request.result == "passed" and not reading.minimum <= reading.value <= reading.maximum:
                    raise ValueError("读数超出人工提供的适用范围，不能提交通过")

    def resume(self, session_id, kind, request, actor=None):
        try:
            with self.lock:
                state = self.load_current(session_id, request.expected_revision)
                self.validate_action(state, kind, request)
                self.graph.invoke(Command(resume={"kind": kind, "input": request.model_dump(), "actor": actor}), config=self.config(session_id))
                return self.sync(session_id)
        except Timeout as exc:
            raise ConflictError("工作流正在处理其他请求，请稍后载入重试") from exc
