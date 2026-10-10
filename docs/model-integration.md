# 模型部署后的接口接入

本项目不管理模型权重、GPU 或推理服务。你完成部署后，只需按以下契约设置 `.env` 并重启 API。

## 对话模型

`SUZHANG_LLM_BASE_URL` + `/chat/completions`，请求包含 `model`、`messages`、`temperature=0` 与 `response_format={type:json_object}`。

响应中的 `choices[0].message.content` 必须为 JSON 字符串：

```json
{"claims":[{"text":"该条款要求人工核对供气压力。","citations":[{"evidence_id":"实际提供的切片id","quote":"供气压力需人工核对","start":0,"end":9}]}]}
```

示例引文只展示格式，实际必须来自上下文。start/end 是该切片 text 内的 Unicode 字符位置，包含 start、不包含 end，不是页内位置或 UTF-8 字节偏移。引文长度须等于 end-start，连续逐字匹配，至少 8 字符，最多 2000 字符。每条结论最多 5 处引文，共最多 8 条结论；根、结论和引文均禁止额外字段。不要返回摘要、页码、标题或版本；服务端核验后生成这些来源信息。

生成上下文包含故障描述、供气路径、压力快照和检索证据；结论只解释文档候选，不以测点快照代替现场检查。服务端核验切片和完整页原文、有效资料与版本，保存 `validation.scheme=exact-quotes-v1` 与 `page_checked=true`。定位正确不代表语义支持成立，始终保留 `semantic_support=human_review_required`。任一条失败拒绝整份说明，状态记录为 rejected，并保留规则与人工核验路径；没有部分通过。

0.6 起旧 summary/evidence_ids 协议不再接受新输出，历史记录仍保留并标为尚未逐条校验。模型部署应支持 JSON 输出；若服务参数不兼容，调整 `app/models.py` 适配请求。模型不改变工作流状态、复测标准或设备控制。

## 向量模型

`SUZHANG_EMBEDDING_BASE_URL` + `/embeddings`：

```json
{"model":"BGE-M3","input":["查询文本","资料切片一","资料切片二"]}
```

响应按 input 序号包含等维数值向量：

```json
{"data":[{"index":0,"embedding":[0.1,0.2]},{"index":1,"embedding":[0.2,0.3]},{"index":2,"embedding":[0.3,0.4]}]}
```

数量、index、维数与有限数值均校验。向量接入后启用余弦 + BM25 + RRF；缺失服务或异常时显式保留 BM25。请先运行 `python scripts/build_index.py` 或点击知识库“增量构建向量索引”。页面提交返回任务编号，可在后台任务页查看进度、取消或重试。每批 32 个切片持久保存；查询只请求问题向量。未构建或有缺失会明确提示并保留 BM25。缓存按服务地址、模型名称与索引格式隔离；更换权重时也应更换模型名称，不能仅复用原名称。当前为线性余弦扫描及后台任务采用本地 worker；分布式队列与 ANN 留待扩展。

## 重排模型

`SUZHANG_RERANK_URL` 是完整地址，请求：

```json
{"query":"气压不足","documents":["切片一","切片二"]}
```

支持两种响应：

```json
{"scores":[0.3,0.8]}
```

或：

```json
{"results":[{"index":1,"relevance_score":0.8},{"index":0,"relevance_score":0.3}]}
```

须返回所有候选的 index 和分数；若服务默认只返回 top_n，调整其服务配置或适配器。当前请求未携带模型名，若重排服务强制 `model` 字段，在 `rerank()` 请求中加入实际部署名。

## 联调顺序

1. 先确认 `/api/health` 中对应配置为 true。这仅说明配置存在，不证明模型在线。
2. 增量构建向量，检查 `/api/indexes` 的 `vectors_missing=0`。再检索不含明确故障码的描述，例如“多设备气压不足”，期望路径 `hybrid_rrf` 或 `hybrid_rrf+rerank`；故障码精确路由不会调用向量服务。
3. 新建排障会话，检查 `mode=model_assisted`、`model_validation.status=anchored`、逐条 claims 及服务端生成的文档/页码/字符位置。
4. 人工核对结论是否被引文支持、数值/单位与现场适用性，运行自己的 [端到端标注评测](evaluation-guide.md)。
5. 服务不可用时检查 warnings；规则流程仍可进行，不会把降级隐藏为成功。

各接口超时默认 20 秒，可设置 `SUZHANG_MODEL_TIMEOUT`。代码关闭环境代理继承，防止本地模型请求被代理转发。API key 分别使用 `SUZHANG_LLM_API_KEY`、`SUZHANG_EMBEDDING_API_KEY` 与 `SUZHANG_RERANK_API_KEY`，通过 Bearer 请求头发送。不要将 `.env` 纳入版本控制或交接压缩包。
