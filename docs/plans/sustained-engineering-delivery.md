# 持续工程分析交付与运行成本计划

状态：A/B/C0 与 F1a 开发增量独立集成验收通过；C1/C2 隔离开发中，其余包待细化。阶段未完成。日期：2026-10-24。

## 目标与用户行为

用户确认本阶段目标：让一种有用的工程分析在固定条件下反复交付，证明质量、实际恢复和总成本值得使用，并且同一项目不能因无关历史不断积累而明显变慢。先用合成传感器校准漂移审核建立可复算的业务与验收方法；它不是实际生产数据或行业校准合规证明。代表性和实际收益仍由后续真实任务评测验证。

Runtime 臂须经 CLI 发布冻结输入、普通动态规划、工具观察与续接、报告、不同 actor 的精确独立接受及合格导出。直接 Harness 臂真正绕过 Aigineering 的任务调度和 commitment；HarnessCandidateAdapter 是受管接入方式，不能冒充这个基线。两臂由同一个外部 oracle 判断业务结果。

分别测新工作区与同一长期工作区。记录合格交付率、错误接受率、实际恢复率、人工介入次数和主动分钟、耗时与总成本；失败保留，不能重跑删失。Provider usage、工具费用、本地开销和人工劳动分开记；未知不计作零。共同外部验收单列，两臂都计入；Runtime 内部 attestation 另计并纳入其总成本。

## 范围、基线与适配

基线 `dev` / `e1ead6e980d510d485753a3041058cb5ed86a1eb`。遵循[仓库工作流适配](../development-workflow-adapter.md)、[DESIGN](../../DESIGN.md)、[边界不变量](../boundary-invariants.md)及[贡献指南](../../CONTRIBUTING.md)。上游 development-workflow 版本 `c626e75`，模板来源经适配记录，不依赖仓库外相对链接。

保持签名事实唯一权威、纯语义 owners、writer 内重验和原子写入、双 Engine 竞争安全、严格重建一致性、Redis 默认必需。数据库可替换性是语义 Store 接口和适配边界约束；不在本阶段实现多个数据库后端或分布式平台。不预设新缓存表，不增加第二调度器、手写业务 DAG 或 mutable task state。ADR 记录实际作出的持久决定，change 记录完成变化，均不替代本计划。

已确认的逐项发布历史重扫是本阶段缺陷。旧 SQLite/CandidatePublisher 测量显示：64 个任务累计物化 37,504 条 runtime rows，256 个同父任务累计 592,384 条；B 将其变成可复现项目证据。它不是 CLI、安装容量或 provider 成功率证明。

复杂度分开计：H 是无关已闭合历史，P 是当前显式工作量，A 是当前相关活跃集合；实际祖先证明、引用数量与级联规模单列。允许索引随 H 的对数开销；冷构建、审计和完整重建可 O(H)。正常每次发布、Asset 传播、pull 和 maintenance 不得重新扫描或物化无关 H。不能以一次 SQL SUM 掩盖同父逐项发布的累计 P²，也不能把必要祖先成本与全域历史重扫混为一谈。

## 预声明工作包与真实依赖

| 包 | 交付 / 文件所有权 | 计划、开工与集成依赖 | 可观察验收 |
| --- | --- | --- | --- |
| A 业务规则、工具与外部 oracle | Luna recheck_tools；`examples/thermal_calibration/` 和 `tests/test_thermal_calibration_oracle.py`。首包只做纯工具、固定数据/manifests、oracle、README。 | 计划依赖：下述 A 契约；开工：独立审计 Ready；集成：F1 消费固定版本，不能重写 oracle。 | 工具和独立 oracle 对全部 golden/invalid 向量一致；手算第三向量核对公式；畸形数据和篡改报告拒绝。本包不宣称已有 CLI 或真实业务资格。 |
| B 签名发布性能基线 | Luna recheck_planning；`scripts/benchmark_planning_queries.py`、`tests/test_benchmark_planning_queries.py`。 | 计划依赖：下述 B 矩阵；开工：独立审计 Ready；集成：C 使用结果冻结优化门槛。 | batch、逐项同父和 chain 全部经过公开编译及签名发布；H 已闭合且测量不含冷构建；脚本、计数、SQL 计划和失败可追溯。 |
| C0 提交去重精确索引 | root；`core/sqlite_schema.py`、`tests/test_sqlite_candidate_lookup.py`。 | 计划依赖：既有精确查询与下述 C0 方案；开工：非作者独立审计；可与 A/B 并行，无接口依赖。 | Candidate ID 查询使用精确索引，签名成功/拒绝/跨连接重放语义不变。仅关闭一个 SQL 热点，不宣称 C 已完成。 |
| C 声明与 allowance 局部化 | 待细化；projection context、causal allowance、SQLite/query owners 及对应测试。 | 计划依赖 B 红灯及具体语义接口；开工前独立审计算法、writer/rebuild/兼容方案和增长阈值；集成需竞争、重放和严格重建通过。 | 固定载荷/引用的 P 个平级或嵌套声明不因重复历史查询退化 P²；预算不双花，拒绝和事实身份保持。 |
| D Asset 传播与精确输入局部化 | 待细化；`core/fact_reducer.py`、披露/context owners 及对应测试。 | 先测真实热点；仅依赖实际需要的窄 Store 接口。共享 SQLite 文件由协调者串行集成，不强制依赖 C allowance 内部算法。 | Asset 先于任务/任务先于 Asset、同批 overlay、完成/取消级联和外层独立验收分支不变；无关历史不反复物化；必要级联成本单列。 |
| E 发现、claim 与维护局部化 | 待细化；`runtime.py`、runtime services、task query 和原 claim owners。 | 先测 pull/expiry/failure/rejection/completion 热点；具体查询接口审计后开工，共享文件有单一集成人。 | 候选集、排序与 blocker 保持；submitted 独立接受、待恢复义务和到期 claim 不漏；SQLite writer 裁决，Redis/view 不授予权利。 |
| F1 业务 CLI 与直接 Harness 对照可运行性 | 待细化；配置、Skill、普通 Worker 夹具、CLI/对照脚本。 | 只依赖 A 和公平对照协议审计，可与 C/D/E 并行；不等待性能修复。 | 普通 CLI 动态规划与两工具续接、不同身份精确接受、导出/重开；正常、畸形输出、缺证据、故障、额度耗尽和实际恢复均可观察。两臂共同 oracle，不以预制 DAG 伪造动态计划。 |
| F2 固定候选制品的长期组合评测 | 待细化；源树外安装和隔离运行 evidence。 | A/B/C/D/E/F1 的实际组合冻结，独立协议及各包验收通过。 | 同一 P/A 下改变 H，完整 signed 生命周期持续累积；重开/竞争/重建语义一致；固定制品的质量、恢复、全成本与热路径增长曲线可复核。 |
| G 真实 provider 成对评测 | 待细化；固定模型、工具、输入、预算、故障日程与外部判分。 | F1 与公平协议通过后可先做 baseline；新的有效 provider/付费授权是开工依赖。最终候选另行冻结并重测，不重标签旧证据。 | 保留每次失败及实际 usage，区分 fixture、真实模型和安装制品；没有证据不宣称质量优势或稳定资格。 |

A/B 可并行。C/D/E/F1 尚无具体方案，不能因目标已同意而跳过各包 Ready；它们的规划、基线和开发可按真实依赖交错推进。开发增量独立验收及适用检查通过后可进入 dev；main、标签和发布另行处理。

## A 的开工契约

固定合成规则 `synthetic-temperature-calibration-v1`，不引用真实计量标准。输入为 JSON：`case_id`、`sensor_id`、`samples`；2–256 行，各行 `sample_id` 唯一，`time_utc` 为秒精度 `YYYY-MM-DDTHH:MM:SSZ` 严格递增，`reference_C`/`reading` 为有限十进制字符串（最多12位整数和6位小数，不接受科学计数、bool 或 JSON number），`unit` 为 C/F/K。标准另为固定版本和非负 `max_abs_error_C`。未知字段、版本、缺行、缺必需值、未知单位、重复/倒序时间均拒绝。

计算使用未舍入值：`reading_C=C` 或 `(F-32)*5/9` 或 `K-273.15`；`bias_C=reading_C-reference_C`；`drift_C_per_hour=(last_bias-first_bias)*3600/elapsed_seconds`（末减首，正号表示读数相对参考升高）；`max_abs_error_C=max(abs(bias_C))`；`within_tolerance` 按未舍入最大误差 `<=` 冻结阈值，不能以显示舍入决定边界。

报告版本固定，包含 case/sensor、源数据和标准 canonical JSON SHA-256、按源顺序的全部 sample_id/time/reference_C/reading_C/bias_C、drift/max error/within_tolerance。每行的 evidence 引用源 hash、sample_id、字段角色；聚合 evidence 包含全部样本引用和标准 hash。具体 JSON 键名在 A README 固定，额外/遗漏字段拒绝。显示数值统一6位小数、ROUND_HALF_EVEN、负零规范为正零；以 canonical 字符串逐字段核对。工具以统一的整数定点分子保留精确单位转换和差分，最后一步才作显示舍入；oracle 用独立 Fraction 计算及独立舍入，不调用工具计算函数。中间把华氏除9提前近似为 Decimal 会使精确 HALF_EVEN 中点偏移，不能以简单增大精度修复。共同 JSON hash 编码协议明确，业务数值算法不共享。后续 CLI 另将这些字节/hash 绑定 exact committed Asset IDs；hash 本身不证明签名或权威。

两个纯工具为 normalize_calibration 与 analyze_calibration，显式 ToolSpec 限界输入/输出和字节；分析工具验证归一结果完整性，不能接受缺行/改读数的中间结果。工具不能读私有文件、访问 provider、写任务或接受报告。Oracle 从原始输入和标准重新计算，不信任工具输出、报告自报通过或相同模型评价。

至少准备 C/F/K 与阈值边界手算向量、正/负漂移、间隔不整小时、近阈值未舍入判定、缺配对、非有限/超长数字、重复 ID/时间、倒序、未知版本/单位、报告篡改/漏行/换源/错证据。train/eval manifests 固定 case IDs 与内容 hash；eval 仅相对方法调试留出，公开仓库不保证保密，不能宣称盲评。A 的确定性门槛为100%预声明向量判定一致。

## B 的开工矩阵与计数

矩阵预声明：P=8/16/32/64（可扩128/256），H=0/16/64，三种模式 one_batch/siblings_sequential/chain，每 cell 至少3个隔离样本。smoke 可用 P=2、H=0/2。每个 case 独立临时 SQLite Store、Ed25519 身份；H 由正常签名 Contract/Asset 生命周期闭合，验证对应 terminal，不能以待执行 roots 增长混淆 A。冷建域/造历史单独计时。一次 cell 连续发布 P 项不重开 Store；当前引用/输入/keys/payload 保持有界，并记录实际最大深度、边数与未闭合数。B 不测可 claim 数，未闭合不等于 ready；真实 eligibility 与领取集合对比由 E 单列。

模式用公开 compile_planning_blueprint，再经 CandidatePublisher 签名提交；同父预算逐次减去已预留量，chain 逐层传递同一有界 grant，不能借基准测试增额。报告每次接受/拒绝及最终事实/Contract/Asset 数量。

测量 schema 包含 driver checkout revision、实际导入包的完整 Python 文件清单 digest（导入源码 revision 未独立验证则 null）和脚本 digest、package/Python/SQLite 版本、结构化参数、退出码/失败分类、P/H/A、构建时长、每阶段 wall time 样本/median/p95。另一次独立 instrumentation pass 记录 get_all_contracts/get_all_assets/scan_runtime_records 的调用和实际返回物化数、SQLite SELECT 次数、代表性查询 EXPLAIN、progress-handler callback 次数、interval 和二者乘积的 VM 工作量代理（不是整次运行的精确数或上下界，短 SQL 的未满 interval 残余可能不触发 callback）、Python profile 调用数与 tracemalloc peak；无 instrumentation 的计时与 instrumented 计时分开。VM opcode 是数据库工作量代理，不冒称精确扫描行；Python 返回行和 profile 也不冒充全部内部迭代。

输出文件独占创建，不覆盖已有成功/失败；失败仍落可解析元数据及非零退出码，完整 stderr 留私有 evidence。公共报告不含凭据、私有路径、原始 Store 或任意异常正文。脚本 smoke 要验证矩阵覆盖、signed 成功、H 闭合与计数结构；不是把已知二次缺陷永久写成期望行为。B 完成后、优化实现前，独立冻结 C/D/E 的增长阈值，不能看优化结果后调门槛。

## C0 的局部修复方案

现有 `_candidate_already_committed` 在 writer 内按 `candidate.committed` 和 payload 中的精确 Candidate ID 查询存在性；当前只有 record_type/revision 索引，必须逐个检查旧 committed heads。给同一事实表增加非唯一 partial expression index：键为 `json_extract(payload_json, '$.candidate_id')`，条件为 `record_type='candidate.committed'`。保留原查询、签名认证、事实、事务和唯一性规则，不引入新表或可变授权摘要。

SQLite 自动随原事务维护索引，回滚不能留下额外权威；旧写者也由 SQLite 更新索引。现行 writable schema 初始化负责为旧 Store 建立缺失索引，冷建索引的全历史扫描及排序、构建成本单列，不要求新 reader floor；只读旧 Store 不自动建索引。验证先用真实签名积累至少64个 heads，比较无索引与新增索引的缺失键 VM 工作量及 EXPLAIN（VM 是工作量代理）；精确索引查找应至少减少四分之三 VM 指令，不以整体发布耗时声称此局部修复效果。验证已提交、未提交/被拒、空 ID、跨连接 exact replay、移除索引后重开重建索引且事实 ID 集不变。适用既有 authentication rejection、commitment、SQLite reopen 与 governance 检查，加全套门禁。仅机制优化，没有新的边界规则；原边界 architecture 约束继续适用。

## C1 的声明投影上下文局部化方案

本切片只优化实际 effects 全为内置 `contract.declare` 的纯投影输入选择，不修改 writer allowance 校验。作者 Luna recheck_planning，root 集成；独立审计和验收 integration_review。只依赖 B 的已确认重扫，不依赖 F1a；共享文件无其他在制作者。预计文件为 `core/projection_context.py`、最小的 `core/commitment.py` 调用适配、Store port/内存实现、SQLite Store/index，以及定向测试；不修改 effect projectors 的规则、writer allowance owners 或 F1a 文件。

Core 根据现有 canonical parser 构造不可变 typed selection request：incoming Contract IDs、direct parent IDs、claim-bound parent ID、实际被输入校验使用的 Asset IDs、allowance source IDs、Candidate ID 与是否需单 effect replay 选择。可选的语义 Store reader 只返回精确既存 Contracts、Assets 和相关 runtime records；SQL 不解析 Contract 或重写规则。内存 Store 提供等价 reader；缺此能力的旧/custom Store 保留 full-context 路径，不增加强制协议门槛。仅其他实际 effect、未知版本或无法安全构造请求时走原路径；注册但本次未执行的 injected handler 不应使纯声明退回全量。

窄预解析只是 read hint，不能提前认证或形成新的 rejection。加载 context 先于 `reduce_candidate`：已知 canonical parser 域错误须明确返回无请求、加载原 full context，再由原 reducer 形成同一认证、拒绝与 audit/trace。只捕获已确定的 parser 域异常，未知编程错误传播。引用不存在由原 projector 拒绝，不能把部分读取当完整；不因未知错误静默扩大查询。

选择范围为：所有 incoming IDs 的既存 Contract（保持 replay 不重复 grant）、直接父项和 claim-bound 父项；不能把未提交声明提前放进 `existing_contracts`。原投影再合并全部 pending declarations，保持父子顺序无关。Asset 仅选 `input_asset_bindings` 中 role 属于 `inputs` 的 IDs，以及 task:v4/v5/v6 的 `context_asset_ids`；未使用 role 不新增语义。原 owner 继续核 ID/name/tombstone/label。

Allowance 输入仍为相关 source 的完整 `granted.contract_id`、`reserved.source_contract_id`、`returned.source_contract_id`、`returned.from_contract_id`、`extinguished.contract_id` 记录。精确 replay 读取全部匹配 receipt/committed heads，并按每个 head 的完整成员列表取回成员；保留重复匹配以供原 uniqueness 校验拒绝，不能只取声明或 grant。纯声明没有 Asset graph assertions；混合或 injected effects 保持原完整 context。SQLite 增加相应关系的 partial expression indexes，既有 record ID/Candidate committed 索引继续复用；无新表、余额摘要或缓存权威。

先保留旧实现红灯，再对 root/child、同批逆序父子、siblings/chain、exact inputs/context、缺失/改名/tombstone、parent bounds、replay 完整/缺失/重复/错序/改成员、mixed/injected/custom Store fallback、malformed/forged Candidate 的 audit 与 claim 不变性作 full-context differential。接受/拒绝及事实身份保持，writer 内原子余额/claim/key 重验不变；reopen/strict rebuild 摘要一致。新增架构约束保证 projectors 不读取 Store、SQL 留在适配器、窄路径不绕过原认证。

增长门槛预先固定：保持显式 references、相关 allowance facts 和 replay 成员数不变，在真实签名闭合 H=0/16/64 下，窄 context 返回及物化对象总数与各关系数必须精确不变。查询应使用索引；H=64 的缺失键选择 VM 工作量相对无索引至少减少四分之三。VM 为独立工作量代理，不是精确扫描行；分别报告 API/SQL 次数、返回行与 VM，不以局部计数声称端到端线性。

C1 不关闭同父逐项 allowance 历史增长、writer 全量校验或 publication proof 的 P²；这些仍是 C 的后续必修缺陷。冷索引构建、真正引用历史及完整 replay proof 成本单列。实现后先定向行为/架构复验和非作者审查，再跑适用完整门禁；Ready 前不开工。

## C2 的单次 allowance 证明索引方案

本方案已独立 Ready，独立于 C1 开工。作者 Luna recheck_tools，root 集成，独立审计/验收 integration_review；生产文件仅 `core/allowance_returns.py`，测试沿用 causal allowance returns/funding conservation owners，必要时补既有 architecture ownership 约束。不修改 C1 的 context/commitment/Store/SQLite 文件，C1 不是开工依赖。依赖 B 已证实的单 Candidate 批量 P² 与以下明确门槛。

当前每个 child grant 都重新扫描全部 records、reservations、声明和头成员；共享大 head 与 receipt 的完整 hash/结构也可能被 P 次重验。拟在 `validate_allowance_grants` 单次调用内构造一个不可变、多值关系索引：按成员找全部 committed heads，按 Candidate/Contract 找全部 declarations，按 child/source/reservation ID 找全部新增 reservations，以及既存 grant 的 Contract 集合。匹配须保留真实多项歧义，不能选首项或最后覆盖。查询重叠只去掉同一个原事实的重复匹配，不改变既有 existing/pending overlay。

复用同一语义 owner/helper，允许传入私有、仅本次调用存活的 proof context；不复制第二套规则。每事实仍核唯一 head、共同 head、精确值/完整 Contract、direct receipt parent 和声明成员关系。每唯一 head 的完整 `validate_runtime_record` envelope/hash、结构及 receipt 证明成功后只验一次；每唯一 receipt 的完整 envelope/hash亦只验一次（effect_types 也可长达 P）。每 head 一次构建安全成员/causal-parent membership sets，后续原 membership 谓词不再逐 child 扫大 tuple/list。仅缓存该不可变输入内已完成的证明，不跨调用缓存、不引入表、持久摘要或新的权威。

索引构造不能丢弃畸形但可匹配的 head/declaration/reservation。安全容器之外的 opaque 历史保留原匹配/拒绝路径，匹配后的 schema/值/receipt/head 闭包仍由原 helper 裁决；域异常类型与稳定错误归属保持，不吞未知错误。返回/资金 ancestry 的既有无 context 调用保持同一规则。Writer 事务、认证、事实身份、幂等和 rebuild 均不改变。

先从真实签名 one-batch 声明构造 P=8/16/32/64 的完整 receipt/head/declaration/reservation/grant 向量并保留旧计数红灯。对缺配对、重复 reservation/declaration/head、金额或 source/purpose/ID 错误、缺/错/重复 receipt、错 Contract、跨 head 分裂、重复 member/closure 错误及 malformed 容器做原行为差分；feature 测试须保持 generic proof 有效且证明到达目标 fence。既有 returns、root retry conservation、writer 竞争、replay 和重建组合继续通过。

量化门槛：每次关系索引只扫描传入记录集合常数次；每 head 原成员和父 tuple/list只作常数次完整遍历；选中唯一 head 的 envelope/hash 和完整结构、每唯一共享 receipt 的 envelope/hash至多各一次。每 child 查询自身多值候选，不重扫全 reservation/record 集。以 test-side 包装计数器和可计数容器核实际成员访问、canonical validation 次数及候选数量，静态复核循环；不能只看 `_validate_candidate_head` 名称调用数而遗漏其前后 hash 或 membership 的 P²。分开记录必要 M（全部输入 head 成员数）、P 和实际验证字节数 V（包含 receipt raw_output 等），证明固定载荷下 O(H+M+P)、一般情况下另计 V 的循环结构，再用上述向量核对；计时/profile辅助，不把有限点拟合当证明，不添加参与裁决的生产 observer。

C2 仍会在每次 publication 建立临时索引；它不消除 H 全量输入、同父逐项余额历史或 writer 每次重扫的累计 P²。C 总目标继续开放。无需新 ADR；作者交付准确变更/验证说明，由独立验收决定性能证据是否足够公开，root 不把局部通过写成整体线性。

## F1 的首个可运行切片 F1a

F1a 只依赖 A，先接普通 CLI，再做直接 Harness 对照和额外故障测量；不等待性能修复。作者 Luna recheck_tools；独立计划审计/实现验收 integration_review。只新增 `examples/thermal_calibration/{CLI.md,SKILL.md,skill.toml,run.toml,workers.toml,task-description.txt,workers.py}`、`tests/fixtures/thermal_calibration_workers.py`、`tests/test_thermal_calibration_cli.py`；不修改 A 已冻结的工具、oracle、输入、README 或 runtime owners。root 负责导航和集成。

配置仅发布 A 的 train/mix-three-point case/standard 两份输入、一个预算32的根 Contract、Skill、Planner/Researcher/ToolWorker 与不同 actor 的确定性 verifier。输入名 `calibration_case`/`calibration_standard`，根输出 `calibration_report`；独立政策要求 `calibration.verify`，冻结两份输入作为接受证据。Planner/Researcher 使用显式 deepseek-flash LLM 配置，verifier 是普通有授权 LocalWorker；默认不使用 MockWorker。生产 Skill 让模型自行拆解普通任务，不提供外部业务 DAG。固定测试 Worker 的 blueprint 仅是显式协议夹具，不冒充真实模型规划。

Researcher 经 normalize_calibration→analyze_calibration 两次普通工具观察和 continuation，发布原样领域 report（A 的严格 schema）及普通 `calibration_evidence`。后者准确绑定两份输入 Asset IDs 和两个不同观察 ID，包含完整观察内容。Verifier 仅消费已披露的 case/standard/report/evidence，通过未修改的独立 Fraction oracle 验报告、核精确输入 IDs 与完整双观察内容一致后，返回普通 `/attest`，目标根 ID/输出/report Asset ID 与冻结两份证据明确绑定。额外字段、缺观察或不一致结果均拒绝；receipt 不能替代 canonical qualification。工具观察的历史存在性由只读运行审计核实，复制的观察内容和自报 ID 本身不证明真实历史，verifier 不读取未披露 Store 或夸大这项保证。正确的 outside-tolerance 报告仍可合格交付。

本切片测试限定三条 CLI 行为：正常动态普通任务闭环；测试专用 registry wrapper 的首次工具故障后实际发布、领取并执行新恢复工作；缺少 verifier 时不可报成功或合格导出，补充同身份配置后只 resume 原义务。故障 marker 仅模拟后端状态，不决定 task readiness/恢复/接受，生产纯工具不变。检查实际 claim/工具观察/attestation actor 分离及精确输入绑定、根终态和导出字节；重开后导出一致，私有副本 strict rebuild 语义摘要一致且原 Store 不变。运行依赖 Redis，各测试使用独立临时工作区；带同一 Redis 测试库的检查串行，完整签名事实才证明完成，只读 audit 不写完成。

F1a 不含直接 Harness 臂、真实 provider、成本或质量优势测量，不关闭畸形 provider 输出、额度耗尽等 F1 剩余组合覆盖；这些义务保留在 F1/F2。先经独立 Ready 再实施，完成后与 A/B/C0 同一准确快照独立集成验收。

## 规划—审计记录

初稿作者 Luna recheck_planning；协调者根据非作者预审修订契约、角色、真实依赖和公开链接。A 作者 Luna recheck_tools，B 作者 Luna recheck_planning；独立计划审计与实现验收均为未参与作者的 integration_review；root 负责集成和路线/状态文档。

A/B：integration_review 对计划 SHA-256 `b2fb7b65e6ec60b868eba6d9813062adfdc508c97568c987386db0873ca2003c` 独立审计通过，2026-10-24 标 Ready，按上述所有权并行开工。审计者未参与计划或实现。C/D/E/F1余项/F2/G：待详细方案审计。F1a 按下述新增审计记录单列。用户开发授权已具备；Ready 是内部流程开工条件，不是重新向用户索取开发许可。真实 provider 的新授权独立于离线工作，不阻断 A/B 等已授权开发。

F1a：integration_review 对计划 `7072de22b8b6b821ebb52ba02b02d2873c8c12e8eaecac409805514e59a2f62d` 独立审计 Ready；为隔离首批交付快照，在 A/B/C0 交付后开工。不包含直接 Harness 和真实 provider。采纳字段大小写笔误修正，不改变 A 接口。

C1：integration_review 对公开计划 `0ab88392e9d22818e9de9e447cdf0fddf41164f57efe717860f18751650687e7` 独立审计 Ready。关系选择需去除查询重叠并保持原 revision 顺序，不能吞掉实际重复 receipt/head；大请求分块。VM 对照的被测关系须实际含足够候选历史，空关系不冒充索引收益。使用独立源副本开发，集成时重新核对与 F1a 的组合快照。

C2：integration_review 对公开计划 `5fc74736aa305d349e6bdf251a9e3d765790f584173c980a1667610538b3f56a` 独立审计 Ready。实现须传播同一 proof context 至 `_committed_fact_receipt`；head 内重复成员不可变成两个同 head 命中而改变 fence，真实重复新增 reservation 不被去重。opaque 畸形容器按原谓词及时点处理。采纳一般载荷另计实际验证字节 V 的澄清，保持单调用范围。

C0：integration_review 对计划 `c9be19f3da3288c9f8526bbd75f0c7895fb500239a1dbb4a24d18fe71a685e4b` 独立审计 Ready；采纳其冷建索引成本表述修正，不将扫描行数等同总 CPU 成本。

## 开发—验收及集成记录

A 的独立复核发现华氏循环小数差分的精确舍入中点错误，作者先加正负红灯回归，再改精确整数有理运算，未修改 Fraction oracle。最终 tools SHA-256 `06d1714eced8feed07e2780c6bc4dadacaf1ac6dc4ca59c4245283eb207b3276`；17 个交付文件清单摘要 `72e356552d304efc441858a1fc3f8f51c3ace4728668107c6eb9aad3f73a6fc4`。非作者 integration_review 独立46项及额外200个正负华氏中点复核通过，另验255/256行通过、257行拒绝。原 red 保留。A 不含 CLI、签名 Asset 绑定或真实业务资格。

B 独立审查发现提交次数与任务数混用、VM 代理误作上下界及 revision 来源混淆；已修订并通过非作者2项测试、12-cell小矩阵和注入失败保留复核。最终脚本 `8969545a6c0f927e082d2a286557ea29f54a8d5d305141d863bdfcedd34ae486`；测试 `52784f944c0f734b1ee934e60cb29cf781ace7e2d4fcbae2dfb4413bf9e2a7cc`。正式旧版本基线36 cells、216个隔离 pass全部通过，非作者逐项核对包hash、拓扑与计数；报告 SHA-256 `4c1aa572b48773f4bf42c53fc9638213ae40cfb80526c7390bdf14d7a62b097a`。旧统计口径和中断轮次另存，不以新字段补填旧结果。

C0 的非作者96项复验通过，独立64个真实签名 heads 的缺失键 VM 工作量减少96%，另验旧连接写入、回滚、只读缺索引及 writable reopen。只关闭精确去重查询热点；原投影和 allowance 重扫未关闭。作者原红灯和独立证据分别保留。

F1a 单包及独立组合验收 Passed，新增9文件清单 `84ff6ab3701d9a88ed75eee08da4f20e2d0f361d3da26d398a73dbd5d628bac0`；独立6项及11种 adapter 对抗检查通过。三条运行另核真实 receipt/head/claim、精确恢复观察归属、v6 frozen inputs/政策、qualification、导出和副本重建，原 Store 字节不变。组合代码/夹具669文件 `b01c8e9a5d451e93d6f457ff504c5f0f50df0ef24ce7be0c20fe9c8baddd0571`：源码3,095项、Ruff/555文件格式、树外安装106项通过；非作者另核975文件公开清单、660成员源码包、wheel和实际安装字节、最终治理49项及46相对链接。闭合仅更新验收元数据，重建最终源码包再核成员。原失败、错误断言、实际 publication 拒绝与 pytest 退出码未知的中断轮次分别保留。见[本批验证](../../reports/109-calibration-cli-development-2026-10-24.md)及[机器证据](../../reports/data/109-calibration-cli-verification.json)。不关闭 F1/F2/G 或 C 总目标。

首批准确代码/夹具快照660文件摘要 `a02df296e2d31d887d258f0fbfa00185a7ac956fea31bda22b9d69e9e2664263`：源码3,089项通过、Ruff与552文件格式通过；默认隔离构建的代理依赖下载失败保留，已有依赖构建成功。wheel `ef8d92d66ced152dcebe216e5b6c01238fb2c7c7f8e26e4c568e4e75a87ec4fe` 的235模块与源码及树外安装一致，安装组合142项与6-cell installed smoke通过。最终文档治理49项通过；非作者 integration_review 最终集成验收通过，绑定964文件公开清单 `bbaa02b77527b3b94d0d0ba7e428b333f7828da2f1e112bd16e7971c681823b8` 与上述代码快照。不以首批通过替代全阶段通过。见[首批验证](../../reports/108-sustained-engineering-foundation-2026-10-24.md)及[机器证据](../../reports/data/108-sustained-delivery-baseline.json)。

## 当前未决项

C/D/E 具体局部查询与可重建投影方案、兼容与迁移规则、增长阈值待 B 结果及独立审计；F1 的具体 CLI/公平对照协议待 A 接口固定。新真实 provider 执行须有有效授权和冻结协议；旧 report107 及旧 key 授权不自动成为新付费批次证据。多后端、云部署平台和一般自主研究可靠性不在本次实现承诺内。
