# 告警接口联调

先导入设备台账，厂商适配层将设备编号映射为设备 id，状态映射为 raised/cleared，时间转换为带时区 ISO。source 为稳定来源，event_id 为该来源的唯一事件编号。消除是新事件，使用不同 event_id、相同 source/device_id/code，不能覆盖发生事件。

```json
{"event_id":"plc-1001","device_id":"PRESS-01","code":"E101","status":"raised","occurred_at":"2026-10-10T16:00:00+08:00","source":"iot-adapter-A","message":"供气压力不足"}
```

POST /api/alarms；配置 SUZHANG_API_TOKEN 时添加 Authorization: Bearer。相同内容重发幂等，编号冲突 409，未知设备 400，无时区 422。压力数据继续用 POST /api/measurements，固定 MPa、node_id/value/observed_at/source。

GET /api/alarms?active=true 获取活动告警；since/until 为带时区 ISO，limit 默认 200、最多 1000。列表截取到 limit，当前没有分页游标；查询大量事件应缩小时间窗。active 状态截至 until，缺省为当前时间。

POST /api/sessions 携带 alarm_ids、symptom，device_ids 可空以推导设备。维修完成后，发送状态 cleared 的新事件，其业务时间须在维修完成后，来源与故障码须与发生事件一致。后续新发生事件会重新阻止关闭。

本接口只落地通用接入契约，未对接真实 PLC、MQTT、IoT 平台。实际厂商认证、字段映射和网络配置须按现场契约联调，不在代码中填写或提交现场密钥。
