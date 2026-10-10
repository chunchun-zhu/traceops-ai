# 端到端评测报告

数据集：供气排障与引用回归（模拟） / v1 / synthetic
来源：项目自编模拟资料与状态契约；模型响应均为明确标记的接口替身，不是现场故障或模型准确率标注。
数据集 SHA256：`f93c08ba29fe1a578302739891b597f12de445fe7d94c0d4464145fc89a3b5e4`

匹配预期的回归案例比例不是故障诊断准确率；引用定位不证明语义支持。模拟数据不代表真实工厂效果。

案例 43；通过 43；失败 0；预期匹配率 100.0%。

模型方式：rules_and_labelled_fixtures。执行时间 24276 ms，仅记录本轮耗时，不是性能基准。

| 案例 | 分类 | 结果 | 模型/结束状态 |
|---|---|---|---|
| retrieval-1 | retrieval | 通过 | exact_code /  |
| retrieval-2 | retrieval | 通过 | exact_code /  |
| retrieval-3 | retrieval | 通过 | bm25 /  |
| retrieval-4 | retrieval | 通过 | bm25 /  |
| retrieval-5 | retrieval | 通过 | bm25 /  |
| retrieval-6 | retrieval | 通过 | bm25 /  |
| exact-v2 | retrieval | 通过 | exact_code /  |
| empty-version | retrieval | 通过 | bm25 /  |
| empty-model | retrieval | 通过 | bm25 /  |
| code-boundary | retrieval | 通过 | bm25 /  |
| unknown-code | retrieval | 通过 | bm25 /  |
| model-code-isolation | retrieval | 通过 | bm25 /  |
| initial-check | workflow | 通过 | rule / checking |
| clarify-devices | workflow | 通过 | rule / checking |
| unsupported-handoff | workflow | 通过 | rule / handed_off |
| missing-documents | workflow | 通过 | rule / handoff |
| disconnected-device | workflow | 通过 | rule / handoff |
| cross-branch | workflow | 通过 | rule / checking |
| all-checks-normal | workflow | 通过 | rule / handoff |
| unknown-check | workflow | 通过 | rule / handed_off |
| early-confirm | workflow | 通过 | rule / checking |
| wrong-check | workflow | 通过 | rule / checking |
| stale-revision | workflow | 通过 | rule / checking |
| duplicate-check | workflow | 通过 | rule / checking |
| confirm-no-closure | workflow | 通过 | rule / repairing |
| restart-preserves-check | workflow | 通过 | rule / checking |
| repair-and-close | workflow | 通过 | rule / resolved |
| reject-missing-device | workflow | 通过 | rule / resolved |
| reject-outside-range | workflow | 通过 | rule / resolved |
| reject-tampered-range | workflow | 通过 | rule / awaiting_verification |
| reject-disabled-standard | workflow | 通过 | rule / awaiting_verification |
| reject-disabled-source | workflow | 通过 | rule / awaiting_verification |
| alarm-closure-gate | workflow | 通过 | rule / resolved |
| failed-retest-repair-again | workflow | 通过 | rule / resolved |
| unknown-retest-handoff | workflow | 通过 | rule / handoff |
| restart-preserves-repair | workflow | 通过 | rule / resolved |
| model-unknown_id | workflow | 通过 | mock / handoff |
| model-changed_quote | workflow | 通过 | mock / handoff |
| model-unbound | workflow | 通过 | mock / handoff |
| model-legacy | workflow | 通过 | mock / handoff |
| model-extra_field | workflow | 通过 | mock / handoff |
| model-timeout | workflow | 通过 | mock / checking |
| model-valid | workflow | 通过 | mock / checking |

## 检索指标

口径：top 5 chunks deduplicated into documents；标题级标签，仅正样本计算 Hit/Recall/MRR，负样本单独计分。

- label_level: document_title
- cutoff: top 5 chunks deduplicated into documents
- positive_count: 7
- negative_count: 5
- Hit@5: 1.0
- Recall@5: 1.0
- MRR@5: 1.0
- negative_pass_rate: 1.0

## 失败详情

本轮没有预期不匹配的案例。
