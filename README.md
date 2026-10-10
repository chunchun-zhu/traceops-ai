# 溯障 · 设备诊断与协同排障助手

项目一的可运行核心版本：资料导入、设备供气关系、证据检索、多轮检查、人工确认、维修复测及交接。模型部署由你完成，当前版本无需模型也能验证流程。

## 先启动

需要 Python 3.11+，本次在 Windows / Python 3.13.5 验证。终端进入项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

脚本首次创建项目内 `.venv` 并安装依赖，随后启动本地服务。不修改系统执行策略。也可以手动启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080
```

- 操作页面：<http://127.0.0.1:8080>
- API 文档：<http://127.0.0.1:8080/docs>
- 健康状态：<http://127.0.0.1:8080/api/health>
- 停止服务：运行终端中按 `Ctrl+C`。

依赖的可读版本约束见 `requirements.txt`，本次测试环境冻结版本见 `requirements.lock.txt`。需要精确复现时用锁文件安装。服务器启动工厂为 `app.main:create_app --factory`，不是 `app.main:app`。

## 五分钟演示

1. 默认选中一号冲压机和一号气动装配台，保留“同时气压不足”的描述，点击开始排障。
2. 系统找到共同上游 `BR-A`，显示手册、历史工单与引用位置；打开原文进行核对。
3. 总管检查录入模拟读数 `0.72 MPa`，提交“正常，继续”。
4. 支路检查录入模拟读数 `0.35 MPa`，提交“异常，待确认”。
5. 记录人工核验人与说明，点击“记录人工确认”，进入待维修状态。
6. 填写实际维修人、处理和完成时间，保存进入复测。
7. 为两台设备填写维修后的模拟读数（如 0.70 MPa）、人工核验的范围（仅演示可用 0.60–0.80）和适用手册，勾选告警消除，提交复测通过后关闭。可导出包含全过程的交接 JSON。

也可提交“无法核验”，检查人工接管分支。关闭再启动服务后，历史会话仍在 SQLite 中，可从记录恢复。跨 `PRESS-01` 与 `CNC-01` 排障时共同上游是总管，下一步分别核对 A/B 支路。

所有预置手册、工单、设备、关系和测点均为虚构模拟数据。演示范围 `0.60–0.80 MPa` 不是现场设备操作标准。页面不执行设备控制。

## 已实现与后续边界

| 能力 | 核心版本实现 |
|---|---|
| 文档解析 | PyMuPDF 文本层 PDF、UTF-8 TXT/MD；页码、章节、字符范围；重复内容与元数据幂等入库 |
| 工单标准化 | `kind=work_order`，分别保存初步判断与最终确认字段；JSON API 可导入 |
| 检索 | 持久化词法索引、增量入库 BM25、故障码分流、型号/版本过滤、原文引用 |
| 混合检索 | 显式批量增量构建、SQLite 向量缓存与模型隔离；查询仅编码问题，余弦 + RRF + 可选重排 |
| 设备关联 | SQLite 图数据、BFS 上下游、共同上游、潜在影响设备；JSON 导入校验 |
| 排障编排 | LangGraph 完整节点编排、原生 interrupt/resume；检查、确认、维修、复测和交接 |
| 数据恢复 | 原生 SQLite checkpoint + 会话/事件投影恢复；文件锁及 revision 拒绝重复反馈 |
| MCP | 官方 Python SDK stdio 服务，4 个只读工具，已用真实客户端调用验证 |
| Skills | 单场景技能说明和 JSON 检查配置，记录版本；不是通用动态技能平台 |
| 数据接入 | 台账 JSON 导入、压力快照 API/页面录入；尚无真实 IoT 或告警接口 |
| 观测 | 请求 id、耗时响应头和服务访问日志、会话节点轨迹 |
| 测试 | API、状态恢复、资料过滤/引用、模型 HTTP 契约及 MCP 协议集成测试 |

本版本没有部署模型，也未搭建 Elasticsearch、Neo4j、PostgreSQL、TimescaleDB、Redis、Celery、MinIO、OCR 或 OpenTelemetry 后端。Docker 文件已提供，当前环境未运行容器验收。没有真实工业评测，不能沿用简历历史指标。

当前使用 LangGraph 原生节点 checkpoint，等待人工输入时 interrupt，接口通过 Command(resume=...) 继续。节点结果保存后，业务投影写入失败可在下一次读取修复。正在执行而尚未保存 checkpoint 的节点可能重跑；不保证外部模型调用恰好一次。模型接入后只解释证据，不改变规则分支；引用校验只验证 id 存在，不自动证明引用内容支持结论。

## 文件布局

```text
app/
  main.py          API 与静态页面
  schemas.py       请求与模型输出约束
  store.py         SQLite 事务、快照、事件
  ingest.py        PDF/文本解析与分块
  indexing.py      持久化词法统计与增量向量缓存
  retrieval.py     精确查询、BM25、向量、RRF
  topology.py      图导入与供气路径查询
  workflow.py      LangGraph 全流程、人工中断与恢复
  models.py        后续模型服务的 HTTP 适配器
  mcp_server.py    MCP stdio 工具
  static/          浏览器工作台
skills/            供气排障说明与检查配置
examples/          台账、文档、工单与模拟评测样本
scripts/           检索评测脚本
tests/             集成测试
docs/              需求、设计、模型接入与验证记录
```

## 导入自己的数据

“供气关系与测点”页可导入 `examples/topology.json` 格式，支持节点新增/更新及关系追加。该接口不会删除已有关系；关系纠正与删除需后续增加审核操作。

“设备知识库”页导入手册时填写适用型号，必须与设备台账的 `model` 相同。公共供气资料使用 `AIR-SYSTEM`。PDF 最多 500 页、文件最多 10 MB，任一页没有文本层则提示先 OCR，不会静默跳过扫描页。原文位置对应解析后的页文本，不是 PDF 图像坐标。

工单用 `POST /api/documents`，格式见 `examples/work-order.json`。压力快照用 `POST /api/measurements`，固定 MPa、带时区 ISO 时间及来源。已开始会话保留旧证据与测点快照，新会话使用最新数据。

需要干净数据环境时，在首次启动前配置 `.env`：

```dotenv
SUZHANG_DB_PATH=data/factory.db
SUZHANG_SEED_DEMO=false
```

再导入现场已核验台账与资料。不自动删除演示数据库。

## 模型部署完成后的接入

复制 `.env.example` 为 `.env`，配置需要的服务地址，重启 API。LLM 和 Embedding 的地址须包含服务路径前缀（通常 `/v1`），重排是完整 URL。三个服务可分别接入。

```dotenv
SUZHANG_LLM_BASE_URL=http://127.0.0.1:8000/v1
SUZHANG_LLM_MODEL=Qwen3-32B
SUZHANG_EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
SUZHANG_EMBEDDING_MODEL=BGE-M3
SUZHANG_RERANK_URL=http://127.0.0.1:8002/rerank
```

这些地址只是示例，代码不会启动模型。接口字段与检查办法见 [模型接入](docs/model-integration.md)。未配置 LLM 时会话 `mode=rule_demo`；配置成功且结构/引用通过时为 `model_assisted`；失败时显示原因并保留规则流程。

部署 Embedding 后，在知识库点击“增量构建向量索引”，或运行 `python scripts/build_index.py`。导入自动维护词法索引；向量每批 32 个持久保存，重复构建复用已完成向量，查询只计算问题向量。停用资料从新检索排除，历史引用保留。向量检索仍为线性余弦扫描，构建为同步任务，适合核心版本；大规模 ANN 和任务队列尚未实现。

数据备份须保留业务数据库及同目录的 `.checkpoints` 文件；服务停止后一起备份。旧版人工确认已结束的记录会迁移为待维修，保留原结论，补齐复测后才能关闭。详见 [闭环与持久化设计](docs/upgrade.md)。

## MCP 工具

先启动一次 API 生成示例数据，再让支持 stdio MCP 的客户端运行：

```json
{
  "mcpServers": {
    "suzhang": {
      "command": "D:/path/to/suzhang/.venv/Scripts/python.exe",
      "args": ["-m", "app.mcp_server"],
      "cwd": "D:/path/to/suzhang",
      "env": {"SUZHANG_DB_PATH": "D:/path/to/suzhang/data/suzhang.db"}
    }
  }
}
```

工具：`get_device_registry`、`find_common_supply`、`get_pressure_snapshots`、`search_maintenance_evidence`。应用内直接复用相同服务，不通过 MCP 网络调用。

## 验证与评测

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/evaluate.py --output docs/retrieval-smoke.json
```

默认 6 条检索样本是模拟冒烟用例。标题标注级指标按前 K 个切片去重后计算 Hit、Recall、MRR，不应与切片级标注或简历的真实业务指标混用。替换 `--cases` 可使用自己的人工标注集。

## Docker

```powershell
docker compose up --build
```

默认仅绑定本机 8080，命名卷保存 SQLite。Compose 没有加载宿主机模型配置；需要模型时在 Compose 添加同名环境变量。容器里的 `127.0.0.1` 是容器自身，宿主机服务在 Docker Desktop 可用 `host.docker.internal`。实际容器启动未在本次环境验证。

本地演示默认无鉴权。共享部署前配置 `SUZHANG_API_TOKEN`，页面“设置访问令牌”填写同一个值。该令牌是项目级访问控制，未实现用户、班组、租户、资料权限与确认人身份认证；操作人字段仅为人工记录。日志不记录令牌或模型响应正文。

## 实现参考

本次核对了 [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 与 [FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/)。依赖实际安装版本见锁文件。
