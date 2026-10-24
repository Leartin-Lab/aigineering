# F1c：校准报告 CLI 的失败边界

状态：单包及 F1b/F1c 组合开发增量独立验收通过。基线 `2d8d4b626d4640ce948c7790a08c3e349f480d3b`。本包遵循[工作流适配](../development-workflow-adapter.md)，属于[阶段计划](sustained-engineering-delivery.md)的 F1；只增加两份测试文件，不改变运行时或业务协议。

作者 Luna recheck_tools，root 集成；非作者 integration_review 负责计划审计，非作者 Luna recheck_planning 负责本包实现及最终组合验收。两者均未参与本包作者工作。F1a 已验收，F1b 的直接循环不是本包开工依赖；F1c 与性能修复没有代码依赖。真实 provider、密钥和付费均不参与。

## 当前缺口与可行性结论

F1a 已覆盖正常闭环、工具 schema 故障后的实际新恢复执行、缺 verifier 的原义务 resume。verifier 直接单测和 planning budget 编译单测不能代替相应 CLI 路径。

隔离可行性探针使用原 CLI、明确本地 scripted Worker、独立 Store 与专用测试 Redis，确认以下当前行为；探针修正配置的失败轮次保留，不当作产品结果。它不是正式测试验收，也不是真实模型执行。

| 场景 | 已核实的行为 | 不得替换成的结论 |
| --- | --- | --- |
| root 返回 `/tool {not-json` | `worker.invocation_failed` 类别为 `worker_error:candidate_encoding_failure`，精确 root terminal failed；新 recovery 已发布但未领取执行；CLI failed，导出 not_completed | 不称 LLM/provider JSON parser，不称恢复完成 |
| Researcher 发布有效 report，但 evidence 删去一条观察及其 ID | report/evidence 实际 committed，verifier 实际领取后 invocation_failure、child terminal failed；无 attest/qualification；root expanded，含 submitted 与 descendant_failed blocker；bounded run timed_out、导出拒绝 | 不把 earlier 部分输出形状拒绝当 verifier 检查；不臆测 generic invocation_failure 内部原因，不把 root 写成 terminal |
| Researcher child budget=2 | 真实 normalize/analyze 工作消耗该资金链，后继 continuation 请求1而 source available0 被拒，失败 child 及 root blocker 可见；无合格导出 | 不称整个 root 余额耗尽，不用 budget=1 编译拒绝冒充运行耗尽 |

三种错误都用原 owner 形成事实；测试只读检查。scheduled recovery 只是新工作，不算 committed recovery 或成功交付。

## 固定交付与边界

只新增 `tests/fixtures/thermal_calibration_cli_failures.py` 和 `tests/test_thermal_calibration_cli_failures.py`。fixture 可复用已冻结 F1a 的纯本地 Worker 行为；不导入 Store/Runtime/scheduler，不发布或修改事实，不驱动恢复。它只产生普通 Worker 原始 action，分别在 root 产生固定畸形文本、在正常 researcher 最终 evidence 中删除一条已复制观察、在普通 blueprint 中将 researcher budget 改为2。A 工具/oracle、生产 verifier、Skill、配置、F1a/F1b 及全部 `src/` 文件均不改。

新测试独自复制 production preset 并精确替换全部模型 Worker 配置为显式测试 factory，保留 ToolWorker 与原 verifier。断言最终 fleet 不含 LLM/provider factory；测试设置禁止真实 provider 构造的 guard，避免配置遗漏在操作者已有凭据时发送请求。该 guard 只阻止网络/模型，不能代替任务 Worker 或产生成功结果。

三场景每项独立 domain/identity/workspace/SQLite/pytest base，串行使用协调的专用 Redis。不得默认连接操作者 Redis 或清空其他测试库。CLI 的 bounded timeout 只是结束观察窗口；必须正向证明目标 child/failure/余额事实已发生，不能只断言 timed_out 或没有输出。

## 行为验收

1. 畸形 action：核实际 CLI failure category、精确 claim grant/package/epoch、失败事实与 root terminal；核 recovery 的原 failure 关联及实际声明，证明尚无该 recovery claim，而不是暗示执行。只读 task status/diagnostics 的 blocker 与历史相符。
2. 缺观察：证明最终 report/evidence 的精确 Asset 与原 researcher claim/committed head，正常双工具观察确实存在，只有 copied evidence 删除目标项；原 verifier 已 claim 且失败，无 attestation/qualification。不得通过直接调用 verifier 单测冒充 CLI。
3. 额度：证明签名声明 child budget=2、exact grant/reservation/工具工作，指出精确耗尽 source 与 requested1/available0 的 rejected Candidate，关联失败 continuation 的 claim/终态。root 余量不能混记为零。缺失/错误资金来源或只在编译阶段拒绝均不满足这一场景。
4. 所有场景核 root 的准确 projected 状态和 blocker；用原 `task result` owner 导出，要求非零 `not_completed` 且没有目的文件；不能只检查 stdout 的 completed=false。保留事实/claims/receipt/head 完整 witness，并通过现有只读 owner 检查，不编写第二套接受规则。
5. 关闭 Store 后重开，核上述状态和事实身份一致；只读审计前后 Store bytes 或语义/事实清单不变。若做 rebuild，只在私有副本按已有停写语义执行，不修原事实。F1a 既有成功与真实工具恢复测试仍通过，未新增 in-place claim reset 或 example scheduler。

## 验证、证据与交付

先保留新行为红灯/缺失覆盖，再实现两测试文件；作者跑精确定向与相关 F1a 组合、Ruff，非作者独立核真实 CLI 和只读 witness。每个 verification process 独立证据目录，命令/原始 stdout/stderr/exit 保存；配置/fixture 错误与产品失败分别报告。unknown usage/cost 保持未知；这些 scripted 失败不扩充为真实模型统计样本。

root 在准确集成快照运行完整规定门禁、树外安装后的同组测试、文档治理与制品检查。wheel 中只有 aigineering；测试和 examples 分别作为冻结验证 bundle，不能声称已进入 wheel。无需 ADR/change：这是现有规则的组合测试，不改变业务事实或架构。若验证暴露真实产品缺陷，先固定可复现反例并按其 owner 另行计划，不在本包悄悄改 kernel。

## 规划—审计与开发—验收记录

integration_review 对计划 SHA-256 `90b986991d737febeadd00a953a8594c07d8eace37d56c658da428f324c865fa` 独立审计 Ready；另独立只读复核三份探针 Store，确认精确失败、root blocker 和导出范围，源字节不变。作者隔离实现中。没有 provider、一般自主恢复可靠性、质量/成本或稳定发布结论；F1/F2/G 的其余资格仍开放。

Luna recheck_planning 非作者独立9项组合通过，另核实际 Store 与普通 failure_result 诊断任务，未复活原失败业务任务。随后只补预算 source 自身唯一 failed 终态断言，非作者独立重跑该用例1项通过，旧9项快照分别保留。最终两文件摘要 `6b757f366cb334ddf22f119fb52e780739287be81a42c6a4c2298889e0428e3a`。root 与 F1b 组合验证，见[组合记录](../../reports/111-direct-harness-protocol-development-2026-10-24.md)。
