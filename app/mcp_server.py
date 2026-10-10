"""Read-only stdio MCP server sharing the same services and database as FastAPI."""
from mcp.server.fastmcp import FastMCP

from .config import Settings
from .models import ModelGateway
from .retrieval import Retriever
from .store import Store
from .topology import Topology
from .operations import Operations

settings = Settings()
store = Store(settings.resolved_db_path)
retriever = Retriever(store, ModelGateway(settings))
server = FastMCP("suzhang")


@server.tool()
def get_device_registry() -> dict:
    """读取设备台账；返回数据来源。"""
    return {"items": store.nodes(), "source": "device_registry"}


@server.tool()
def find_common_supply(device_ids: list[str]) -> dict:
    """查询共同上游、连接路径及潜在影响设备；不认定故障根因。"""
    try:
        return {"items": Topology(store).common(device_ids), "source": "verified_topology_or_demo"}
    except ValueError as exc:
        return {"error": str(exc), "items": []}


@server.tool()
def get_pressure_snapshots(node_ids: list[str]) -> dict:
    """返回最新已存储压力快照及其时间、来源，不保证当前现场状态。"""
    return {"items": store.measurements(node_ids), "source": "stored_measurements"}


@server.tool()
def search_maintenance_evidence(query: str, model: str | None = None, version: str | None = None) -> dict:
    """检索设备手册与工单，返回型号、版本、页码、原文范围与工单原因字段。"""
    if not query.strip() or len(query) > 1000:
        return {"error": "query 须为 1–1000 字符", "items": []}
    return retriever.search(query, [model] if model else None, version)


@server.tool()
def get_alarm_events(device_id: str | None = None, since: str | None = None, until: str | None = None, active: bool = False) -> dict:
    """读取告警事件时间窗或未消除告警；保留来源与事件时间，最多 200 条。"""
    try:
        return {"items":Operations(store).alarms(device_id,since,until,active),"source":"stored_alarm_events"}
    except ValueError as exc:
        return {"error":str(exc),"items":[]}


@server.tool()
def get_pressure_standards(model: str | None = None) -> dict:
    """读取有效、原文可追溯的复测标准；数值范围由人工核验。"""
    operations=Operations(store)
    return {"items":[s for s in operations.standards() if s["active"] and operations.store.document(s["reference_document_id"])["active"] and (not model or s["model"] in {model,"AIR-SYSTEM"})],"source":"approved_pressure_standards"}


if __name__ == "__main__":
    server.run(transport="stdio")
