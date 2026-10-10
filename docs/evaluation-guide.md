# 引用校验与端到端评测（0.6）

## 本地运行

```powershell
python scripts/evaluate_e2e.py --output docs/evaluation-smoke.json --markdown docs/evaluation-smoke.md
```

默认 `examples/e2e-cases.json` 有 12 个检索、31 个流程案例，均为自编模拟数据。流程包含 7 种模型协议/宕机替身。临时库在操作系统临时目录创建并在结束后清理，不读写你的运行库。默认清空模型端点，CLI 关闭 tracing；不下载模型，不调用真实识别或推理服务。

JSON 记录具体断言与失败详情；Markdown 展示概览。默认全部案例须匹配预期，否则进程退出 1；无效输入退出 2。可用 `--min-pass-rate 0.95` 调整回归门槛，报告仍保留每个失败；该门槛不是效果验收标准。Windows/Linux CI 同时运行测试与 43 个案例。

报告注明数据来源、类别、修订版及规范化内容 SHA256。Hit/Recall/MRR 按 top K 切片去重后的标题标签计算，只统计正样本；负样本按“是否没有结果”单独统计。`--k` 为 1–20。完整闭环的预期匹配率不是诊断准确率。默认 43/43 不代表现场效果，也不复现简历历史指标。

## 自己编写案例

参照示例 JSON。顶层必须包含 name/revision/kind/provenance，检索或流程组至少有一个案例，ID 全局唯一。`kind=annotated` 要求 `seed_demo=false`，通过 `topology` 与 `documents` 显式提供资料；该标记只是你声明的来源类别，不自动证明人工标注质量。文件不超过 5 MB，案例不可执行任意代码或指定外部业务库。

检索案例包含 query、可选 model/version，以及不重复的 relevant_titles，或 `expect_empty=true`。可附 expected_route。当前是标题标签，应避免重名文档。

流程案例包含 start（设备、现象）、expected（初始状态路径）、steps。支持 clarify、feedback、confirm、repair、verify、restart、export、clear_alarms、document_status、standard_status。步骤包含 payload、expected_http（默认 200）、expected（响应字段精确比较）。路径如 `current_check.id`、`checks.0.result`、`session.status`；缺失、类型或数值不符都失败。API 拒绝动作后还检查会话 revision 不变。

payload 占位符只在整个字符串等于下列值时替换：

- `${revision}`：当前会话版本；未填写 expected_revision 时自动带入。
- `${check_id}`：当前检查编号。
- `${now}`：执行该步骤时的带时区 UTC 时间。
- `${document:P-200}` / `${standard:P-200}`：该型号唯一手册/标准 ID，包括已停用项，以便验证拒绝分支；不唯一时案例失败。

状态停用步骤须填写 target_model。`with_alarms=true` 为 start 中各设备建立模拟告警；clear_alarms 为同来源、同设备、同故障码写入执行时刻的消除事件。restart 使用同一案例临时库，保留检查点。

`disabled_document_models` 用于资料不足分支。model_fixture 可选 valid、unknown_id、changed_quote、unbound、legacy、extra_field、timeout；这些固定替身仅测试协议与降级，不评估模型语义质量。所有协议错误都拒绝整份说明，没有“部分结论通过”。

## 模型部署后验证

```powershell
python scripts/evaluate_e2e.py --cases my-annotated-cases.json --live-models --output my-report.json --markdown my-report.md
```

这个显式选项才读取 `.env` / 环境中的真实模型配置，仍运行临时数据环境。请准备与你的型号、文档、实际预期匹配的数据集；默认规则预期集不等于真实模型效果集。指定 model_fixture 的案例依然使用替身，报告逐案例区分 mock/live/rule。真实调用会把评测资料发到你配置的服务。真实模型部署和效果验收由你完成。

## 逐条模型引文

新模型契约见 [模型接入指南](model-integration.md)。每处引文必须连续逐字匹配，引用位置按 Unicode 字符计数；服务端核验完整页文本并补齐来源。通过后记录 `anchored`，结构、切片、引文、页原文或有效版本不匹配时为 `rejected`，HTTP 服务异常为 `unavailable`。

页面展示每条结论、引文、版本和页码，可点击核对并高亮原文；PDF OCR/表格仍需原图核验。旧摘要保留为“历史模型说明 · 尚未逐条校验”。旧切片若曾规整空白导致位置不一致，会拒绝模型说明；用真实的新资料版本导入，可生成精确切片。

定位正确不意味着结论成立。当前保留 `semantic_support=human_review_required`，不自动核验语义蕴含、数值换算、单位正确性或现场适用性。模型拒绝或故障时保留规则检查，人员可转接管；模型不会自动确认根因、生成可信复测标准或关闭故障。
