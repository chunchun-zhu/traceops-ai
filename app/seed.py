import json

from .ingest import ingest
from .schemas import DocumentInput
from .store import Store, now


def seed(store: Store):
    nodes = [
        {"id": "COMP-01", "name": "一号空压机总管", "kind": "source", "model": "AIR-SYSTEM"},
        {"id": "BR-A", "name": "A 区供气支路", "kind": "branch", "model": "AIR-SYSTEM"},
        {"id": "BR-B", "name": "B 区供气支路", "kind": "branch", "model": "AIR-SYSTEM"},
        {"id": "PRESS-01", "name": "一号冲压机", "kind": "device", "model": "P-200"},
        {"id": "ASSY-01", "name": "一号气动装配台", "kind": "device", "model": "A-100"},
        {"id": "CNC-01", "name": "一号数控机床", "kind": "device", "model": "C-300"},
    ]
    with store.connect() as db:
        for node in nodes:
            db.execute("INSERT OR IGNORE INTO nodes VALUES(?,?)", (node["id"], json.dumps({**node, "source": "synthetic_demo"}, ensure_ascii=False)))
        for source, target in [("COMP-01", "BR-A"), ("COMP-01", "BR-B"), ("BR-A", "PRESS-01"), ("BR-A", "ASSY-01"), ("BR-B", "CNC-01")]:
            db.execute("INSERT OR IGNORE INTO edges VALUES(?,?)", (source, target))
    if not store.measurements(["COMP-01"]):
        for nid, value in [("COMP-01", 0.72), ("BR-A", 0.35), ("BR-B", 0.68), ("PRESS-01", 0.34), ("ASSY-01", 0.33)]:
            store.add_measurement({"node_id": nid, "value": value, "unit": "MPa", "observed_at": now(), "source": "synthetic_demo", "is_demo": True})
    for model, code, title in [("P-200", "E101", "冲压机"), ("A-100", "E201", "装配台")]:
        ingest(store, DocumentInput(title=f"{title}供气检查说明（模拟）", model=model, text=f"# {code} 供气压力不足\n\n本资料为虚构演示，不是实际设备操作手册。\n\n故障代码：{code}\n故障现象：气压不足，气动执行机构动作超时。\n可能原因：供气总管压力异常、支路压力异常、设备入口压力异常。\n\n# 观察检查步骤\n\n先核对总管压力，再核对支路压力，最后核对设备入口压力。演示设定正常范围为 0.60–0.80 MPa；实际判定须以真实设备手册为准。仅读取现有压力表，不执行阀门操作。"))
    ingest(store, DocumentInput(title="公共供气关联排障说明（模拟）", model="AIR-SYSTEM", text="# 多设备气压不足\n\n当同一支路的多台设备同时气压不足，沿设备供气路径查询共同上游，作为候选范围，不直接认定根因。\n\n# 证据与确认\n\n记录总管、支路和设备入口读数及其时间。历史告警和模拟测点只能提供线索，当前压力以现场核验为准。检查前核对设备资料型号和版本。"))
    ingest(store, DocumentInput(title="A 区低压维修工单（模拟）", model="AIR-SYSTEM", kind="work_order", preliminary_cause="疑似空压机压力不足", confirmed_cause="A 区支路压力表故障", text="# 历史工单 DEMO-WO-001\n\n故障现象：冲压机和装配台同时提示低压。\n初步判断：疑似空压机压力不足。\n最终确认原因：A 区支路压力表故障，授权维修人员独立仪表核验后确认。\n处理结果：维修人员处理后复测正常。该记录为模拟案例，不可据此认定本次故障。"))


def seed_standards(operations):
    """Explicit simulated pressure standards, never inferred from a real manual."""
    from .schemas import StandardInput
    existing = {s["reference_document_id"] for s in operations.standards() if s["is_demo"]}
    quote = "演示设定正常范围为 0.60–0.80 MPa；实际判定须以真实设备手册为准。"
    for doc in operations.store.documents():
        if not doc["title"].endswith("（模拟）") or doc["id"] in existing or doc["kind"] != "manual" or doc["model"] not in {"P-200","A-100"}:
            continue
        if quote in doc["pages"][0]:
            operations.add_standard(StandardInput(title="入口压力范围（模拟）",model=doc["model"],minimum=0.6,maximum=0.8,
                reference_document_id=doc["id"],page=1,quote=quote,approved_by="模拟数据初始化"),is_demo=True)
