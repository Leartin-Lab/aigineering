# C3b：声明余额与完整 publication 证明的局部读取

状态：Proposed，待独立计划审计，未开工。基线为 `2d8d4b626d4640ce948c7790a08c3e349f480d3b` 加经独立验收的 [C3a](allowance-checkpoint.md)；后者是开工依赖，不以在制代码作为已验收基线。

本包遵循[工作流适配](../development-workflow-adapter.md)，属于[阶段计划](sustained-engineering-delivery.md)的 C。root 负责方案与集成，Luna 执行固定实现，integration_review 非作者独立计划审计与验收。它只优化实际同质内置 `contract.declare` 的投影与 writer allowance 输入，不关闭 Asset、claim/maintenance、return/extinguishment 的其余热路径。

## 已确认问题与不可省略的证明

C1/C2 组合后，原 B 脚本的36组、216次运行仍显示逐项发布的历史重扫。H0 下 P8/16/32/64 的 `scan_runtime_records` 返回行分别为552/1,872/6,816/25,920；H64P64为206,528。`get_all_contracts` 仍调用2P次。计数只覆盖这些公方法，不是全部内部 SQL 或物理扫描行。导入模块逐文件与基线一致；原报告 imported revision 为空，另以模块哈希绑定，不改原字段补造来源。

原 writer 的完整 C2 owner 会检查所有 committed heads 对 funding fact 的竞争引用、全部匹配的声明，并在存在任意 opaque head 时保留旧扫描谓词。独立反例证明，只选择 pending IDs、目标 grant 和 source terminal 会把原本拒绝的历史变成接受。因此不能仅靠余额检查点删除全量读取，也不能假设历史 head 必然属于当前 Candidate。

## 同 Store 的精确证明关系索引

新增 adapter 私有的 head-member 多值关系索引，键为 member ID，值保留全部不同 head record IDs。它仅提供候选集：原 C2 owner 仍检查 member 与 causal-parent 同时匹配、head/receipt 完整结构、唯一性和原声明/资金规则。同一 head 的重复 member 在关系表只出现一次，与原匹配谓词一致；原 head 内容不改，原重复成员 fence 仍执行。不能按 Candidate ID 过滤竞争 heads。

安全索引谓词由一个纯 core helper 共享给 C2 和 SQLite 机械投影：members 必须 list/tuple、每项非空 str，causal_parents 必须 tuple、每项非空 str。其他容器为 opaque，不能舍弃。全域 opaque 标记或 reader 发现的旧写者尾部 opaque 都立即选择原完整 proof 输入；pending overlay 可能遮蔽旧 opaque 时仍可保守走完整路径，由原 owner 得出结果。

索引状态包含 adapter 私有 format/cursor/basis 与 opaque 标记；cursor 只适用于 SQLite 串行 writer 的完整已提交前缀，不进入通用 Store port。每次新 `candidate.committed` 插入后，在原同一事务内刷新 cursor 后的全部 head tail、更新多值关系并推进状态。非 head 的新事实不引起全表扫描。旧 binary 未维护索引时，reader 在同一 snapshot 补读 head type+revision tail，只生成临时补充关系，不读侧写入。缺表、缺状态或已知无效状态走完整 proof；正常下一 accepted head 的写事务可从完整 head 前缀 cold-build，成本单列。已有 opaque 不因后续正常 head 消失。

同一 Store 内的普通 B-tree/表是可重建派生数据，不增加事实或接受权威。回滚必须同时回滚 head、关系和 cursor。未知 SQL/I/O/编程错误传播；合法缺表不是业务拒绝。显式 reference mode 旁路此索引。只读完整 audit 重算关系与 opaque/status/prefix，检测遗漏、多余或数值/关系损坏；普通点读信任正常事务维护，不宣称无需读前缀即可发现自洽的人工数据库篡改。私有副本 rebuild 清除并重建派生索引，原事实不变；cache-only rebuild 事务所有权沿用 C3a，不扩大整个 runtime rebuild 的并发保证。

声明关系使用 SQLite 自维护的非唯一 partial expression index，按 `(candidate_id, contract.id)` 精确选出全部 `contract.declared`；SQL 显式要求 candidate_id、contract.id 为非空 JSON text，contract 为 object，防 bool/number 比较混淆。它保留不同 record IDs，不选首个、不覆盖歧义；旧写者由 SQLite 自动维护此原生索引。

## 不可变输入与余额消费

core 提供 optional immutable balance inputs：None 表示原 reference 路径；加速输入必须覆盖全部被请求的 source IDs，不能把缺 entry 当成零。valid raw totals 只在现有 allowance owner 内应用原公式：无 grant 才使用当前 Contract budget，最后才 clamp。opaque 保留准确 source facts，由原 `allowance_available` 在认证后按原分组计算/报错，不在预认证读取中提前转换坏 amount。原函数保持参考行为。

C1 的 selected inputs / EffectProjectionContext 可增加上述可选字段；Memory、旧/custom reader 默认 None。SQLite 在一个 adapter-owned snapshot 中选择 Contracts、Assets、余额/回退事实以及 replay receipt/head/member。pending 新 parent 不进入 existing Contracts；但所有 source IDs 包括 pending parent 都必须获得准确 raw 输入，保留旧历史和 migration budget 语义。raw balance 已覆盖的前缀不能因 replay/proof records 再次计入。混合/injected effects 保留原 context。

## Writer 的精确选择与原边界

新请求由 core 根据实际内置声明 intent 和候选事实构造，不接受调用者的“已安全”布尔开关。仅支持已知 canonical task identity 的 source/incoming IDs 和识别出的实际声明 batch；recordless、未知/legacy IDs、mixed/injected、return/extinguishment/terminal batches 保留原路径。legacy terminal predicate 原来用 str(payload.contract_id)，只有确认非字符串 JSON 无法 stringify 成该 canonical ID 才能使用精确 SQL selector；不按 terminal outcome 筛选。

实际 claim-bound 声明可带 `attempt.closed(outcome=expanded)` 及对应 trace；不能误将它当新 feature 或丢掉。`claim.submitted` 是第二次 allowance 校验之后的派生写入；`idempotency.bound` 不能未经来源核实就当普通声明事实放入 allowlist。审计前冻结支持的完整事实集合与构造规则；无法确认时走 reference，不放宽原 validator。

每次 writer 校验都在原 BEGIN IMMEDIATE 内选择：精确 materialized incoming/source Contracts；全部 pending record IDs 的 existing 对象用于原 overlay/replay；所有目标 Contract 的既存 grants；source 的全部 terminal 事实；source raw balances/opaque 原事实；全部 funding/declaration facts 的竞争 heads 和选中 head 的完整 members/receipt 闭包；原 owner 可查询的全部 `(receipt Candidate ID, Contract ID)` 声明匹配。

证明读取分为有界 typed stages：先按 funding fact IDs 取全部 head 候选，再按完整 head 取 members/receipt；只有唯一 funding head 的 Candidate ID 与其精确 receipt 一致且可安全读取时，才据此做声明 pair selection，否则完整 reference。接着对全部匹配声明的 fact IDs 再反向查全部 heads，并补齐这一轮新增 head 的完整 members/receipt 闭包。第二轮必须包含仅引用 declaration、不引用 grant/reservation 的其他 Candidate 竞争 head。不是递归遍历任意历史关系：原 owner 只为 funding facts 和匹配声明查询 head，两轮即可；完整证明载荷 M 为两轮 union。所有阶段保持同一 writer snapshot。existing 和 pending 必须先按原 record-ID overlay 语义解释，不能直接拼接造成假歧义；关系查询重叠只去掉同一个存储记录。缺失、重复或畸形证明仍由原 owner 拒绝，不把没有选到当作不存在。

保留原两次 `_validate_allowance_records`，两次均 fresh 选择；不移除 candidate 幂等、claim/key/snapshot、完整 funding proof、root retry、source terminal、全 batch reservation 和 immutable payload 冲突检查。SQL 只在 SQLite adapter，core 只构造不可变语义请求与纯校验，不暴露表或 revision。

## 文件、依赖与交付

预计 owner：`core/causal_allowance.py`、`projection_context.py`、`effect_projection.py`、可选 `store_capabilities.py`、`allowance_returns.py` 的共享纯索引谓词适配、一个窄的纯 publication-index helper、`sqlite_store.py`/`sqlite_schema.py`，对应新行为/架构测试。不得修改 A/F1a/F1b、Redis、provider、依赖或 task wire。最终精确文件名单与 helper/typed request 契约由独立 Ready 冻结后交给 Luna；现在不授权实施。

一个新 ADR 只记录可重建 publication 关系索引这一持久决策及替代方案；一个 change 只记录完成行为。C3a 余额 foundation 与本 consumer/proof index 分别验收，在同一候选快照验证收益后组合提交 dev，不先推送没有消费者的写成本。root 负责计划、状态、报告和最终制品；作者自测不作独立结论。

计划依赖为上述独立反例、C3a 接口设计和具体 selection/overlay 审计；开工依赖为 C3a 独立验收、精确文件/API/事实集合及增长门槛 Ready；集成依赖为旧/新 writer、竞争、重放、reopen/rebuild、完整 CLI 的同快照组合门禁。

## 验收与预定增长门槛

行为差分需覆盖 root/child、同批逆序 parent、siblings/chain、pending/existing parent、无/零 grant、opaque grouped 错误、跨 Candidate 竞争 head、全域 opaque head、重复声明、pending ID 遮蔽旧关系、缺/错/重复 receipt/member、exact replay、forged audit 与 claim 不变。feature 红灯不能被更早 generic 拒绝冒充。

双 Engine 在相同旧余额投影后竞争，必须只允许预算内的一方提交；source terminal/余额变化须由原 writer fence 拒绝。测试 facts+两个派生索引在失败/rollback/重开后不分离，已打开旧 binary 写者的两类尾部都被覆盖。缺/禁用 cache 保持同决定和事实身份，允许慢。snapshot/private rebuild 摘要及原 Store 字节不变，audit 不修复事实。

完整 B 矩阵保持 P8/16/32/64、H0/16/64、三模式、每组三次隔离 timing 与 instrumentation。旧 B driver 不变以保可比，另用 test-side connection/row-decoder/fold wrappers 分阶段计数新增 direct SQL，不能漏掉新读取后宣称变少。

正常 warm、固定引用/载荷/控制历史 W 下，同一 P 的 projection/writer/cache/tail 物化量在不同无关 H 中必须精确相同；不调用全量 get_all_contracts/get_all_assets 或无类型全 runtime scan。必要 actor/control W 单列。每条 source 的 raw 输入读取上界为投影一次+两次 writer，共3U（U为实际 source 请求次数）；维护按新事实影响的账户数另计。warm 后折叠的旧相关 prefix 必须为零，新 tail 总 fold 数严格等于新 balance facts 按受影响账户计数。完整 head 成员 M、验证字节 V、Asset refs S 及 cold build/audit/replay/opaque/legacy fallback 单列，不能承诺与合法证明载荷大小无关。结构证明应给出这些显式集合与循环边界，不以有限计时点拟合复杂度。R/source-tail 门槛沿用 C3a，head tail 另需 type+revision 范围 SEARCH 与固定新 tail 不随旧 head R 线性增长。

## 当前未决项

这是方案细化，尚未 Ready。实现前还须冻结共享纯 helper、typed staged requests、原 overlay 的关系修正、精确 batch allowlist、publication 索引状态/audit/rebuild schema 和头尾 VM 量化门槛。存在这些未决项时不能声称已经关闭 writer 历史增长。

## 待独立审计的实施契约

以下将未决项收敛为候选固定契约，仍须独立审计，不因写入本节自动 Ready。

生产文件限定为 `core/allowance_publication_index.py`（新）、`core/allowance_returns.py`、`core/causal_allowance.py`、`core/projection_context.py`、`core/effect_projection.py`、`core/sqlite_store.py`、`core/sqlite_schema.py`。新行为文件为 `tests/test_allowance_publication_selection.py`、`tests/test_allowance_projection_selection.py`，架构为 `tests/architecture/test_allowance_publication_index_ownership.py`；作者另交 `docs/adr/ADR-048-rebuildable-publication-relations.md` 和 `changes/122-local-allowance-publication-selection.md`。root 独占计划/状态/报告。没有额外 mandatory StoreProtocol 或 backend；C1 可选 reader 的旧返回对象用默认 None 保持兼容。

纯新模块提供 `safe_committed_head_relations(record)`：对非 head 返回 None，head 的安全形状完全沿用 C2，返回 members/parents 两个 frozenset，不验证 envelope、hash、唯一性或签名。C2 只以此替代原 shape/set 构造，其他规则不改。模块中的 frozen request/result 类型只含 IDs、Contract/RuntimeRecord、tuple/frozenset，避免运行时循环导入；不依赖 Store、SQL 或 callbacks。

余额输入为 frozen `AllowanceBalanceInput(contract_id, totals, reference_records)`，totals 为 C3a RawAllowanceTotals 或 None。若 totals 非空，reference_records 不参与该 source 的余额计算；若为空，reference_records 是该 source 的完整原事实，由原 owner 求值。整个可选字段 None 表示旧路径；tuple 缺被请求 source 是 adapter integrity/programming failure，不能补零。最终算式抽到一个原 allowance owner 内的纯 helper，reference 与 totals 路径共用，不复制一套业务规则。

私有表为 `publication_head_members(head_record_id TEXT, member_id TEXT, PRIMARY KEY(head_record_id,member_id))`，另有非唯一 `(member_id,head_record_id)` B-tree；`publication_index_state` 恰一行，保存 format_version、through_revision、basis_record_id、has_opaque_heads。无 Contract 外键，不增加 schema reader floor。状态缺失不信任残留关系；reader 完整 fallback，下一新 head 的原写事务可 cold-rebuild。已知格式/cursor/basis 损坏的现有状态不在读侧或局部追加中悄悄修复，只 fallback，显式 rebuild 恢复；未知 I/O 仍传播。opaque flag 保留，tail opaque 当次 full fallback。使用 C3a 的显式 reference mode 一并旁路 allowance proof 加速，显式完整 audit 不受旁路影响。

任一必要 cache 表缺失时，read 与 head hook 均旁路，不隐式 DDL；只有正常 writable schema initialization 建表。上述 cold-build 仅指两表已存在而 state 行缺失。完整 audit 在 cache 表齐全时核有效状态的断言；无表/空索引是性能缺失，state 缺失而残留非空关系报告安全完整性错误，operational 读取仍只用 reference。

缓存 rebuild owner 没有外层事务才 BEGIN IMMEDIATE 并自己提交/回滚；已有事务不替调用方提交/回滚。head hook 在 `_insert_runtime_record` 真正新插入 committed head 之后机械调用，刷新完整 type+revision tail 后写关系和状态，同一 fact transaction。重复 ID replay 不追加关系或挪 cursor。full audit 对 captured prefix 一次分派并重算 expected member pairs/opaque，检测缺漏、多余和坏 metadata；不逐 head 扫整个 log。runtime 私有副本 rebuild 在既有写阶段调用此 owner，digest 先核其完整性但不 hash 物理 cache 时序/存在性，与 C3a 缺表/缺行/reference 语义一致。

writer read hint 仅在实际非空 effects 全为内置 contract.declare、每个 canonical payload 可解析且 ID/source 为 `task:v3/v4/v5/v6` 的标准 content identity、pending 类型属于 candidate.received/candidate.committed/contract.declared/allowance.granted/allowance.reserved/trace.recorded/attempt.closed 时成立。attempt.closed 仅 outcome expanded；未知形状走原 reference。hint 不建立新的 rejection。基础输入先取全部 pending IDs 的 existing rows；如同 ID payload 与原值不同，hint 选择完整 reference，让原 validate_allowance_commit 的原顺序冲突检查拒绝，不在新 reader 提前判域错误。

typed stages 固定为：`PublicationHeadsRequest(member_ids)` 查询 funding facts 的全部既存候选 heads；`PublicationClosureRequest(record_ids)` 查询这一轮完整 member/parent/receipt；`DeclarationPairsRequest(pairs)` 查询全部精确匹配；再以所有匹配声明 IDs（含 overlay 后 pending 对象）重复 `PublicationHeadsRequest` 和 `PublicationClosureRequest` 两步。第二轮闭包与首轮 union，不额外递归查无关成员的业务关系。每步由 core 纯函数据 previous inputs/pending 构造，SQLite 仅执行查询；nullable result 表示无法完整选择，调用方加载原完整输入。core 不在 hint 阶段执行 canonical proof validation；不可安全解释的 container/receipt/pair 或无唯一 funding head 时只要求 reference，原 owner 决定异常时点。pending head/fact 对按 ID 的原 map 进行 overlay；旧 head 的过时关系候选必须按 overlay 后对象重新过滤，不能拼接导致假歧义。不同 IDs 始终保留。声明 pair 取唯一 funding head 的 Candidate ID，须其精确 receipt 同值方可安全派生；不假设它等于当前 Candidate，也不用不一致 receipt 替换 head 字段。

旧/custom selected 返回对象不一定有新字段，加载使用 getattr(..., None) 或等价兼容，不能只靠新 dataclass 默认值。原注册边界已禁止 injected handler 替换内置类型；有未执行的 injected registry 不使纯声明退化，实际 mixed/injected 才 full fallback。原 effect_fact_owners 不变，不新增授权布尔开关。

首轮 closure 还须取全部被校验 funding facts 的 direct causal-parent IDs，不只取 head 的 parents。原 `_committed_fact_receipt` 会在 grant（root）或 reservation（child）的直接父对象中检查 receipt 数量；畸形但可查询的额外父 receipt 不能因未选中而改变异常结果。仍仅这一层明确引用，不递归 ancestry；其他无法安全构造的 hint 走完整 reference。匹配声明的 direct parent 值由原 owner 原样比较，不能在 hint 中修正。

原两次 writer 校验分别 fresh 构造这些 inputs；`_validate_allowance_records` 只在 commit_ingress 的实际 Candidate 参数齐备时尝试 hint，failure/expiry/recordless 等调用保留默认 reference。所有 pending records 仍原样交给原 validator，选集只缩 existing facts；不得省略原 grant/receipt/head/terminal/整批余额规则。

头尾压力冻结 R0/16/64/256、tail1/2：head tail SEARCH 需同时 type equality 与 revision 两界；member lookup 需 member equality，declaration lookup 需两个 text expression 等值。固定 tail/完整证明载荷时，每 R 的 VM callbacks 不超过同 tail R0 的2倍+64；returned/decode 与真实 selected heads/members/pairs 严格一致。opaque/缺 cache/cold 情况另列。SQL row-factory 的真实返回数、RuntimeRecord decode 和 fold wrappers 分别记录，不能用 typed result 自报计数自证。上述 head-tail 门槛与余额 R 门槛并列，不以正常数据省略全域反例。
