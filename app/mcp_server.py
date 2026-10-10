"""Read-only stdio MCP server sharing the same services and database as FastAPI."""
from mcp.server.fastmcp import FastMCP

from .config import Settings
from .models import ModelGateway
from .retrieval import Retriever
from .store import Store
from .topology import Topology

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


if __name__ == "__main__":
    server.run(transport="stdio")
