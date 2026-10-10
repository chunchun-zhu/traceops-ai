"""Append-only alarm events and immutable, source-backed pressure standards."""
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from .schemas import AlarmInput, StandardInput, utc_timestamp
from .store import ConflictError, now


class Operations:
    def __init__(self, store):
        self.store = store

    def add_alarm(self, request: AlarmInput):
        item = request.model_dump()
        if datetime.fromisoformat(item["occurred_at"]) > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("告警事件不能晚于当前时间超过五分钟")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload FROM alarms WHERE source=? AND event_id=?", (item["source"],item["event_id"])).fetchone()
            if previous:
                old = json.loads(previous[0])
                if any(old[k] != v for k,v in item.items()):
                    raise ConflictError("同一来源的事件编号已存在且内容不同")
                return {**old, "duplicate": True}
            nodes = {n["id"]:n for n in self.store.nodes()}
            if item["device_id"] not in nodes or nodes[item["device_id"]]["kind"] != "device":
                raise ValueError("告警设备须为台账内用气设备")
            item.update(id=uuid4().hex, received_at=now())
            db.execute("INSERT INTO alarms VALUES(?,?,?,?,?,?,?)", (item["id"],item["source"],item["event_id"],item["device_id"],item["code"],item["occurred_at"],json.dumps(item,ensure_ascii=False)))
        return {**item,"duplicate":False}

    def alarms(self, device_id=None, since=None, until=None, active=False, limit=200):
        since = utc_timestamp(since) if since else None
        until = utc_timestamp(until) if until else now()
        if since and since > until:
            raise ValueError("时间窗起点不能晚于终点")
        with self.store.connect() as db:
            sql = "SELECT payload FROM alarms WHERE occurred_at<=?"
            args = [until]
            if device_id:
                sql += " AND device_id=?"; args.append(device_id)
            rows = [json.loads(r[0]) for r in db.execute(sql+" ORDER BY occurred_at DESC,rowid DESC",args)]
        if active:
            latest = {}
            for row in rows:
                latest.setdefault((row["source"],row["device_id"],row["code"]),row)
            rows = [r for r in latest.values() if r["status"] == "raised"]
        return [r for r in rows if not since or r["occurred_at"] >= since][:limit]

    def selected_alarms(self, ids):
        available = {r["id"]:r for r in self.alarms(active=True,limit=1000000)}
        if len(set(ids)) != len(ids) or any(i not in available for i in ids):
            raise ValueError("所选告警已消除、被新事件替代或不存在，请刷新告警")
        return [available[i] for i in ids]

    def standards(self):
        with self.store.connect() as db:
            return [{**json.loads(r[1]),"active":bool(r[0])} for r in db.execute("SELECT active,payload FROM standards ORDER BY rowid DESC")]

    def standard(self, standard_id):
        for item in self.standards():
            if item["id"] == standard_id:
                return item
        raise KeyError(standard_id)

    def add_standard(self, request: StandardInput, is_demo=False):
        doc = self.store.document(request.reference_document_id)
        if not doc["active"] or doc["kind"] != "manual" or doc["model"] not in {request.model,"AIR-SYSTEM"}:
            raise ValueError("标准须引用有效且型号适用的手册")
        if request.page > len(doc["pages"]):
            raise ValueError("引用页码不存在")
        text = doc["pages"][request.page-1]
        start = request.source_start
        if start is None:
            if text.count(request.quote) != 1:
                raise ValueError("原文须唯一匹配；重复条款请填写 source_start")
            start = text.index(request.quote)
        if text[start:start+len(request.quote)] != request.quote:
            raise ValueError("引用原文与资料页内容不一致")
        item = {**request.model_dump(),"id":uuid4().hex,"unit":"MPa","source_start":start,
                "source_end":start+len(request.quote),"document_title":doc["title"],"document_version":doc["version"],
                "approved_at":now(),"is_demo":is_demo}
        with self.store.connect() as db:
            db.execute("INSERT INTO standards(id,payload) VALUES(?,?)",(item["id"],json.dumps(item,ensure_ascii=False)))
        return {**item,"active":True}

    def standard_status(self, standard_id, active):
        with self.store.connect() as db:
            if db.execute("UPDATE standards SET active=? WHERE id=?",(int(active),standard_id)).rowcount != 1:
                raise KeyError(standard_id)
        return self.standard(standard_id)

    def validate_reading(self, reading, model):
        if not reading.standard_id:
            raise ValueError("通过复测须选择已审核的结构化标准")
        standard = self.standard(reading.standard_id)
        doc = self.store.document(standard["reference_document_id"])
        if not standard["active"] or not doc["active"] or standard["model"] not in {model,"AIR-SYSTEM"}:
            raise ValueError("复测标准已停用或型号不适用")
        if (reading.minimum,reading.maximum,reading.reference_document_id) != (standard["minimum"],standard["maximum"],standard["reference_document_id"]):
            raise ValueError("复测范围和依据必须与所选标准一致")
        return standard


    def cleared_for(self, snapshots, repaired_at):
        latest = {}
        for event in self.alarms(limit=1000000):
            latest.setdefault((event["source"],event["device_id"],event["code"]),event)
        result = []
        for alarm in snapshots:
            event = latest.get((alarm["source"],alarm["device_id"],alarm["code"]))
            if not event or event["status"] != "cleared" or event["occurred_at"] < repaired_at:
                raise ValueError("关联告警须有本次维修完成后的消除事件，才能通过复测")
            result.append(event)
        return result
