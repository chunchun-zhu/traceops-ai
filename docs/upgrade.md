# 0.2 闭环与持久化设计

本文件记录 0.2 版本；0.3 已增加后台队列、告警事件与结构化标准，当前行为参阅 README 和 docs/tasks、docs/alarms、docs/standards。

## 目标与验收

在可运行核心之上补齐维修复测、完整 LangGraph 人工中断与持久化检索索引。模型权重部署继续由用户负责，现场操作由人员完成，系统记录过程。

原因确认进入 `repairing`；保存维修进入 `awaiting_verification`。只有全部告警设备有维修后的读数、适用有效资料、读数在人工提供范围内且确认告警消除，才能 `resolved`。失败回到维修，无法核验进入人工接管。范围是否正确源自人工核验，代码未自动证明资料支持该数值。每轮维修及复测均保存，不覆盖失败记录。

## 状态与一致性

所有反馈动作进入独立图节点。`await_input` 使用原生 interrupt；HTTP 输入预校验通过后以 Command resume。`SqliteSaver` 的 thread_id 为 session id，checkpoint 文件为业务库路径加 `.checkpoints`。业务快照与事件是图状态的投影，图成功但投影失败，读取时补齐。节点错误在读取恢复时从保存点重试；中途未保存的模型调用可能重复。

跨进程文件锁串行化同一数据库的工作流操作，旧 revision 返回 409，不消耗人工中断。适合小规模核心部署，非生产吞吐保证。旧版确认结束记录迁移为待维修，原确认及检查保留。无 checkpoint 的旧记录在图入口重新挂接。

## 知识索引

导入事务同步维护词频及长度。启动补齐旧库缺失的词法索引。有效资料按型号和版本过滤后计算 BM25，停用/启用可逆。向量显式增量构建并按批持久保存；重试复用缓存，查询只编码问题。端点地址、模型名称及索引格式决定缓存键，密钥不入缓存键。配置更换后独立索引，同名权重更换须主动换名。查询遇到模型故障回退 BM25，不删除缓存。

当前使用 SQLite 存储、线性向量扫描、同步构建；没有 ANN、异步任务队列或生产规模压测。备份应停止服务后同时保存业务库和 checkpoint 库，锁文件不承载数据。

## API

- POST /api/sessions/{id}/repair：operator、action、带时区 completed_at、expected_revision。
- POST /api/sessions/{id}/verify：result（passed/failed/unknown）、operator、note、alarms_cleared、readings、expected_revision。读数字段为 device_id/value/minimum/maximum/observed_at/reference_document_id。
- GET /api/indexes：有效切片与当前模型向量覆盖率。
- POST /api/indexes/embeddings/build：已完成批次保存，失败可重试。
- POST /api/documents/{id}/status：active 布尔值，停用不抹除历史证据。

## 验证证据

新增测试覆盖维修前禁止复测、读数缺失、告警未消除、范围错误、时间错误、资料不适用、重复设备、失败后二次维修、无法核验、节点崩溃恢复、投影失败恢复、双写版本冲突、旧库迁移、词法排名一致、资料停用及恢复、向量增量/重启复用/配置隔离/故障降级。模型使用测试替身，未部署真实服务。
