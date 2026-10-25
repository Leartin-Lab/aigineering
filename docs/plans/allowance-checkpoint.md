# C3a：SQLite 同 Store raw allowance checkpoint

状态：**隔离基础包单项独立验收 Passed，与 C3b consumer 的 root 源码、安装与构建检查通过，非作者组合终验 Passed**。本包是可重建的原始 allowance 汇总基础设施。基线冻结为 `2d8d4b626d4640ce948c7790a08c3e349f480d3b`。C1/C2 单包和组合开发增量独立通过。公开阶段总目标 C 仍 OPEN。

本包遵循[工作流适配](../development-workflow-adapter.md)，属于[阶段计划](sustained-engineering-delivery.md)。作者 Luna recheck_planning，root 集成，非作者 integration_review 审计与验收。

## 切片目的与边界

C1 将声明 projection context 限定到明确引用和相关 allowance 历史；C2 在单次调用中索引声明的完整证明输入。两者均未消除同一 funding source 的旧 reservation 在后续发布中重复折叠，也没有为 writer 的余额检查提供历史局部化。

C3a 只建立 SQLite 内的**可重建 raw checkpoint**，以及同一 SQLiteStore 对 checkpoint 后追加事实的 indexed tail refresh。它提供一个可独立比对的纯 fold、原子写维护、重建和只读审计边界，供随后 C3b 消费。它不更改 `allowance_available` 的调用路径，不向 projection 或 writer 提供余额，也不改变任何发布/拒绝决定。因此 C3a 单独没有端到端性能收益；其独立价值仅是先证明 C3b 将要信任的可恢复缓存生命周期和旧 writer tail 兼容性。因此按独立可审计的必要基础设施切片验收，开发交付仍与 C3b consumer 合并，不把 C3a 单独标成性能修复。

不做：改变 Causal Allowance 语义、授权/认证顺序、writer balance validation、projection dispatch、reservation/head proof、Contract budget、Redis、Claim/Asset 状态；改变 schema reader floor；用缓存取代签名事实；引入 Redis 或进程级余额 authority；同时声称关闭 D/E 或 C 总体 P²。

## 当前语义作为精确参考

`core/causal_allowance.py::allowance_available` 是参考结果 owner。它先按 `record.id` 建 dict 去重，同 ID 后项覆盖此前对象而保留首次插入顺序；随后依序处理 grant、reservation、source return、donor return、extinguishment 各组。每组对 `amount` 使用 `int(payload.get("amount", 0))`，不同 ID 的同值事实均独立计数；只在全部求和后执行 `max(0, granted - reserved + returned - returned_out - extinguished)`。只有在没有匹配 grant 时才使用传入 Contract 的 `budget`。0 amount 的 grant 仍令 grant 存在；负数及 `int()` 可接受的历史值保留现有代数行为。`TypeError`、`ValueError`、`OverflowError` 的可观察次序由上述分组顺序决定。

这不是写入许可。新 grant、paired return、终态 extinguishment 各自现有 validators 在不同 owner 中执行更严格/特定的校验。C3a 不改 `allowance_available`、验证器或这些异常所有者，也不把合法 SQLite Contract budget 声称为可超过 SQLite INTEGER 持久化界限。raw 累计量仍可能大于 INT64，应使用 Python 任意精度与十进制 TEXT。

## 建议数据与纯 fold 契约

在 `core/causal_allowance.py` 增加小型冻结类型与纯函数，不复制 allowance 规则：

- `RawAllowanceTotals` 只包含 `grant_count`、`granted_total`、`reserved_total`、`returned_in_total`、`returned_out_total`、`extinguished_total`。core 内数值为 Python `int`。不存 Contract budget fallback、clamped availability、信任状态、候选决定或合成 RuntimeRecord。
- `fold_raw_allowance_records(contract_id, records)` 对一个 source 的完整输入按参考 `record.id` 去重、字段精确相等、类型分组，并返回 `valid totals` 或 `opaque`。同一个 `allowance.returned` 可同时对相同 Contract 增加 returned-in 和 returned-out，不能按 ID/类型只折一边。数据库唯一记录行虽消除重复物化 ID，纯 fold 仍须测试参考函数的 last-ID overlay。
- 对 `int()` 失败的 JSON amount，将 checkpoint 标成 opaque/reference-only，不把坏值改成零、不在机械插入 hook 中抛出新的业务 rejection。随后仍由原 `allowance_available` 按原分组和原异常顺序处理。未知代码错误和 SQLite I/O/事务错误不捕获、不转成 cache miss；后者让外层原事务失败回滚。
- valid raw totals 保留负值和 0 grant count 的区别；只有消费 owner 之后再按现有语义决定 budget fallback 与 `max(0, ...)`。未知版本、非 canonical 十进制、字段缺失、cursor/basis 不一致、或无法证明完整性的 row 一律是 opaque，绝不解释为零余额。

SQLite 私有表按 Contract ID 唯一一行，建议字段为 `format_version`、`status(valid|opaque)`、`through_revision`、`basis_record_id`、`grant_count` 和五个 signed canonical-decimal TEXT component。cursor 为全局 runtime revision，不是该 Contract 的最后相关事实 revision；basis ID 对应恰在 cursor 上的 runtime row（cursor=0 时为空），用来检测 cursor 指向不存在/不同前缀的基本损坏。累积字段 canonical encoding 拒 `+1`、`01`、`-0` 等歧义，不经 SQLite 数值类型或 SQL `SUM`。表不对 `contracts` 加外键：legacy/trusted facts 和迁移顺序可能先有 allowance fact 后有 materialized Contract。它是私有 cache metadata，不是新事实、authority 或业务 schema。

## SQLite 读写生命周期

**Schema/兼容：**在 SQLite private table DDL 和其主键索引上工作；不 bump `CURRENT_SCHEMA_VERSION` 或 ADR-034 的 schema-19 reader floor。新代码 writable open 用现有 schema initialization 创建缺表；旧代码可忽略额外表。没有 checkpoint 表/行是合法 cache miss，必须重算参考值，不能因为新表缺失而拒绝 Store。`snapshot_from_path` 的只读 source 不得建表或改写；新代码在临时 snapshot 中面对缺表时应安全走 reference/miss。普通 snapshot 复制 checkpoint 时必须连同相同 immutable facts 复制，并保留运行 mode。用旧版/Reference mode Store 追加签名事实后，新版读取 tail 必须覆盖旧 writer 未维护的事实。

**一致快照：**读取 `checkpoint`、runtime `MAX(revision)`、cursor 后的 allowance tail 和 basis row 必须在一个 SQLite read transaction 中。若调用方已有 `BEGIN IMMEDIATE` 或其他外层事务，辅助函数不得 `BEGIN`、`COMMIT` 或 `ROLLBACK` 它；否则自行 `BEGIN` 一个 deferred read transaction，只关闭自己开启的事务。不能假定 Python sqlite 的多个 SELECT 自动共享一个 snapshot。C3b 未来还需将准确 Contract/terminal/grant existence 与余额输入绑定到同一 snapshot；C3a 不提前提供那些投影语义。

Tail 使用 C1 已存在的五个精确 allowance partial expression indexes，按 `(revision > cursor AND revision <= captured_revision)` 筛选关联字段：grant.contract_id、reserved.source_contract_id、returned.source_contract_id、returned.from_contract_id、extinguished.contract_id；revision 严格排序。多 OR 关系一次选择后按 record ID 规范化，不因 returned 的两个字段重叠重复折叠同一行；不同 record IDs 保留。超过 bind 参数限制分块。输出实测/测试中的 SQL returned rows、解码记录和 source fold count，而不以 VM opcode 冒称精确扫描行。

**机械维护：**只在 `SQLiteStore._insert_runtime_record` 成功插入一条新的 allowance balance fact 后更新受影响的 distinct Contract ID checkpoint，使用同一 connection 和调用者已有事务。grant 影响其 `contract_id`；reservation 影响 `source_contract_id`；return 分别影响 source 与 donor，二者相同则只刷新同一账户一遍但同时计入两个 raw components；extinguishment 影响其 `contract_id`。刷新为读取旧 checkpoint，折叠该 cursor 之后、包括刚插入行在内截至当前全局 revision 的精确 indexed tail，然后写入新 totals/cursor/basis。多条 reservation 在同批顺序插入时，每次只折叠新增 tail，不能每条重扫之前 reservation。

写入表本身和 RuntimeRecord 在同一原事务提交/回滚。若 cache DML 出现 SQLite 错误，向外传播并让整笔事务回滚，避免事实与声称有效 checkpoint 分离；未知 programming errors 也不吞。若新插入事实的 amount 无法进行参考 `int()` 折叠，则只将相关 checkpoint 状态设为 opaque，事务仍按原业务 owner 的决定继续；不得拒绝此前允许的事实或编造 amount。既有 record ID / 同 payload 重放不产生新事实、不重复累加、不强行前移 cursor；冲突仍由原 `ImmutableRecordConflict` owner 拒绝。Contract `add_contract/_insert_contract` 不写 allowance totals；budget fallback 不进 checkpoint。

这是集中 hook，不应在每个调用层复制更新逻辑。必须逐项核实 `append_runtime_record`、`commit_ingress_batch` 及派生 release/submission/terminal facts、claim/invocation failure/expiration writer、task handoff return、`persist_claim`/claim renewal、SQLite migration writers等 `_insert_runtime_record` 生产调用点的事务归属。普通公开 append 对 balance-changing allowance facts 的拒绝不变；测试私用 insert 不可被拿来当生产入口。任何不走 hook 的旧 binary/显式 reference-mode writer只追加原事实，之后 checkpoint reader 的 indexed tail 必须看见其已提交新 revision。

**无读侧写入：**查询/投影阶段不得为“暖缓存”写 checkpoint。checkpoint+tail 仅形成临时 raw 结果；不更新表，不在 Engine 内缓存。这样 malformed/forged Candidate 的认证拒绝和 audit 优先级不会被 pre-auth cache parse 预判，projection 仍纯。首次 cache miss 的完整历史 fold 允许 O(H)；warm cursor 后的实际 tail 必须只含相关新 facts。由于 C3a 不消费 raw result，以上先以 Store 内部诊断/测试接口验证，不纳入 public StoreProtocol。C3b Ready 时再审计 core typed balance reader 与接口所有权。

## 显式 reference mode 和 fallback

为 SQLite Store 提供显式内部 `checkpoint` / `reference` 模式，供差分、兼容与基准使用；它控制 raw helper 是否读/维护私有表，不改变 signed facts、所有 validators、事务或 observed decisions。默认 operational 路径不能静默切到 reference 以绕过 Redis 或掩盖 cache 故障。

Reference mode 完全忽略 checkpoint 并以完整 `scan_runtime_records`/`allowance_available` 构造 oracle。缺表、缺 row、格式未知或不可验证 cursor 均返回显式 cache miss/opaque，并走参考路径；SQLite I/O/编程错误仍传播。opaque 不可由后续好记录“从局部继续”变 valid；需 private rebuild 从可信 immutable prefix 重算。旧 writer checkpoint 之外的合法事实属于普通 tail 而非 cache corruption。测试确认 mode 切换、reopen、新旧 Store 同库和 `:memory:` 都不改变 facts/claims/决定。

## Rebuild、audit、snapshot 与 cache invalidation

新增 cache-only `rebuild_allowance_checkpoints` owner：没有外层事务时明确 BEGIN IMMEDIATE 后才捕获 MAX revision 和完整 immutable records，分组一次折叠并替换所有 cache rows，最后只提交自己开启的事务；失败只回滚自己开启的事务。已有外层事务时复用它，不 BEGIN/COMMIT/ROLLBACK，异常向调用方传播。分组不得对每个账户再次扫描整份 log；每事实只分派到受影响的 distinct IDs，同账户 return 同时算 credit/debit。cursor/basis 来自同一捕获前缀；SQLite writer 锁禁止替换期间其他写者穿过此前缀。不删除、重写或重排 immutable facts。

runtime materialization rebuild 在其现有 Contracts/Assets 重建写事务内调用该 owner；它只扩展 cache 的重建，不把其他 registration/claim/graph helper 改成全量原子事务。既有全 materialization rebuild 仍要求私有副本或外部停写，本包不声称修复其全部并发语义。cache-only owner 的双连接竞争独立验证：另一写者只能先于锁或在提交后写入，后续新事实由 tail 覆盖。只读 audit 仍不调用 rebuild。

增加只读 `audit_allowance_checkpoints`，在单一致快照对每个 `valid` row 重算其 exact source prefix 的参考 raw totals，并比较所有五个 component、grant count、format/status、through revision 和 basis ID；返回安全、结构化差异，不泄露原 payload。Missing row 是 cache miss，不是语义损坏；opaque 是 reference-only，不作为总和断言；损坏的 valid row 不能被标成有效或默默读为零。审计不得修复、追加或写 claim/完成事实。`runtime_materialization_digest` 先调用只读 audit，对损坏的 valid assertion 或结构非法行给安全 integrity error，再计算原语义 hash，不把 checkpoint 的物理存在、cursor 或编码写入 hash。缺表/缺行/规范 opaque 不影响 digest。reference mode 只旁路 operational checkpoint 读写，不禁止这项显式全审计；两者不得混淆。

正常 open 不做 O(H) 全缓存审计。checkpoint 与事实通常在相同 Store transaction 下维护，但任意手工 DB 改写不是受信任 guarantee。point helper 拒绝结构/编码/cursor/basis 损坏；canonical 但错误的 totals 且 basis 正确只能由 full audit 重算前缀发现，点读信任正常事务维护，不承诺即时发现这种数值篡改。完整 audit 能检测数值/前缀不一致。

缺表时 read/helper/hook 均明示 reference/miss，不隐式建表、写零或制造业务拒绝。缺行可在正常新 balance fact 的 writer hook 内 cold-fold 后创建；已知坏行或 opaque 行在后续 hook 中保持原行并跳过加速，不从局部好记录继续或偷偷修复。一次合法插入导致 amount coercion 失败时将此前有效行设为 opaque，后续只走 reference；显式 private rebuild 才清除并从完整原事实恢复。只有真实 cache DML/I/O 或未知编程错误传播并使原事务回滚；合法缺表/已知 invalid metadata 不是 SQLite I/O 故障。正常 writable schema initialization 建表与 read-only snapshot 缺表行为分别验证。

## 文件所有权建议

作者：Luna recheck_planning；root 为集成人，integration_review 为非作者计划审计与验收。C3a 可写文件限定为：

- `src/aigineering/core/causal_allowance.py`：raw totals/opaque typed result 与纯折叠，不改 reference balance helper 的行为。
- `src/aigineering/core/sqlite_schema.py`：私有表 DDL / constraint / PK；不改 schema floor。
- `src/aigineering/core/sqlite_store.py`：SQLite 私有读/tail/维护、explicit mode、snapshot/rebuild/audit 集成；hook 不获得业务授权。
- `tests/test_raw_allowance_totals.py` 与新 `tests/test_allowance_checkpoint.py`：纯语义差分、SQLite 生命周期和性能计数。
- 必要的新 `tests/architecture/test_allowance_checkpoint_ownership.py`：checkpoint 无权写 facts/claims，SQL 只在 SQLite adapter，core fold 纯函数，rebuild/audit不改原 facts，reference mode 真旁路 cache。

不改 `core/commitment.py`、`core/projection_context.py`、`core/allowance_returns.py`、`StoreProtocol`、插件、Redis、计划总表/ROADMAP、DESIGN、ADR-034/035。C3b 单独规划、独立 Ready 后决定如何消费 typed balances。作者交付 `docs/adr/ADR-047-rebuildable-allowance-checkpoints.md` 记录这一持久检查点决策与替代方案；新增 `changes/121-rebuildable-allowance-checkpoints.md` 只记录完成的行为和验证。ADR/change 分别记录决策与实际实现，不将计划写成已实现事实。root 负责主计划和状态整合。

C3a 先在独立源副本实现和验收；尚无消费者时，不单独推送增加 operational 写入成本的版本。C3b 通过独立计划审计、接入真实窄读取并取得组合收益证据后，才作为一批开发增量整合交付。两个切片仍分别保留作者与非作者结论。

## 计划依赖 / 开工依赖 / 集成依赖

- 计划依赖：当前签名 allowance/reference 语义、C1 五路 SQLite relation indexes 与 H 基线、C2 单调用 proof 索引通过事实，以及本文件对 checkpoint cursor/opaque/audit 的边界达成独立审计。若 checkpoint 方案无法安全表示 old writer committed prefix或 transaction/read snapshot，先改方案，不开工。
- 开工依赖：独立计划审计明确 Ready；8类新增/保留 regression fixture 已落测试设计；一个独占 SQLite/pytest evidence 目录。没有 Redis/provider 依赖，因为 C3a 不改变 operational paths；不运行 Redis也不能据此声称应用整体通过。
- 集成依赖：C3a 单包通过后，与 C1/C2 所在精确快照跑竞争、snapshot/reopen/rebuild 的组合审核；C3b 不以 C3a raw helper 名称或内部 SQL 调用直接绕过其自身 Ready/验收。C3 整体性能结论仍等待 C3b 与 allowance return/extinguishment、Asset/maintenance 热路径适用所有者关闭。

## 行为与结构验收

1. **纯 fold 对 reference 差分。** 合成、签名 facts 和内存 RuntimeRecord 输入均覆盖：无 grant/有 grant/零 grant；各类 debit/credit/extinguishment；正、零、负、相等/重复/不同 ID；returned source=donor 和不同 source/donor；ID 复用导致 last object overlay；missing amount、numeric string、bool、float 和 int 溢出/非法形状。可表示的合法 `int()` 值须与 `allowance_available` 原子数一致；不可转值成为 opaque，不吞、不改原函数异常类型/顺序。额外证明 `grants=[]` 才用 Contract budget、grant_count>0 不 fallback、raw totals不 clamp。
2. **尾部与高水位。** 对精确 signed root/child/return/terminal facts比较 Reference mode 与 checkpoint raw `allowance_available`：新 row 每次只有新关联 facts 被折叠，记录返回行数、decode数、source fold数、revision区间和 EXPLAIN。H=0/16/64 下 P 固定、源引用集合固定，warm tail 实际返回/折叠行恒为本次新 balance facts 数，不依历史 H 增长；冷 miss/rebuild单列 O(H)。SQL 只读事实和 cache，没有 in-memory全任务表。VM回调仅辅助指标，不等价实际扫描量。
3. **所有事实写口。** 用 signed Candidates 逐一路径制造 grant/reservation，return 与其 paired donor debit，terminal extinguishment、Worker failure/expiration派生终态及 migration fixture；分别检验 checkpoint rows 与 immutable fact reference 一致。对 exact idempotent replay不重复计入；同 ID 不同 payload按现有冲突拒绝。再用 direct Contract budget fallback和无grant Contract证明 checkpoint不存 budget。
4. **reference/旧writer。** 通过明示 Reference mode 的 Store、旧schema/无表 copy 和不同 Store instance写入新 signed allowance事实；checkpoint reader在下一一致snapshot扫描tail并与完整reference相等。missing/unknown-version/opaque checkpoint不能产出zero；重新启用cache模式、reopen、`:memory:`及同库双Engine时事实身份与 decisions不变。
5. **Transaction 原子与竞争。** 在 grant/return每种维护点注入事务内失败，确认 facts/checkpoint均回滚或维持原行，不能留下ahead cursor；预期金额coercion失败时只生成 opaque checkpoint，业务接受/拒绝由reference owner决定。并发两个 SQLite connections 对同一source签名发布，序列化前后 checkpoint累积无双计或丢项；两种mode结果及事实/claim/receipt/head身份完全一致。双源/同源return并发包含源 ID排序或锁定协议，不可死锁/乱写。
6. **Prefix / snapshot / rebuild / audit。** checkpoint写入、source事实写入一个backup snapshot一致保留；只读 path snapshot不得修改源且缺表安全。point helper拒绝cursor ahead、wrong basis ID、unknown version、bad decimal；canonical但错误的总数由full audit重算发现，不宣称point可见。cache-only rebuild只修derived row，semantic facts字节/IDs不变；私有副本strict runtime rebuild得到同一 `runtime_materialization_digest`，物理checkpoint是否缺席不改变语义digest，损坏valid行使严格cache audit失败。分别证明owner自有事务原子替换、外层事务中不偷commit/rollback，以及旧全rebuild的停写边界没有被扩大声称。
7. **机械完整性。** 枚举生产中每个 `_insert_runtime_record` callsite并断言 allowance balance facts 经过机械hook，包含迁移/`commit_ingress_batch` 内派生事实；原 public append对 allowance类型仍拒绝。断言 core fold不访问Store/SQL，私有cache行不出现在外部Store/list、trace、claim、facts、Redis或 signed Candidate，C1/C2 与 ADR-034 的现有不变量均保持。

## 性能、成本及未解决项

C3a 是加一个物化 projection；它的创建、commit与tail读取本身有额外成本。实现报告必须把冷 full fold、每次 writer cache刷新、warm tail实际 rows、SELECT/EXPLAIN/VM代理和端到端签名发布wall-time分开，不能宣称因为表上出现一行就变快。固定source/事实载荷、P=8/16/32/64 siblings/chain、H=0/16/64；如已有B脚本冻结扩展则使用同等隔离样本，报告 P/H、引用数、深度、相关新事实数。冷建/rebuild/完整 audit可 O(H)。writer钩子每新balance fact的正常工作应为索引定位与新增tail fold；不得每一步重算整个prefix。阈值在实现前由独立审计冻结，不从作者测到的曲线倒推门槛。

本切片的独立验收门槛是语义/一致性，不是提升：逐项参考决策和事实身份零差异；warm cursor的 actual fold input 不含 checkpoint之前事实；tail数据完整含 old-writer revision；rollback后不存在 checkpoint 领先事实；audit/rebuild只作用 derived table。若 writer refresh导致端到端恶化，也必须报告并调整/拒绝设计。

另固定同 source 已 checkpoint 的相关旧历史 R=0/16/64/256，分别保持新增 tail=1 和2，与无关 H 单列，逐路实际查询并计数。EXPLAIN 必须显示 source 等值及 revision/rowid 上下界共同参与 SEARCH，不能只有 USING INDEX 后再过滤全部旧 prefix；固定 tail 时 VM proxy 不得随 R 线性增长。现 C1 expression index 的隐含 rowid 可能已支持此范围，不预设加索引。保留每路 returned/decode/fold rows、EXPLAIN 和 VM，明确 VM 是工作代理，不当精确扫描行。正常维护不能把旧 Python 历史扫描藏入 SQL。

独立审计冻结量化门槛：相同 query shape/固定 tail=1或2，所有 R 的 returned/decode/fold input 数严格等于该新 tail；五路 SEARCH 同时含 source 等值与 revision/rowid 上下界；各 R 的 VM callbacks 不超过同 tail R0 的2倍+64。不事后调界限；合法实现若需变更，保留红灯再审计。多 grant 前缀的索引压力 fixture 明确为 raw query 数据，不能宣称已获多 grant 业务授权。

计数口径澄清：上述逐路非重叠压力 fixture 每路返回 tail 条；另测 source=donor 的 return 重叠。若实现分路 SELECT，同一事实可从两路各返回一次，必须报告原始 SQL 行数与按 record ID 去重后的 decode/fold 数，后者等于唯一新 tail。原始 SQL 行不得隐藏为 set 大小；其上界为五个关系的真实匹配次数，固定 tail 时仍不得随 R 增长。这一澄清不改变原各路 R/VM 门槛。

**明确没有关闭：** C1窄 context的source balance关系仍由后续C3b接入；writer每次发布若仍扫完整 reservation prefix，则同父 sequential P 仍为 P²；单次 proposal 的 publication proof成本由C2局部化但仍有完整head/receipt成本；handoff returns/extinguishments、projection、writer allowance consumers、Asset传播和claim maintenance整体热路径尚未全部消费这个checkpoint。旧写者不停运行可能让当前cursor长期落后，tail反复读旧 writer追加量；若该成本不可界定，C3b须加显示的参考/刷新政策而非隐藏读写副作用。未来非SQLite Store必须提供自有一致事务前缀/锁协议；通用 Store 语义接口不暴露SQLite sequence或checkpoint表。

## 方案风险与需审计的具体问题

- SQLite追加 revision在串行 writer+同事务提交下作为 private prefix；不能把该假设外推到 PostgreSQL sequence。多后端未来适配须用同一snapshot和可提交prefix证明；C3a不承诺多后端。
- DDL table create不升schema version是否满足本项目旧读者/备份规则；`snapshot_from_path`临时 Store 在backup前后对可选表的具体SQLite行为要先有行为测试。
- `basis_record_id`是否足以识别普通损坏，不防协同改写全表；安全文案需限于cache audit与正常immutable DB，不宣称攻击者防护。
- 重建期间并发writer的快照/锁定需要代码owner通过两连接实验定稿；不能由“runtime log append-only”自动推出无竞态。
- C3a虽然作为必要缓存foundation可审计，但没有余额consumer就没有产品速度/决策行为收益。本包可单独检验正确性，但仅与C3b consumer验证实际收益后组合交付，不能独立增加运行写成本。

## 规划—审计与开发—验收记录

integration_review 对计划 SHA-256 `05dc060385c8a8e555e756555f1ca3d61da9c7f8d373f5c2c432c2a989567c84` 独立审计 Ready，并在实施前冻结上述 R/tail 量化门槛。Luna 作者在隔离副本实施，root 不提前集成无消费者的运行成本。当时 C3b 尚未 Ready；其当前开工状态见[C3b计划](scoped-declaration-allowance.md)，整体 C 继续开放。

首轮独立组合发现新增 pure-fold 测试与既有 architecture 文件同 basename，标准 pytest collection 失败。仅将新文件更名为 `tests/test_raw_allowance_totals.py`；不更改全局 import mode，首轮失败保留；该首轮未通过，后续结论见下文。

修订后由 integration_review 非作者独立验收：95项行为/资金/架构与37项SQLite测试通过；另有2,500个纯差分向量、120格真实SQL返回/解码/折叠及VM门槛、4300位整数原红向量、已打开旧写者追加与只读snapshot严格重建检查通过。最终8文件摘要为 `e424a1cb32d80584260cdeefdd6ee042be7c94733ee0310137c200f57259c7ff`；最后只修change121陈旧的作者证据文字，代码/测试不变。单项 Passed 不代替全套与安装验证，也不证明端到端收益；与C3b consumer同快照验证后才组合交付。

C3a 与精确 C3b consumer 已在同一711文件代码/夹具快照通过root源码3,342项、树外444项、B完整矩阵与安装smoke。无消费者的foundation未单独进入开发分支；最终非作者组合验收 Passed，开发交付最后按文档/包轻闭合流程处理，见[开发验证](../../reports/112-local-allowance-development-2026-10-25.md)。
