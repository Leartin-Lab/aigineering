# G0a：离线请求预算与 usage 计量接口

遵循[工作流适配](../development-workflow-adapter.md)与[阶段计划](sustained-engineering-delivery.md)。

状态：隔离实现已独立通过，root 集成适用门禁及非作者总体验收通过，待 dev 交付；integration_review 审计快照 `0bec6a765d4cf0951cd339a553e76db2bbde435d1276b0336bb7db1c67034232` 通过。本包只定义一个可离线验证的 run-scoped meter，不接入 provider 或 Runtime。依据：工作流适配器 SHA-256 `ea61aa8a83b30a88ae08b799033c3a2248bc9f7323486382d506c263baf187a6`；阶段计划 SHA-256 `4667512fb400ed5f530358393e22a240631ef9a24b5354b4135be072851b628f`；只读 G0 接入盘点 SHA-256 `0299194b320ea27316a5ce788a1169c9726e1beda34f79ede7be094cc04ea50c`。盘点所据源快照为 `2d8d4b626d4640ce948c7790a08c3e349f480d3b`；实施前须重新冻结依赖代码和计划版本。

## 目标和限制

提供一个小型、纯内存、线程安全、单个执行进程内有效的请求许可与 usage 汇总接口，供未来 direct client 和 Runtime LLM transport 各自接入。它验证如何在并发 Worker 与物理重试下限制每次请求许可、预留输出上限、共享 cooperative deadline，并准确表达未知 usage。

G0a 不发 HTTP，不读 SDK、密钥、环境变量，不接入 LLMConfig、LLMWorker、Fleet、Runtime、F1a 或 F1b，不修改 F1b 的严格消息/wire schema，不加重试策略、持久表、Redis key、日志 sink、task/cache authority 或启动配置。`examples/` 不进 wheel；这个切片定义并测试可复用的最小语义接口，但不能声称已成为安装后的 Runtime 公共依赖。以后若要 Runtime import 该模块，需另审共享代码位置、打包和接线方案。

它不创建本地 JSON 账本，也不承诺跨进程或 restart 恢复。meter 新实例代表新 run；不能拿新实例的额度续接旧 run 并仍声称同一 run 在原预算内。恢复时持久化/复用 budget ledger 是独立计划。causal allowance 仍是工作权限，不可换算成请求数、token 数或费用。

## 唯一所有权与依赖

作者：Luna `recheck_planning` 提供初始模块与测试，Luna `recheck_tools` 完成测试与作者验证。仅新增：

- `examples/thermal_calibration/provider_meter.py`：冻结的纯 Python API 与本地实现；不改 direct loop、provider transport 或 Runtime。
- `tests/test_thermal_calibration_provider_meter.py`：typed API 行为、并发边界、未知计量和无外部依赖约束。

计划由非作者 `integration_review` 独立审计；root 未参与两份实现文件的编写，承担隔离实现验收与集成，准确集成结果仍由 `integration_review` 独立总体验收。D0 benchmark 保持优先，G0a 初始实现后交另一 Luna 完成，未占用 D0 的 Store 或矩阵验证资源。G0a 不依赖 D0 的结果，因为它只验证计量语义；G0a 也不能替代 G 的协议冻结或实际 arm 接线。

## 最小 API 与原子语义

首版固定以下 typed API，不留待实现时决定的字段：

```python
@dataclass(frozen=True)
class RunBudget:
    max_attempts: int                 # >=0；0 明确禁用所有请求
    max_output_tokens_per_request: int  # >0
    output_reservation_ceiling: int    # >=0；0 明确禁用所有请求
    deadline_seconds: float            # 有限且 >0

@dataclass(frozen=True)
class RequestPermit:
    permit_id: int
    attempt_number: int
    output_cap: int
    reserved_output_tokens: int
    timeout_seconds: float
    deadline_monotonic: float

@dataclass(frozen=True)
class UsageComponent:
    value: int | None
    status: Literal["reported", "pending", "not_returned", "missing", "invalid"]

@dataclass(frozen=True)
class AttemptRecord:
    permit_id: int
    outcome: Literal["pending", "response", "failed", "timeout"]
    prompt_tokens: UsageComponent
    completion_tokens: UsageComponent
    total_tokens: UsageComponent
    completed_monotonic: float | None
    late: bool | None
    result_eligible: bool | None
    inconsistent_total: bool | None
    output_cap_violation: bool | None
    cached_reasoning_status: Literal["unsupported"]

@dataclass(frozen=True)
class ComponentAggregate:
    known_subtotal: int
    unknown_count: int
    reported_count: int
    value: int | None

@dataclass(frozen=True)
class RunUsageSummary:
    admitted_attempts: int
    output_reserved: int
    pending_count: int
    prompt: ComponentAggregate
    completion: ComponentAggregate
    total: ComponentAggregate
    inconsistent_total_attempts: int
    cached_reasoning_status: Literal["unsupported"]
    attempts: tuple[AttemptRecord, ...]
```

`RunMeter(budget: RunBudget, clock: Callable[[], float] = time.monotonic)` 是唯一 mutable owner；`RunBudget` 和以上所有 permit/observation/record/summary value objects 都 frozen。meter 私有地持有 attempt 状态和唯一 permit 实例。唯一方法为 `acquire_request(*, output_cap: int, request_timeout_cap: float) -> RequestPermit`、`finish(permit: RequestPermit, *, outcome: Literal["response", "failed", "timeout"], usage: Mapping[str, object] | None) -> AttemptRecord`、`snapshot() -> RunUsageSummary`。所有方法以同一锁保护；`snapshot` 返回全新、不可变的一致快照，已完成记录不受后来 `finish` 影响。

typed 异常固定为 `InvalidRunBudget`、`InvalidRequestLimit`、`RequestAttemptBudgetExceeded`、`OutputReservationExceeded`、`RunDeadlineExpired`、`UnknownRequestPermit` 和 `PermitAlreadyFinalized`；它们都继承 `ProviderMeterError`。不捕获其它内部异常并改写为这些额度错误。

构造时 `max_attempts`、`output_reservation_ceiling` 为排除 `bool` 的非负整数；前者或后者为0即明确拒绝任何请求。`max_output_tokens_per_request` 和每次 `output_cap` 均为排除 `bool` 的正整数；每次 `request_timeout_cap` 与 `deadline_seconds` 均为排除 `bool` 的有限正数。类型/配置错误用 `InvalidRunBudget` 或 `InvalidRequestLimit` 拒绝，不静默转换。

`acquire_request` 是唯一准许新 HTTP attempt 的入口，未来 transport 必须在每次物理请求（包括 retry）前紧邻调用。它在同一锁内检查 clock `now >= deadline`、单请求 cap、attempt 剩余额与全 run 输出 reservation 余额，并一次性占用 attempt slot 与完整 output cap reservation 后返回 permit。相等 deadline 也拒绝新许可；相等 reservation 边界允许。并发争用到上限时恰有一个获准，其余分别得到 `RequestAttemptBudgetExceeded`、`OutputReservationExceeded` 或 `RunDeadlineExpired`；不能在锁外先查额度再竞争。permit 发放计入保守的 `admitted_attempts`，G0a 不观测 socket 是否实际 dispatch，因此不冒称精确 provider 请求数。

`RequestPermit` 是不可变值，但仅字段相等不足以授权 finish。meter 只接受自己发出的那个确切 Python object identity：伪造实例、另一 meter 的 permit 或 `copy.copy`/`deepcopy` 生成的不同对象一律抛 `UnknownRequestPermit`，不插入任何 attempt observation；对象 identity 校验和 `permit_id` 状态迁移在锁内完成。再次 finish 同一已 finalise permit 抛 `PermitAlreadyFinalized` 且不覆盖首个结果。所有记录只能由 meter 从收到的调用输入生成；meter 不验证 provider 真伪，不能把它描述为签名或独立可信用量。

每个 permit 预留完整 `output_cap`，且该值不得超过单请求 cap。预留判定采用 `reserved + output_cap <= output_reservation_ceiling`，边界相等接受、超出拒绝。预留按 permit 永不退还，包括连接错误、timeout、失败响应或 unknown usage；它是最坏输出容量预算，不是已消费 token 或实际 usage。报告必须将 `output_reserved` 与 reported usage 分开。meter 不限制 input tokens，也不能宣称在请求前硬性限制 tokenized prompt 大小；provider 实际 completion usage 若超过请求 cap，保留原始值并将该 attempt 作为 visible quota violation，不可截断或掩盖。

meter 创建时记录 `deadline_monotonic = clock() + deadline_seconds`。permit 的 `timeout_seconds = min(request_timeout_cap, deadline_monotonic - now)`。`finish` 取 meter clock 的完成时刻；`completed_monotonic >= deadline_monotonic` 必标 `late=true`，严格早于 deadline 标 `late=false`。`result_eligible=true` 仅当 outcome 是 `response` 且严格早于 deadline；failed/timeout 永远 false。尚未 finish 的记录在快照中 `completed_monotonic`、`late`、`result_eligible` 均为 null。deadline 不抢占同步 transport/tool；result_eligible 仅是计量/执行边界信息，不决定 Candidate、task 或业务结果接受。

`finish` 的 `outcome` 和 `usage` 相互独立：response、failed、timeout 三种结果都可携带有效 usage，只要调用方确实观察到了 provider usage；meter 一律保留这些合法 component，即使 outcome 是失败或该响应迟到。没有可观察 response usage 时传 `None`，三个 component 标成 `not_returned`。合法错误响应里已观察到的有效 usage 不能因 `outcome="failed"` 被抹掉。`output_cap_violation` 在 completion usage 已知时为 `completion_tokens > permit.output_cap` 的精确布尔值，未知时为 null；超 cap 不修改 reported value。尚未返回的请求在 `snapshot` 里计 pending、usage unknown；`attempts` tuple 内对应一条 `outcome="pending"` 且三个 usage component 均 pending 的 AttemptRecord。snapshot 不 finalise permit，后续 finish 可出现在新快照中，旧快照保持不变。

`finish` 在锁内先验证 permit identity 和已完成状态，再验证 outcome。outcome 只接受三种字符串枚举；包括 dict/list/None 在内的其他输入抛 `InvalidRequestLimit`，不调用 clock、不读取 usage、不 finalize。整体 usage 为 None 时三个 component 都为 `not_returned`；为空 Mapping 时缺字段为 `missing`；整体不是 Mapping/None（如 list/str/bool）时三个 component 都为 `invalid` 且值 None。Mapping 中已存在但值为 None 的字段为 `invalid`，不是 missing；每个已知字段独立解析，扩展字段忽略。

`finish` 必须先在局部生成完整不可变 AttemptRecord，再一次提交状态。自定义 Mapping 的 get/读取行为或 clock 抛未知异常时，原异常传播，permit 仍 pending、已 reserved 额度和旧 snapshot 完全不变；不得吞成 invalid 或留下半完成记录。正常 budget 输入校验只处理明确定义的类型/值错误。

`result_eligible` 仅表达 response 且严格早于 run deadline，并不代表 usage 完整、total 一致、未超输出 cap、额度资格通过或业务可接受。下游须分别读取 usage/violation 字段；G0a 不赋予这些判断任何 task 权威。测试须包含 result_eligible=true 同时 inconsistent_total 或 output_cap_violation=true 的组合，防止口径合并。

## Usage 结构和汇总规则

首版仅解析并保存 provider 报告的 `prompt_tokens`、`completion_tokens`、`total_tokens` 三个独立 component。每个 component 显式标记 `reported`、`pending`、`not_returned`、`missing` 或 `invalid`；只有 `reported` 可有值，且值只能是非负 JSON/Python 整数（排除 bool），其它状态的 `value` 必为 null。不得将 missing、负值、bool、float、字符串、部分结构或 timeout 转成零。`total_tokens` 按 provider 原样记录，不以 prompt+completion 重算，也不因二者相加相同而推断其可靠。首版不支持 `cached_tokens`、`reasoning_tokens` 等扩展字段，summary 明确给这些维度 `unsupported`，不纳入三项汇总。

对每个 component，summary 同时报已知 reported subtotal、unknown attempt count、reported count，以及 aggregate value。只要该 component 在任一 admitted attempt 上 pending、not_returned、missing 或 invalid，aggregate value 就是 null；已知 subtotal 仍保留，不能把未知变零。prompt/completion/total 分别计算，不能因一个字段未知而抹掉另两个字段的已知值。若同一 attempt 三个 component 均 reported，且 `total_tokens != prompt_tokens + completion_tokens`，AttemptRecord 保留三个原值并置 `inconsistent_total=true`，summary 增加 `inconsistent_total_attempts`；不得改写 total 或默默声称该报告一致。若任一相关 component unknown，则 `inconsistent_total=null`，不猜测。completed late response 若带合法 usage 仍计入观测总数/总和并单列；budget qualification 必须另看 run/deadline 状态。任何 usage/parser 内部未知程序错误不能吞并成 `invalid`；只有明确定义的输入形状/值校验失败转为该字段的 invalid 状态。

如果未来 sink 写入失败，调用方不得静默重发 HTTP 或伪造 zero usage。G0a 本身没有 sink/I/O；meter 已获准的 attempt 和内存中已观测的 usage 留在当前进程状态，调用方必须把 sink 错误按真实失败上报。跨进程可靠 ledger 不属于本包。

## 行为与隔离验收

测试只调用新 meter 与 fake clock，不发网络、provider 或 Redis 请求，不访问 Store/Runtime。最低覆盖：

1. 两个线程同时争一个剩余 permit：一个获准、一个 typed 拒绝；预算和 reservation 总数不超限。
2. request count、每请求 output cap、aggregate reservation 的刚好等于边界可用，超一即拒绝；失败、timeout、unknown 不返还已预留 capacity；再次许可不能越界。
3. 请求 timeout 是 per-request cap 与 remaining cooperative deadline 的较小值；deadline 后拒绝新 permit；fake clock 推进后晚响应保留合法 usage、标 late 且 `result_eligible=false`。
4. 对 prompt/completion/total 分别验证有效零与正整数、missing、负数、`bool`、float、错误类型；零只有 provider 明确报告时才是 known zero。聚合同时断言 subtotal、unknown count 与 null/non-null；不重算 provider total。
5. 无 response usage 的失败/timeout 及未结束 pending 均为 unknown 而非零；晚 response 仍保存准确 usage；每个 permit 第二次 finalize 被拒且第一次观测不变。
6. 首版 cached/reasoning usage 明确 unsupported；over-cap response usage 保留原报告并产生可见 violation，绝不把 reservation 误写成真实消费。
7. 不同 RunMeter 对象互不共享状态；新实例是新 run，不自动恢复旧 run 预算。该测试只验证内存作用域，不能证明重启恢复或 durable enforcement。
8. 架构断言该模块无 HTTP/SDK、环境变量、credential、文件、Store、Redis、Runtime、日志或 provider import/调用。不开直接 Harness message schema 的宽容口子。

用小型线程/fake-clock 参数化单测即可；无需安装依赖、真实 provider、Redis、性能基准或全套测试。保留首次失败命令、stdout/stderr 与退出码，不以重复绿灯抹掉。实现验收绑定确切源/测试 hash，独立 reviewer 复核 typed boundary 和未知 usage 账本语义。

## 非目标和后续接入条件

G0a 通过最多说明 meter 的并发许可、保守输出容量 reservation、cooperative deadline 与 usage unknown 表达自身正确。它不说明现有 direct arm 或 Runtime 实际调用 meter，更不说明两臂有相同 request/token/time 上限。当前 LLMWorker 的 internal retry、task 级 Candidate usage、F1b 的 offline-only strict response envelope，以及 causal allowance 均不因 G0a 改变。只有后续经 Ready 的 direct transport 与 Runtime transport 接入包逐个物理 HTTP attempt 获取同一 meter 语义，并通过真实配置路径的组合行为验证后，才可声称限额被实施；真实 provider 公平对照仍须单独冻结两臂参数、usage 来源、预算和失败协议。

## 实施与独立验收记录

计划独立 Ready 快照见开头。实施以 `ec5f1224cb253e6395d689b2708d9f9c3b1167aa` 隔离源为基线，不改其已有文件。模块 SHA-256 `1e4139b83ad7a2774b746565bc510dbdb0120eecefae1a43837c107fd3a863f0`，测试 `561a62cb3657b04b4364969df39be380b4d83d65fe5a029ff26c599d0241d1da`。作者最终49项与 Ruff 检查通过；缺模块、格式和测试参数错误的早期失败均保留，不混作最终通过。

root 对上述两文件独立验收 Passed：49项通过，另验证20线程竞争4许可、失败响应的已知 usage、超 cap/total 不一致、timeout/pending 的 null 汇总、deadline 相等迟到、旧快照不可变、复制 permit 拒绝及未知 Mapping 错误下原子保持 pending。该结论只覆盖离线模块，不替代 root 自身集成的独立总体验收。

root 集成同一两文件源摘要：完整源码测试3,257项通过、0 skipped、6项既有依赖警告；Ruff check 与566文件 format check通过。显式公开文件集合构建成功，235个 wheel 模块逐字节匹配源码且 wheel 与前一增量相同；新 example 由真实 sdist 提取，树外安装包及 example 来源均核对，49项通过。第一次树外测试把 test 放在错误目录，静态源码检查48过1失败；调整私有验证目录以保留 `tests/` 相对布局后49项通过，未修改公开源码或测试，原失败保留。

本次无 provider、HTTP 或真实模型请求；完整套件包含隔离 Redis 的既有运行边界检查。轮换密钥、两臂接线、公平预算、持续业务评测与持久重启预算不因本包完成。准确公开集合的文档治理49项、59条相对链接及根指南29条链接/68个路径检查通过，新增公开材料无私有路径或凭据。

非作者 integration_review 对9个变动文件摘要 `2727f3cfa0e8d1412f4de03e9f8fb32b2179cd8b2115890637dc8c1e05dbbe61`、1000文件公开集合摘要 `bbbb48ea0e0b353db0a1fcf1027dc1813fd074e635c887649bf94c726fdb6f74` 独立集成验收 Passed：核对相对 `ec5f122` 的准确范围、235个 runtime 模块不变、678个源码包文件逐字节一致及真实源码包/安装来源；独立98项（49计量+49治理）和树外49项通过。另验部分 Mapping 第三字段抛错的原子性，以及8线程并发结束同一许可只有1次成功。完整3,257项是 root 的集成证据，未误写为独立全套。该结论不关闭 C/D/E/F2/G 的阶段目标；最后仅更新本验收元数据，并重建源码包、复核文档与准确摘要后交付。
