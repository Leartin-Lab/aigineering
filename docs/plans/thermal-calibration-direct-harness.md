# F1b：直接工具循环离线协议夹具

状态：单包、窄维护及 F1b/F1c 组合开发增量独立验收通过。基线 `2d8d4b626d4640ce948c7790a08c3e349f480d3b`，2026-10-24。

本包遵循[工作流适配](../development-workflow-adapter.md)，属于[阶段计划](sustained-engineering-delivery.md)的 F1b。作者 Luna recheck_tools，root 集成，非作者 integration_review 审计与验收。

## 目标与接口结论

F1b 先以 scripted model 离线证明“直接模型工具循环”可调用与 F1a 相同的冻结纯工具、可把完整工具结果回送模型、可把最终 report 交给同一个外部 oracle，并能逐轮留下不可覆盖的成功/失败记录。它建立未来直接臂的最小协议 seam；不证明真实模型动态规划、质量/成本公平性、真实恢复或 provider 可用性。

已核实：`src/aigineering/agent/harness.py` 中的 `HarnessCandidateAdapter` 把 harness action 编译为签名 Candidate，经 `compile_worker_envelope` 进入 Worker/commitment 边界；因此不适合直接臂。仓库没有第三方 Harness SDK 依赖。可复用的窄接口是 A 包 `examples.thermal_calibration.tools:build_registry()` 返回的 `ToolRegistry.get_spec/list_specs/run`、`agent.tool_registry_loader.provider_tool_definitions()` 生成的原生 OpenAI-compatible function schema，以及冻结 oracle `examples.thermal_calibration.oracle:evaluate_report`。F1b 不调用 `agent.llm` 或 HarnessCandidateAdapter，也不新增 runtime 集成。

直接臂在单个进程/调用中用注入的模型客户端协议驱动原生 `tool_calls` 循环；工具仅经 ToolRegistry 的纯 handler 执行。它不创建 Contract、Candidate、claim、observation Asset、task graph、scheduler、Redis/SQLite 事实、attestation 或 completion。测试里的 scripted tool-call 序列是预录协议输入，不是动态计划或质量样本。Runtime 臂继续使用冻结的 F1a CLI preset。两个臂共享 A eval 输入、工具实现与 oracle；其余真实对照是否可比须由后续 G 另审。

## 冻结的离线 client wire 子集

client seam 为 `complete(*, messages, tools, timeout_seconds) -> str`，返回严格 JSON 文本。它不是整个 provider ChatCompletion 对象；G 的独立 adapter 将来负责映射实际 provider 响应及 usage。请求的 messages 遵循 system/user/assistant/tool 消息结构，tools 来自冻结 registry schemas，timeout_seconds 为当前剩余正数时间。

响应对象必须恰有 message、usage 两键；F1b scripted client 的 usage 只能 null。message 恰有 role、content、tool_calls 三键，role 固定 assistant。工具响应的 content 必须 null，tool_calls 必须为恰一个元素的数组；该元素恰有非空唯一 id、type="function"、function，function 恰有已注册 name 和严格 JSON object 文本 arguments。最终响应的 tool_calls 必须为空数组，content 必须为非空严格 JSON report 文本。两种形式互斥；不接受额外键、finish_reason/choices 或 SDK 对象。整个运行的 tool-call IDs 不得复用。所有 JSON 文本拒重复对象键和 NaN/Infinity。多 call 明确拒绝，不能执行部分再拒绝。

调用前将原样 assistant message 追加到会话；工具结果以 `{"role":"tool","tool_call_id":id,"content":text}` 回送，不另带 name。text 是严格 JSON 文本，恰有 ok、result、error_code：成功时为 true、registry 原始返回字符串、null；显式可恢复故障时为 false、null、transient_tool_failure。后续模型请求必须含这条消息。最终 report 必须来自模型 final content，loop 不代模型生成或补字段。

解析前 UTF-8 byte cap 固定为：完整 client response 1,048,576，arguments 524,288，final report 262,144；均为正整数，拒 bool。不截断后继续解析。模型请求次数在调用 client 前计数，工具 attempt 在完成 wire/arguments/input schema 验证后、进入 registry.run 前计数；失败调用也计数。协议错误不算已执行的工具 attempt，但保留请求与拒绝事件。

## 错误与停止的所有者

本包唯一可恢复异常为 DirectTransientToolError，只由显式本地故障注入 wrapper 抛出，A handlers 不改。只捕获该类型并回送 tool error，计入 attempt/turn 上限。未知工具、wire/arguments JSON 错误和预先按原 ToolSpec 校验的 input schema 错误立即终止，分别记录 protocol、tool_arguments、tool_input_schema。registry.run 的 ToolSchemaValidationError / ToolOutputLimitError 分别为 registry_validation / tool_output_limit 终止，不按错误文字猜可恢复性。其他 handler/client 异常（含 ValueError、TypeError）终止为 tool_handler_error / client_error，只记录安全类型/代码，向调用者重新抛出原异常。顶层 CLI 可为保存失败 manifest 和非零退出码捕获异常，不得送回模型、继续成功或吞掉编程错误。client TimeoutError 单列 client_timeout，保留失败。

限额禁止下一次对应调用：恰第 N 次模型调用返回合法 final 可成功；工具上限已到仍可发未超模型上限的 final 请求，只有下一工具调用被阻止。deadline 在每次 client/工具调用前后检查；同步调用返回时若已过期即终止，不发布成功。fake clock 验证边界，不承诺抢占同步 handler 或任意 client。

## 最小文件所有权

作者 Luna recheck_tools；root 集成。只新增以下文件；不改 A/F1a 冻结文件、runtime owners、依赖或现有 eval 内容：

- `examples/thermal_calibration/direct_harness.py`：小型、无持久状态的工具调用 loop，接受注入的 native `ModelClient` protocol、case/standard、冻结简报、registry、精确限额、单调时钟/timeout seam 和 run-event sink。client 返回原始 response str，以严格 JSON parser（duplicate-key hook、拒绝 NaN/Infinity）读取完整 native envelope，并分别严格解析 tool arguments/final report；拒绝缺失/额外或类型错误的 envelope 字段、重复 tool-call ID、空 final、单轮多于一个 call（本切片禁止并行）、坏参数。限额必须是正整数并拒绝 bool。deadline 用 monotonic clock 在每次 client/tool 边界前检查，并给 client 传剩余 timeout；同步 ToolRegistry handler 或不遵守 timeout 的任意 client 不可被安全抢占，因此只能声称边界检查的 cooperative deadline，不能声称硬总 timeout。Protocol error 终止；registry transient execution failure 作为该 call 的 tool error message 回送模型，可继续到冻结限额。final report 由同一个 Fraction oracle 判定。没有自己的任务分解器/scheduler；loop 不负责发送网络请求或读取凭据。
- `examples/thermal_calibration/direct-brief.md`：与 F1a Skill 业务知识等价的简报，删除 `/plan`、Contract、Worker、claim、attest 等 Runtime 专用指令；含合成边界、两个输入角色、工具使用、报告要求和 outside-tolerance 解释。
- `examples/thermal_calibration/direct-run.py`：仅 offline fixture CLI；没有 HTTP/provider client、endpoint/key 参数或环境变量读取。
- `examples/thermal_calibration/direct_fixture.py`：随 sdist 交付的明确 scripted client 与预定故障 wrapper，仅供显式离线 CLI/测试；不成为 runtime 默认 Worker。CLI 不导入 tests；测试 fixture 可复用此模块并加入对抗响应。
- `tests/fixtures/thermal_calibration_direct_model.py`：确定性 scripted native tool-call client，覆盖正常序列及预定错误/fault 序列；可用 fake monotonic clock 推进 deadline。
- `tests/test_thermal_calibration_direct_harness.py`：离线行为和依赖边界测试。
- `examples/thermal_calibration/direct-README.md`：只说明如何运行离线 fixture、证据格式和限制；不提供真实 provider 调用说明。

若必须修改 A/F1a 文件、公共协议、依赖或 runtime 才能完成，先停下并更新计划审计，不扩大本包。无需 ADR/change：只新增离线对照夹具，不改变实现架构/业务规则。

## 离线执行和证据

测试用 frozen eval manifest 中的 `near-threshold-fahrenheit` 与 `short-interval`，以准确 JSON bytes 和哈希绑定输入。正常 scripted 路径至少执行：`normalize_calibration`，把完整 `result` 传给 `analyze_calibration`，产出 final JSON report，再调用 `evaluate_report`。Tool schemas 从 ToolSpec 动态取得，不另复制业务 schema/算法。断言 oracle 的 `passed` 与 `within_tolerance` 分别解释；正确但 outside-tolerance 可以 `passed=true`。

Direct loop 对每次模型请求/工具尝试/返回/终止写 append-only JSONL；每轮新建唯一 run ID 和排他目录，不覆盖已存在结果。最小 manifest 记录代码/工具/oracle/简报/输入哈希、case、模型字段（离线为 `scripted`）、限额、注入 fault、phase/status、oracle verdict、时间与 exit code。错误保留安全错误类别和阶段，不落凭据、HTTP headers、原始 Store 或含 secret 的 prompt。测试验证失败事件仍在，后续结果不会把失败覆盖。失败轮次和 stdout/stderr 按 run 分开保留。

离线 usage 字段必须是 `null` 并标 `unavailable`，不能填零；模型费用、人工分钟/成本与总成本均为 `null`/`incomplete`。本包不计算或推断成本节省。

## 离线验收

- 实际调用冻结 registry/oracle 的正常路径通过；eval 的 near-threshold 与 short-interval 数据均由外部 Fraction oracle 检查。
- unknown tool、坏参数/schema、重复 call ID、native envelope 错形、重复 JSON key、NaN/Infinity、空 final、多 call 边界错误、一次预定 registry 执行异常、缺失/多余报告字段、oracle 拒绝、回合/工具上限耗尽均有精确终止/phase 事件；只有已分类的可恢复 handler 执行故障作为 tool error 回送 scripted model 并可继续到限额；参数/schema错误和未知异常终止，不吞成工具消息。用 fake clock 验证 client 接收剩余 timeout、边界 deadline 到期时停止；另验证同步 handler 耗时超限后只能在返回边界检测，不能声称抢占/硬时限。
- Architecture test 约束该路径不依赖 HarnessCandidateAdapter、Worker/Runtime/Fleet、`compile_worker_envelope`、Store/Redis；允许纯 ToolRegistry 与 oracle。
- 运行日志与最终结果按不同 run ID 保存，不可覆盖。为 scripted protocol 与 external oracle 结果写稳定哈希摘要。
- 使用项目 Python 环境跑定向测试和 Ruff。当前 Hatch wheel 只收 `src/aigineering`，examples 不进 wheel；树外验证需分别安装实际构建的 `aigineering` wheel，并从实际 sdist 解出冻结 examples bundle，再从这两个制品各自导入/校验模块路径与准确 bytes/hash。明确 direct runner/examples 并未进入 wheel，不改 `pyproject.toml` 或暗示它已安装。只报告实际执行命令和退出码；不把单测称为 provider 执行或完整质量评测。

## 公平性前提与 G 的未决门槛

F1b 只冻结并验证后续可用的对照输入、工具、oracle、原生 tool-call 消息形状、故障事件结构和 run manifest；不会声称两个执行机制已同预算或质量/成本可比。G 若要成对比较，至少需预注册相同模型/provider revision、参数、输入字节、A 工具/oracle 摘要、等价业务简报、每运行 cooperative deadline、最大模型回合、工具尝试、输出 token 上限、执行次序/重复数及故障日程。F1b 的可注入 client seam 不包含真实 HTTP/provider 实现或 key/env 读取；provider client 及其安全/timeout 行为须在 G 单独审计。Runtime allowance 是任务权限/工作限制，不等于 token budget；需同时报告其原始值与实际用量，不能假装单位可比。若 Runtime 和 direct loop 无法强制同等请求/时间上限，G 只能作为清楚标注的分臂预算研究，不能称公平同预算评测。

G 的共同故障日程可在首次 `normalize_calibration` 工具边界注入一次 transient fault；Runtime 按 F1a 实际 recovery 路径，direct loop 接收原生 tool error 并由模型继续/停止。两者恢复机制语义不同，必须逐轮记录 attempt/recovery/失败与是否完成，不声称相同。不得选择性重跑失败；外部 oracle 对两臂最终 report 使用同一规则。`passed` 代表 report 完整正确，`within_tolerance=false` 是可能的正确合成测量结论。

G 的真实运行须核对现有会话的 provider 测试授权，冻结有界请求/token/时间预算；只有超出既有范围才补授权，F1b 离线实现不扩大该范围。G 还需接入经审计的 provider client、指定 credential source（不写入 logs）、确认 privacy/retention 和可审计 usage 字段；若 usage 不可得必须保持 null、总成本不完整。公开 eval fixtures 不是保密留出集。离线 scripted fixture 不能填充真实模型、人工介入、质量或总成本数据；F1b 通过仅表示协议夹具准备就绪。

## 规划—审计与开发—验收记录

integration_review 对计划 SHA-256 `77ed2555787744c0811db7d22b880456416edfffd726d864ae1f01ed6e4f327c` 独立审计 Ready。明确 seam 只返回 str、限额只禁止下一次相应调用、deadline 返回边界拒绝过期结果。作者在隔离源副本实现，零真实 provider 调用。F1b 的离线通过不关闭 F1/F2/G。

首轮独立66项通过仍发现失败 lifecycle/metadata、JSON数值溢出和浅复制问题，作者修后由 integration_review 独立73项及原对抗向量通过，准确7文件摘要 `07b4f3e63facbe814d8da640351681197dabc43871548d2ae57d10c9a1af2762`。树外新回归暴露测试副本缺 package initializer，仅改该测试布置及 import 来源断言，非作者 Luna recheck_planning 独立1+73项通过；最终7文件摘要 `2b46ca0742c755d8669a6276f2c1d2a2abfcad1983d3d585168ee2525f401563`。原失败保留；root 与 F1c 同快照整合，最终资格见[组合记录](../../reports/111-direct-harness-protocol-development-2026-10-24.md)。
