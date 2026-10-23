# Report 107：CLI 动态任务闭环

状态：live08/09 有界真实案例已通过；最终源码、安装组合与独立总体复核通过，待 dev 推送。稳定资格和一般研究能力仍开放。日期：2026-10-23。

## 目标与总体标准

让操作者通过正式 CLI 明确发布根任务、输入、输出、权限、Worker 路由和接受政策。
动态拆解、规划、工具执行、续接、恢复及完成均由普通 runtime 任务、签名 Candidate
和既有 commitment 边界推进。示例只发布 Skill、输入、根 Contract 和适配配置，
不提供领域 DAG、独立调度器、可变任务状态或只读审计写入。

总体标准预先确定为：一个有用的有界样例可经 CLI 动态规划、先后消费两个工具观察、
提交报告、由不同授权身份绑定精确输入与报告接受，再精确导出。验收缺席、工具失败、
畸形输出、缺失输入和额度不足不能被报成成功；恢复须实际执行而不止 scheduled。
重开及严格重建保持语义，原始失败保留。确定性夹具、真实模型和安装制品证据分别报告。
这不是对任意目标、模型正确率或全自动研究可靠性的保证。

流程采用 [仓库适配](../development-workflow-adapter.md)，上游 development-workflow
版本为 `c626e75`。技术约束见 [DESIGN](../../DESIGN.md)、
[boundary invariants](../boundary-invariants.md) 和[贡献指南](../../CONTRIBUTING.md)。
不新增通用工具成败猜测、不把 tool_scope 当强制调用义务、不弱化自验收隔离，
不以历史成功替代当前证据。独立审计由非作者读取原始需求、实现及证据。

## 来源与当前判定

原始 Catalyst automation feasibility assessment（2026-10-19）为 759 行、48,450 bytes，
SHA-256 `cb3640a3a207b829b38cc1af7b2bccf314448652742ae6f1324ce04fa1a4f4b3`。
它是独立报告，不是待执行方案。对照现行知识库中的任务状态、规划执行模型和阶段验收，
再回到当前代码与正式 CLI 复现后作如下判定。

| 原报告线索 | 当前判定与处理 |
| --- | --- |
| 默认完成可能被误当业务真值 | 普通输出是授权断言；同名共享是已有语义，工具权限不等于必调义务。样例显式采用输出 shape、精确证据及独立接受，不引入正文关键词判定。 |
| `{success:false}` 被记为执行成功 | 宽松 schema 下仍成立；任意业务 JSON 不应由内核猜语义。适配器以现有 output schema 明确要求 success=true，失败走已有 invocation failure/recovery。 |
| 续接没有观察，所有多跳必失败 | 普遍结论被先前双工具链反证；本轮更完整样例确证原输入丢失、后续步骤丢较早观察，实施 change 110。 |
| 规划子任务缺省预算 1 被拒 | 真实缺省冲突，change 107 对工具缺省 2；显式不足仍拒绝，不能提升父额度。 |
| draft 自身预算 1、父输出说明矛盾 | draft/dependencies 是各一次普通文档任务，不需要各自再规划；说明中的父目标不同于当前声明输出。保留原阶段协议。 |
| 无恢复、拒绝信息不可操作 | 无恢复的普遍说法不成立；已有 change 105 保留有界结构化编译诊断。本轮再修有效 v6 恢复历史的 tuple/list wire 比较误拒。 |
| retry 无限授额 | 根 grant 拒绝已经进入基线；Worker retry 沿既有父与额度路径。旧 67 层 provider 压力结果不冒充本轮复现；通用资金治理仍按开发状态列限制。 |
| 无声明式多任务编排 | 普通 Contract 和 runtime 编译计划已表达生产消费关系。单根 preset 不等于通用工作流产品；不增加另一套 DAG 解释器。 |
| task 路径缺 session/trace/replay | task audit/events/runs/lineage 有事实证据；完整 session 产品对齐是后续方向，本轮给明确 task 审计入口。 |
| CLI 退出码/命名不齐 | 既有部分显式失败退出修复保留；本轮工具 add/show 明确完整 spec 和精确 ID，歧义拒绝。未宣称所有命令完全统一。 |
| 手工工具描述符与 Registry 碰撞 | CLI spec 表达不足及列表顺序依赖已确认；changes 106/108 修复，仅从已披露集合唯一匹配。 |
| attachment/report 前置不同 | 类型与精确证据约束，不能通过放宽证据祖先关系修复。 |
| 已有同名输入影响新配置任务 | 本轮组合新增确认；change 109 固定本次已提交输入 ID，resume 不重绑。 |
| 同域并发 relay 误报 ahead | 本轮验收实际观察并经有效签名双连接时序重现；change 112 仅在同一 Store 严格前进且原前缀保持时有界重取，再做完整 proof。真分歧/真 ahead 仍拒绝。 |
| Redis sync 在 WatchError 后以旧 source capture 重试 | 已增加 change 116：剩余固定预算内，从同一 Store 重取；只有 revision 严格前进且完整旧前缀保持才采用。未前进继续使用原 capture，fork 拒绝，最后一次失败不额外重取。持续竞争仍可能耗尽预算；该修复不证明既有集成失败由此原因导致。 |
| 聚合 task list 在 submitted 独立接受 child 上崩溃 | 已确认并由 change 113 修复：聚合 task list 对后代风险重用同一个 task-view projection snapshot，避免遗漏 effect context。32 项相关测试和独立 integration review 通过；live04 Store 经 source CLI 可读 9 个任务、revision 238。 |
| 合法 compile-stage 工具结果被当作 blueprint | 已确认并由 change 114 修复：compile stage 标签被 method child 继承后污染 active role，ToolWorker 真正成功结果因此触发 blueprint fence。共享 active-role owner 隔离 method child，保留六个 stage、新 nested stage 独占及旧 typed mixed-label 历史只读兼容；continuation/compile/retry/recovery 仍经 blueprint fence。 |
| 严格 JSON nested payload 在签名 action adapter 中被误拒 | 已确认并由 change 115 修复：`action_from_dict` 深冻结后，浅转换仍留下 nested `mappingproxy`，合法 attest/tool/parallel payload 无法 JSON 序列化。统一 deep thaw；独立差分审查以合法 payload 复现，147 项纯测试和 2 项 Redis 测试通过。原 2 项 sandbox 失败保留。 |

历史 Windows/CatAgent3 原始运行、长耗时工具、通用模型能力、取消树策略、完整会话产品及
一般规模资格不在这个有界开发组合中宣称关闭。每项未完成义务仍须可观察结果；边界变更
不修写旧事实，历史已发布空输入续接需要明确的新授权工作而非自动换合同。

## 工作包、依赖与两个循环

源码基线为 `825b4bd210b4ca62ae1b8f7cebacdc555cd6491b`，开工时含既存未提交
change 105。主协调者保留该工作并安排独立审查；私有 Store、旧工作区和原始日志不入库。
各包使用明确文件所有权；测试有独立临时目录、身份和证据，带固定 Redis key 的套件串行。

| 包 / 交付 | 计划与开工依赖 | 作者 / 独立审计与验收 | 当前状态 |
| --- | --- | --- | --- |
| 适配与最小计划 | 现行 workflow、原报告、OB 与仓库规则 | Luna workflow_plan / 主协调者 | 适配已接贡献入口、导航及本地根指南；总体记录以本文件维护。 |
| 105 结构化编译诊断 | 既存实现、认证失败边界、恢复 owner | 既存作者 / Luna workflow_plan | 无阻断独立审查，定向 39 项通过；已纳入本轮 3,012 项源码全套结果。历史证据保持其原快照。 |
| 106 完整 CLI 工具 spec、真实提交 ID、歧义拒绝 | 原报告工具组合复核、ToolSpec/schema owner | Luna recheck_tools / 主协调者 | 真实 CLI 9 项通过；未改变执行权限。 |
| 107 缺省工具预算及诊断兼容 | 计划编译最小额度规则；无输入/权限扩展 | Luna recheck_planning / Luna recheck_tools 与主协调者 | 两格式缺省一致；组合发现旧字段回归后已修，相关 114 项通过。 |
| 108 已披露描述符唯一匹配 | 106 spec 表达，可独立开发 | Luna recheck_planning / 主协调者 | 仅已有披露集合匹配，旧版单独披露仍拒绝；49 项相关工具检查通过。 |
| 109 配置输入固定 | ADR-032 既有 v6 guard、当前发布结果 | Luna workflow_plan / Luna recheck_planning 与主协调者 | 普通无敏感政策 preset 也固定本次 ID；冲突拒绝、重开、规划继承验证通过。 |
| 110 新续接保留输入与累积观察 | E 正式 CLI 红灯复现、历史 recognition 安全方案审计 | 主协调者 / Luna workflow_plan 计划审计、integration_review 实现审查 | 新任务保留原角色/政策，只冻结 exact parent context 加当前观察；旧发表不换合同，不刷新历史上下文。已通过非作者总体独立审查。 |
| 111 v6 恢复 wire 比较 | 有效 source/receipt/head/member 到达该精确 fence 的红灯 | Luna workflow_plan / 主协调者与 integration_review | 只规范化嵌套 tuple/list 表示；定向恢复历史测试 44 项通过，含 v6 system-tool recovery 的精确历史证明。 |
| 112 并发 relay capture 更新 | E 观察到的并发失败、双连接签名复现及独立计划审计 | Luna workflow_plan / 主协调者与 integration_review | 新测试文件 7 项、既有 collaboration 23 项、events 集成 1 项通过，Redis DB13；独立定向验证 20.02 秒。包含 sync 前缀安全刷新及 events 对冻结快照的 bounded contention 重证。 |
| 113 复用 task-view projection snapshot | live04 Store 真实聚合列表崩溃、task view 投影 owner | Luna recheck_planning / integration_review | 最小传递 effect context 修复；32 项相关测试及独立审查通过。source CLI 可读 live04 Store 的 9 个任务、revision 238。 |
| 114 隔离规划阶段 active role | live04 真 compile 工具成功后 blueprint fence 错拒、stage role owner | Luna recheck_tools / integration_review 与主协调者 | 已实现并进入最终快照；method child 不继承 compile stage 标签，六个 stage 隔离，nested stage 独占，兼容旧 typed mixed-label 历史只读读取。保留 continuation、compile/retry/recovery 的 blueprint fence。 |
| 115 严格 JSON payload 深度解冻 | live07 provider 失败与合法 JSON action adapter 差分复现 | Luna recheck_planning / integration_review | 已实现并进入最终快照；只修复 adapter 序列化的 nested frozen mapping，147 项 pure、2 项 Redis 定向验证通过。 |
| 116 WatchError 后安全刷新 Redis source capture | 116 前集成 E 同步失败现象及独立 WatchError 机制复现 | Luna recheck_tools / 主协调者与 integration_review | 实现已记录：同一 Store、固定三次预算、严格 revision 前进与完整旧前缀保持才更新捕获；non-advance 沿用原捕获，fork 拒绝，末次无额外 capture。116 前集成 E 与独占 round8 的原完整原因未知；真实签名双连接 Watch 机制差分与 34 项测试通过，独立相关 36 项通过。持续竞争仍可 pending。 |
| 117 恢复来源证明快照生命周期 | 116 后全套 E 的 TaskViewQueryMismatch、独立诊断与 snapshot 方案 Ready 审计 | Luna recheck_tools / 主协调者与 integration_review | 同一 SQLite snapshot 内 history/source/ancestor 证明；关闭副本后仍原 live runtime 发布。签名双连接差分、真 grant 错配、资源关闭、原 writer 发布及架构约束通过；独立 58 项通过，源码全套 3,039 项通过。 |
| 验证隔离修复 | 安装组合暴露 query 测试永久替换 CLI reader；不改变公开设计 | Luna recheck_planning / integration_review、主协调者 | pytest monkeypatch 取代自制替身；原 query→E 顺序安装红/绿与独立 5 项通过。最终全套 3,039 与原序安装组合 334 项通过。 |
| E CLI 动态研究、工具、恢复、接受、导出 | 106-117 正式 CLI、普通 runtime 和独立接受集成 | 主协调者样例、Luna recheck_tools 固定 local Worker 夹具 / integration_review | 有界目标 live08 完成，独占 Redis，无 worker errors，CLI exit 0。动态计划、summary→convert 双工具观察、不同 actor 独立 attestation/qualification、精确导出、重开和严格副本重建均有证据；确定性故障/正常夹具和独立组合验证通过。一般研究/稳定资格仍开放。 |

各包按实际依赖审计并独立验证。E 的 live08/09 曾由普通 CLI 和真实 provider 完成有界闭环；
确定性夹具与独立审计补充覆盖 failure/recovery。116 后全套发现的快照漂移已由 117 修复，最终完整证据及独立总体复核通过。根预算、
权限、投影纯度、SQLite 唯一 durable boundary 和 Redis 必需性均保持。成功样例不外推到任意
研究目标或稳定资格。

## 组合证据与未完成项

最终代码清单 SHA-256 为
`fd51a47d9a805a77d82489b329e138578456f63020db636ab164ccc7111a2d3c`，614 文件。
源码全套 3,039 passed、0 skipped、6 条既有警告，123.78s；Ruff 与 547 文件格式检查通过。
当前 wheel `9859ef509773c722c70d1d9750415e41c8e35b6838a07ff97c470316f7373dbd` 的
235 模块与源码及树外安装逐字节相同；原顺序安装组合 334 passed、52.79s。
运行时代码冻结后的独占 E 五轮十例通过（96.590s），独立验收相关 58 项及 E 两例通过。
独立总体复核已恢复通过。

live08 正常和 live09 实际工具故障恢复，均在 change115 安装制品上经普通 CLI 完成了
预声明有界目标；精确导出、独立只读事实审计与严格副本重建通过。后续 116/117 改动的是
operational relay 与恢复来源读取边界，当前没有重跑 provider；旧真实运行不重标为新制品。
默认隔离构建的依赖下载失败保留，已有依赖下 `--no-isolation` 构建成功。

失败证据分别保留：116 前集成 50 passed/1 failed 的 slot error、独占 Redis round8 的
sync pending，原完整原因未知；116 后全套 3,036 passed/1 failed 的 query revision
错误，经新签名双连接差分定位为恢复来源准备使用 live 查询。116 的 Watch 机制证明不
倒填前两次原栈；117 不放松 generic query 匹配，资源副本关闭后仍由原 publisher 提交。
历史 live01–07、共享测试库清空的隔离错误及 sandbox 连接失败也不被后续绿灯覆盖。

完整过程与快照证据见[开发报告](../../reports/107-cli-dynamic-research-development-2026-10-23.md)、
[机器清单](../../reports/data/107-cli-end-to-end-verification.json)。原始目标、实现边界和
当次组合由非作者 integration_review 独立验收；非作者 integration_review 已按当前完整证据恢复有界目标及 dev 开发交付的总体通过。

## 提交与交付

已审核功能增量逐次进入本地 `dev`；最终文档、示例及组合验收已通过，随后完成推送。
稳定资格、`main`、tag 与包发布不在本轮开发交付范围。
