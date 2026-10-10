# 溯障 · 设备诊断与协同排障助手

项目一的可运行核心版本：资料导入、设备供气关系、证据检索、多轮检查、人工确认、维修复测及交接。模型部署由你完成，当前版本无需模型也能验证流程。

## 0.6 逐条引文与端到端评测

- 模型每条说明绑定逐字引文及切片位置，服务端复核完整页原文、版本与页码；任一条不符拒绝整份说明，保留规则与人工核验路径。
- 页面逐条展示引用并高亮原文；旧说明保留为未按新规则校验。
- 新增隔离 API 评测工具，默认运行 12 个检索与 31 个流程模拟案例，生成 JSON/Markdown 和失败详情，接入 Windows/Linux CI。

引文定位校验不证明结论被原文支持。评测默认使用临时数据库、规则与明确标记的模型替身，不部署模型；说明与替换数据方法见 [评测指南](docs/evaluation-guide.md)。新模型响应契约为 claims，详见 [模型接入](docs/model-integration.md)。

## 0.5 PDF 表格与 OCR 接入

- 文字层 PDF 直接提取表格，保留行列、空单元格、页面坐标和完整表格切片。
- 扫描/混合 PDF 接入可选 OCR 服务；支持解析模式、逐页进度、失败重试和取消。
- 保存原始 PDF，来源窗口展示页图、解析方法、引擎版本及置信度；账号模式下 OCR/表格标准审批须确认原图核验。

无需模型即可体验文字层表格。识别服务封装已提供，模型由你最后部署；步骤、契约和边界见 [OCR 接入指南](docs/ocr-integration.md)。

## 0.4 运行保障与人员审核

- Windows/Linux CI：自动测试、依赖及语法检查；独立 Docker 构建和启动冒烟，不部署服务。
- 停服成套备份与恢复：业务库 + 流程检查点，校验损坏，恢复到新目录，保留任务和排障历史。
- 可启用账号模式：查看员、操作员、审核员、管理员；登录人员身份进入事件快照，标准由另一人独立审核后用于复测。

默认本地演示保持 `legacy`。启用账号、创建管理员、备份与恢复命令见 [运维操作指南](docs/operations-guide.md)。账号模式不接受原共享 Token 或手填审核人；历史标准须重新提交审核，已关闭会话保留。没有默认管理员密码。

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
7. 为两台设备填写维修后的模拟读数（如 0.70 MPa）、各自的“入口压力范围（模拟）”标准（0.60–0.80 MPa），勾选告警消除，提交复测通过后关闭。可导出包含全过程的交接 JSON。

也可提交“无法核验”，检查人工接管分支。关闭再启动服务后，历史会话仍在 SQLite 中，可从记录恢复。跨 `PRESS-01` 与 `CNC-01` 排障时共同上游是总管，下一步分别核对 A/B 支路。

所有预置手册、工单、设备、关系和测点均为虚构模拟数据。演示范围 `0.60–0.80 MPa` 不是现场设备操作标准。页面不执行设备控制。

## 0.3 新增操作

- 知识库上传返回任务编号，后台解析后自动入库；向量构建也在后台执行。任务页显示状态、进度、结果，失败可重试，已完成批次复用。
- 在“供气关系与测点”页录入或查询告警，选择未消除告警发起排障。会话保留发起时快照；关闭时还须存在维修完成后的同来源、同设备、同故障码消除事件。
- 知识库新增复测标准：人员核对压力范围，选择手册及原文条款、页码和审核人。复测选择标准自动带入范围，篡改范围、标准或资料停用均拒绝关闭。原文一致性校验不等于自动证明数值正确，仍由人员核验。

[后台任务设计](docs/tasks/02-方案设计.md)、[告警接入契约](docs/event-integration.md)、[标准设计](docs/standards/02-方案设计.md)。

## 已实现与后续边界

| 能力 | 核心版本实现 |
|---|---|
| 文档解析 | PyMuPDF 文字层 PDF/表格、可选扫描页 OCR、UTF-8 TXT/MD；页码、字符范围、解析来源、原图核对及幂等入库 |
| 工单标准化 | `kind=work_order`，分别保存初步判断与最终确认字段；JSON API 可导入 |
| 检索 | 持久化词法索引、增量入库 BM25、故障码分流、型号/版本过滤、原文引用 |
| 混合检索 | 显式批量增量构建、SQLite 向量缓存与模型隔离；查询仅编码问题，余弦 + RRF + 可选重排 |
| 设备关联 | SQLite 图数据、BFS 上下游、共同上游、潜在影响设备；JSON 导入校验 |
| 排障编排 | LangGraph 完整节点编排、原生 interrupt/resume；检查、确认、维修、复测和交接 |
| 后台任务 | SQLite 持久化任务队列、本地 worker、进度、取消、重试、重启恢复 |
| 复测标准 | 不可变标准、型号/手册版本/原文页码与字符定位、审核记录、复测快照 |
| 数据恢复 | 原生 SQLite checkpoint + 会话/事件投影恢复；文件锁及 revision 拒绝重复反馈 |
| MCP | 官方 Python SDK stdio 服务，6 个只读工具，已用真实客户端调用验证 |
| Skills | 单场景技能说明和 JSON 检查配置，记录版本；不是通用动态技能平台 |
| 数据接入 | 台账、人工压力快照、告警发生/消除事件、幂等接入、时间窗查询；真实 IoT 厂商联调尚未完成 |
| 观测 | 请求 id、耗时响应头和服务访问日志、会话节点轨迹 |
| 测试 | API、状态恢复、资料过滤/引用、模型 HTTP 契约及 MCP 协议集成测试 |

本版本没有部署模型，包括 OCR 模型；未搭建 Elasticsearch、Neo4j、PostgreSQL、TimescaleDB、Redis、Celery、MinIO 或 OpenTelemetry 后端。Docker 构建启动已在 GitHub Actions 验证，本机未运行容器。没有真实工业评测，不能沿用简历历史指标。

当前使用 LangGraph 原生节点 checkpoint，等待人工输入时 interrupt，接口通过 Command(resume=...) 继续。节点结果保存后，业务投影写入失败可在下一次读取修复。正在执行而尚未保存 checkpoint 的节点可能重跑；不保证外部模型调用恰好一次。模型接入后只解释证据，不改变规则分支；逐条引文核验精确文本、范围、页码和版本，不自动证明结论语义被引文支持。

## 文件布局

```text
app/
  main.py          API 与静态页面
  schemas.py       请求与模型输出约束
  store.py         SQLite 事务、快照、事件
  ingest.py        文档入库与来源分块
  document_parser.py PDF 文字层、表格与扫描页解析
  ocr.py           OCR HTTP 契约与受限表格转换
  ocr_service.py   可选独立 PP-StructureV3 服务
  tasks.py         持久化后台任务、取消、重试与恢复
  operations.py    告警事件、结构化复测标准
  indexing.py      持久化词法统计与增量向量缓存
  retrieval.py     精确查询、BM25、向量、RRF
  topology.py      图导入与供气路径查询
  workflow.py      LangGraph 全流程、人工中断与恢复
  models.py        后续模型服务的 HTTP 适配器
  grounding.py     逐条引文、页原文与版本校验
  evaluation.py    隔离 API 场景评测与报告
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

“设备知识库”页导入手册时填写适用型号，必须与设备台账的 `model` 相同。公共供气资料使用 `AIR-SYSTEM`。PDF 最多 500 页、文件最多 10 MB、解析后最多 500000 字符。扫描页未配置 OCR 时拒绝整份导入；配置后可重试，不静默跳过失败页。字符位置对应规范化页文本，块坐标另存为 PDF 点或渲染像素。表格空位置不自动继承数值；标准须人工核对型号、单位和范围。

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

部署 Embedding 后，在知识库点击“增量构建向量索引”，或运行 `python scripts/build_index.py`。导入自动维护词法索引；向量每批 32 个持久保存，重复构建复用已完成向量，查询只计算问题向量。停用资料从新检索排除，历史引用保留。向量检索仍为线性余弦扫描，页面提交后台构建，任务进度、取消和重试在“后台任务”页查看。当前采用本地线程 worker 与持久化队列，适合核心版本；大规模 ANN 和分布式任务调度尚未实现。CLI 与旧构建 API 仍同步执行以兼容原用法。

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

工具：`get_device_registry`、`find_common_supply`、`get_pressure_snapshots`、`search_maintenance_evidence`、`get_alarm_events`、`get_pressure_standards`。应用内直接复用相同服务，不通过 MCP 网络调用。

## 验证与评测

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/evaluate.py --output docs/retrieval-smoke.json
.\.venv\Scripts\python.exe scripts/evaluate_e2e.py --output docs/evaluation-smoke.json --markdown docs/evaluation-smoke.md
```

默认 6 条检索样本是模拟冒烟用例。标题标注级指标按前 K 个切片去重后计算 Hit、Recall、MRR，不应与切片级标注或简历的真实业务指标混用。替换 `--cases` 可使用自己的人工标注集。

新增端到端工具默认 43 个模拟案例，临时库隔离当前业务数据；任一案例未符合预期时返回非零退出码。正负检索样本分别计分；流程通过率只表示预期匹配，不是诊断准确率。部署模型后可显式 `--live-models`，应使用匹配真实资料与预期的标注集。

## Docker

```powershell
docker compose up --build
```

默认仅绑定本机 8080，命名卷保存 SQLite。Compose 没有加载宿主机模型配置；需要模型时在 Compose 添加同名环境变量。容器里的 `127.0.0.1` 是容器自身，宿主机服务在 Docker Desktop 可用 `host.docker.internal`。GitHub Actions 验证了镜像构建、健康状态、静态页面与模拟设备读取，未包含 GPU/OCR 模型运行。

本地演示默认 legacy、无鉴权；可配置项目级 `SUZHANG_API_TOKEN`。需要登录人员身份与独立审核时启用 accounts，按 [运维指南](docs/operations-guide.md) 创建管理员并配置来源及 Cookie。账号模式提供角色和人员事件快照；未实现租户、班组或文档级权限。日志不记录令牌或模型响应正文。

## 实现参考

本次核对了 [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/)、[PyMuPDF Page API](https://pymupdf.readthedocs.io/en/latest/page.html) 与 [PP-StructureV3](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md)。核心依赖实际安装版本见锁文件。
