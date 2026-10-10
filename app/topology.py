import json
from collections import deque

from .store import Store


class Topology:
    def __init__(self, store: Store):
        self.store = store

    def ancestors(self, node_id):
        parents = {}
        for e in self.store.edges():
            parents.setdefault(e["target"], []).append(e["source"])
        paths = {node_id: [node_id]}
        queue = deque([node_id])
        while queue:
            child = queue.popleft()
            for parent in parents.get(child, []):
                if parent not in paths:
                    paths[parent] = [parent] + paths[child]
                    queue.append(parent)
        return paths

    def common(self, device_ids):
        nodes = {n["id"]: n for n in self.store.nodes()}
        if not device_ids or any(d not in nodes or nodes[d]["kind"] != "device" for d in device_ids):
            raise ValueError("请选择台账中存在的用气设备")
        ancestry = [self.ancestors(d) for d in device_ids]
        common_ids = set.intersection(*(set(a) for a in ancestry)) - set(device_ids)
        ordered = sorted(common_ids, key=lambda nid: (sum(len(a[nid]) for a in ancestry), nid))
        return [{"node": nodes[nid], "paths": [a[nid] for a in ancestry], "affected_devices": self.downstream(nid)} for nid in ordered]

    def downstream(self, node_id):
        nodes = {n["id"]: n for n in self.store.nodes()}
        children = {}
        for e in self.store.edges():
            children.setdefault(e["source"], []).append(e["target"])
        seen, queue = set(), deque([node_id])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(children.get(node, []))
        return [nodes[nid] for nid in sorted(seen) if nodes[nid]["kind"] == "device"]

    def import_graph(self, request):
        """Append/upsert registry nodes and edges atomically after DAG validation."""
        if len({n.id for n in request.nodes}) != len(request.nodes):
            raise ValueError("导入节点编号重复")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            nodes = {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,payload FROM nodes")}
            nodes.update({n.id: n.model_dump() for n in request.nodes})
            edges = {(r[0], r[1]) for r in db.execute("SELECT source,target FROM edges")}
            edges.update((e.source, e.target) for e in request.edges)
            children, degree = {}, dict.fromkeys(nodes, 0)
            for source, target in edges:
                if source not in nodes or target not in nodes:
                    raise ValueError("供气边引用了不存在的节点")
                if nodes[source]["kind"] == "device" or nodes[target]["kind"] == "source":
                    raise ValueError("供气方向须从供气源/支路指向支路/设备")
                children.setdefault(source, []).append(target)
                degree[target] += 1
            queue = deque(n for n, d in degree.items() if d == 0)
            visited = 0
            while queue:
                current = queue.popleft()
                visited += 1
                for target in children.get(current, []):
                    degree[target] -= 1
                    if degree[target] == 0:
                        queue.append(target)
            if visited != len(nodes):
                raise ValueError("供气关系存在环，未导入")
            for n in request.nodes:
                db.execute("INSERT INTO nodes VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (n.id, json.dumps(n.model_dump(), ensure_ascii=False)))
            for edge in request.edges:
                db.execute("INSERT OR IGNORE INTO edges VALUES(?,?)", (edge.source, edge.target))
        return {"nodes_upserted": len(request.nodes), "edges_submitted": len(request.edges)}
