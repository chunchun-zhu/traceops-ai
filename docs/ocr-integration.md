# OCR 与 PDF 表格接入指南（0.5）

文字层 PDF 的表格可以直接使用，不需要 OCR 模型。扫描页所需服务由你最后部署；核心 API 不下载识别权重。识别服务的代码、接口及替身测试已提供，真实 PaddleOCR 环境、模型效果和资源占用尚未验收。

## 先体验文字层表格

```powershell
python examples/generate_pdf.py --output table-demo.pdf
```

在知识库上传生成的模拟 PDF，选择“自动识别扫描页”。任务完成后打开原文，核对页面图像、逐行表格和字符位置。该样本只用于演示，不是现场压力标准。

模式：`auto` 使用文字层，检测到扫描页才调用 OCR；`native` 强制文字层，有图像时提示可能漏字；`ocr` 每页都调用 OCR。扫描页未配置识别服务时任务失败，资料不入库；配置后可在任务页重试。自动检测采用启发式，不能保证发现所有混排扫描内容。

## 由你部署可选识别进程

建议单独创建 Python 环境。按 [PaddleOCR 官方 PP-StructureV3 文档](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md) 选择支持的 Python/PaddlePaddle CPU 或 GPU 组合并安装 PaddlePaddle，再安装本项目封装依赖：

```powershell
python -m pip install -r requirements.ocr.txt
```

`requirements.ocr.txt` 的 SDK 约束不是 GPU 兼容性保证。主项目锁文件不包含重型识别依赖。准备模型文件或允许首次初始化下载后，在识别进程终端设置：

```powershell
$env:SUZHANG_OCR_API_KEY = '<由你生成的识别服务令牌>'
$env:SUZHANG_OCR_MODEL_VERSION = 'factory-docs-2026-10-v1'
$env:SUZHANG_OCR_DEVICE = 'cpu'
# 可选：配置本地模型路径等，具体格式遵循官方 SDK
# $env:SUZHANG_OCR_PADDLEX_CONFIG = 'D:/models/structure.yaml'
python -m uvicorn app.ocr_service:create_ocr_app --factory --host 127.0.0.1 --port 8003 --workers 1
```

此步骤会初始化模型，由你执行。没有默认令牌或模型版本。模型、词典、配置变化时更新版本标签并重启识别进程。封装关闭文档旋转、矫正、文字行旋转和公式识别以保留原图坐标；歪斜页面需预处理或后续扩展。服务按锁串行推理，当前适合小规模核心版本。

随后给主 API 的 `.env` 添加配置并重启主 API：

```dotenv
SUZHANG_OCR_URL=http://127.0.0.1:8003/v1/ocr
SUZHANG_OCR_API_KEY=<与识别进程相同的令牌>
SUZHANG_OCR_TIMEOUT=120
SUZHANG_OCR_DPI=150
```

地址必须为完整接口 URL。核心应用只把渲染后的页图发送到这个地址；因此只配置你信任的服务。远程连接使用 HTTPS，令牌不提交仓库。Docker 主应用调用宿主服务时需使用适当的宿主地址并传入配置。`/api/health` 的 `ocr_configured` 只表示地址已配置，不证明模型能工作；应上传一份可核对的小样本验收。

## 自有识别服务契约

`POST /v1/ocr`，JSON 请求：`image_base64`（PNG）、`width`、`height`、`page`，可使用 `Authorization: Bearer ...`。

```json
{
  "engine": "your-engine",
  "model_version": "weights-and-config-v1",
  "width": 800,
  "height": 1200,
  "blocks": [
    {"kind": "text", "text": "模拟条款", "bbox": [10, 10, 790, 80], "confidence": 0.94},
    {"kind": "table", "rows": [["型号", "范围"], ["P-200", "模拟值"], [null, "空位置"]], "bbox": [10, 100, 790, 250], "confidence": null}
  ],
  "warnings": []
}
```

块按阅读顺序返回，类型为 text/heading/table，坐标为请求图像的像素，尺寸必须与请求一致；文字块必须非空。表格须矩形，可附 `spans`，每项为整型 `row/column/rowspan/colspan`。null 代表空或合并位置，不能自行继承数值。置信度未知时返回 null，不编造分数；当前 PP-StructureV3 封装使用块内可取得的文字识别分数最小值，它不是整份条款正确的概率。

文档最多 10 MB、500 页、解析后 500000 字符；表格最多 500 行、40 列；响应不超过 4 MB。渲染图像受到像素与字节限制。超时为 HTTP 连接/读写等待超时，不是整份文档的总耗时上限；取消在页边界检查，当前 HTTP 请求返回后才能取消。失败或取消后重试会重新解析各页。

## 原文与标准审核

保存原始 PDF 并展示页图；解析字符范围对应规范化页文本，坐标帮助定位块。表格切片保留整张表，防止范围与型号被普通段落分块分开。复杂多栏、跨页表格、图表、手写及数值仍需要人工核验。

账号模式下，引用 OCR 页或表格的压力标准须独立审核，勾选“已核对原始 PDF 页面图像中的型号、单位和数值”。API 对应 `visual_checked=true`，未确认拒绝批准。该操作是人员核验声明，不能自动证明标准正确。原图接口遵循账号或共享令牌鉴权，没有新增文档级权限。

同源文件、业务元数据、解析模式和模型版本复用首次成功结果；如要重解析，应使用真实的新版本标签。0.4 及更早入库的 PDF 没有保存源文件；重新上传后才可查看原图，旧历史引用保持原记录。成套停服备份会包含新原始 PDF，见 [运维指南](operations-guide.md)。
