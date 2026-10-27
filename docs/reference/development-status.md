# 开发功能状态

更新日期：2026-10-27；开发版本：`0.5.13.dev0`。

项目定位：Zero Trust agent runtime for auditable AI-assisted engineering and research.

“已实现”表示功能已经进入仓库并有开发验证，不表示整个稳定版本已通过验收。
“部分实现”表示可用范围与未完成部分需要分别看待；“未开发”不作为已有能力宣传。
每项证据只证明其记录的快照和范围。

新阶段目标是让代表性工程分析任务在固定条件下反复交付可检查的结果，并测量
质量、实际恢复、人工介入、耗时和总成本。新工作区与同一长期累积工作区分别验证；
已确认的任务发布历史重扫属于待修复缺陷，不能以未来规模资格替代。保持事务与
事实权威边界，数据库可替换性作为适配约束，暂不开发多个数据库后端。
具体工作包、独立审计和验收状态以[阶段计划](../plans/sustained-engineering-delivery.md)为准。
下表记录已有能力；本阶段目标尚未完成，历史有界案例不替代新阶段证据。

| 功能 | 状态 | 当前可用范围与剩余工作 |
| --- | --- | --- |
| 可复算的工程分析验收器 | 部分实现 | [合成校准工具与 oracle](../../examples/thermal_calibration/README.md)已有固定数据、精确计算与逐样本证据检查；[CLI 配置](../../examples/thermal_calibration/CLI.md)的普通规划、双工具续接、不同 actor 确定性接受、实际恢复及原根续跑已通过协议夹具独立复验。源码3,095项与树外安装106项通过；真实模型与业务收益仍待验证。另有[离线直接循环和CLI失败组合](../../reports/111-direct-harness-protocol-development-2026-10-24.md)单包及组合独立验收通过，源码3,208项、零跳过与树外300项通过；前者共享工具/oracle，后者覆盖raw Candidate错误、缺观察及child额度耗尽，不构成公平模型对照。见[F1a记录](../../reports/109-calibration-cli-development-2026-10-24.md)。 |
| 长期工作区的局部查询成本 | 部分实现 | change118–122已覆盖精确去重、同批证明、支持的纯声明上下文与两次writer额度校验，C3已随3d2e815开发交付。change123收窄纯Asset effect-projection上下文，D0c校正真实入口测量；35文件组合源码3,532项、树外656项通过，独立总体验收Passed，最终元数据/源码包按相同717文件代码快照轻闭合。FactReducer传播、claim/maintenance、legacy fallback及整体n²仍开放。见[前批验证](../../reports/113-asset-context-direct-transport-development-2026-10-25.md)。 |
| 任务创建、状态、列表、看板、统计 | 已实现 | `task create/status/list/board/stats/show`；状态来自已提交事实，Redis 默认必需。 |
| 文件输入、Skill 与上下文选择 | 已实现 | 冻结输入、角色绑定、按 Contract 披露；`task context` 默认只展示元数据。 |
| 模型 Worker 与配置入口 | 已实现 | `run --config`、显式任务恢复和兼容模型 Worker；report107 的普通 CLI live08 使用 deepseek-flash 完成有界真实任务，provider 证据见 [开发记录](../../reports/107-cli-dynamic-research-development-2026-10-23.md)。 |
| 工具执行、被动续接与规划展开 | 部分实现 | report107 live08 通过普通 runtime 动态规划、summary→convert 两工具续接、独立 verifier 接受及导出。change 114 隔离 method child 的规划阶段标签；change 115 修复严格 JSON payload 的深冻结对象序列化。任意目标、复杂研究及通用模型可靠性仍未验收。 |
| 规划拒绝的结构化诊断 | 部分实现 | plan/replan compile 拒绝字段经有界版本格式保留到既有恢复上下文；签名领取后的失败、重开、发布中断、领取披露和精确诊断有合成夹具验证。未验证编译拒绝后继修正 blueprint；工具方法真实恢复见 live09；见 [change 105](../../changes/105-structured-compile-failure-diagnostics.md)。 |
| 结果检查与文件导出 | 已实现 | `task result` 按冻结接受政策检查输出，再导出精确内容；未接受输出不能当作完成。 |
| 评论、阻塞、解除与提升 | 已实现 | 签名评论及 holds；解除不重置已领取任务，提升绑定完整原 hold 集。 |
| 取消、重试、交接与文本编辑 | 已实现 | 发布新的有界后继，保留原任务、claim 与终态；不会原地重置任务。 |
| CLI retry 资金守恒 | 部分实现 | 新的根 retry 不能无扣账授额；投影与 writer 均拒绝，保留审计，历史精确重放不补授额。非根 retry 仍按原 containment parent 预留；原工作授权域、旧资金注销/转移/冻结和跨身份恢复语义未决。见 [change 104](../../changes/104-retry-root-grant-conservation.md)。 |
| 独立审查、接受、退回与修订 | 部分实现 | 普通 reviewer、政策决定及签名修订已实现；10/18 历史 reviewer 建议修改并由 Root 另作有理由的裁定；本轮 live08/09 由独立 Worker 绑定精确证据接受，无操作者补验收。工具续接有界保留默认审查余额，重启复用精确原任务；后续合法支出仍可能使固定意图被拒绝。见 [change 102](../../changes/102-continuation-review-funding.md)。 |
| 指派、改派、优先级与依赖 | 已实现 | 共享 SQLite claim 边界；优先级仅排序，依赖不扩大输入或权限。 |
| 批量操作与中断恢复 | 已实现 | 多任务阻塞、解除、取消、提升、指派、改派、优先级及重试；逐任务原子结果，保留部分成功及 Redis pending。 |
| 运行历史、诊断、日志、事件与观察 | 已实现 | 精确 claim 诊断、辅助本地日志及事实事件；日志结束不代表业务完成，完整会话记录未提供。 |
| 附件关联与导出 | 部分实现 | 已有签名关联、历史和字节校验导出；附件移除及保留清理未开发。 |
| 多看板与工作区切换 | 已实现 | `board create/list/show/switch/archive`；属于本地路由配置，归档不取消任务或停止 Worker。 |
| 持续 Fleet 与进程管理 | 已实现 | `fleet run/start/status/stop/restart`；本次 Tool-only 有界周期实际执行通过，本轮有界配置 Fleet 的真实模型闭环通过，常驻长跑仍待补齐。 |
| 统一单次执行循环 | 已实现 | `run --once`、配置运行及 `fleet run --once` 共用执行 owner，首次成功领取后停止；本次真实工具提交已接受，根未完成的周期边界仍返回非零。 |
| 任务关系导航与数百节点管理 | 部分实现 | `task lineage ID` 已证明 containment、重试、部分恢复、handoff 与 revision 关系；change 113 复用同一 task-view projection snapshot 修复 submitted 独立接受 child 的聚合列表崩溃，32 项相关测试和独立集成审查通过。被动续接关系、折叠及复杂自然任务规模验收未完成。 |
| 统一任务失败处理 | 部分实现 | 工具/MCP 异常共用现有 claim 失败通路，工具恢复保留原请求和范围；显式 `/fail` 普通任务完成后使父任务失败，恢复由操作者另行决定。目标运行等待树内有效 claim；当前制品已有签名失败/恢复夹具，不将本次实际成功链当作全部真实失败源验收。 |
| Redis 一致性与恢复 | 部分实现 | 事件流与查询代核对精确 SQLite 历史前缀，普通同步拒绝分歧；显式修复重新读取当前工作区，查询失败保留非 ready 栅栏并检测并发修改。change 112 只在同一 Store 前进且原前缀保持时刷新 ahead capture；events 遇有界 sync contention 会重跑冻结前缀证明。change 116 在 WatchError 后仅于同一 Store revision 严格前进、完整保留旧前缀时更新 capture；不前进沿用原 capture，fork 拒绝，最后一次尝试不额外重取。固定预算耗尽后持续竞争仍是可见 contention。116 前同步失败原因未完全确定；116 后全套发现的恢复快照漂移由 change117 独立修复。当前源码全套、安装组合及独立总体复核均通过。见 [change 103](../../changes/103-query-source-binding-and-conditional-repair.md)、[change 112](../../changes/112-concurrent-relay-source-refresh.md) 与 [change 116](../../changes/116-refresh-redis-source-after-watch-conflict.md)。 |
| 定时启动、配置通知 | 未开发 | 后续方向，不作为本阶段验收条件。 |
| 保留清理、PR 接受适配 | 未开发 | 完整功能为后续方向；长跑及一般规模的稳定资格另行验收。 |

2026-10-18 开发验收绑定源码 `52020af` 和 wheel SHA-256
`1f7725623f47bcae791e816489721e52a1b7e4eaa9d493549b1c9631bfe1d016`：
外部 Luna 经正式 CLI 创建、签名领取和提交工具请求，ToolWorker 实际执行，
另一新上下文 Luna 从真实观察续接生成中文报告，再由不同身份的 Luna 提交普通审查。
Reviewer 建议修改；Root 保留该建议，核对生产包与审查包的区别、原签名任务参数、
精确 claim 输出及冻结证据后，另作有理由的政策接受裁定，没有改写报告或扩大政策。
这包含操作者准备与介入，不是无人干预验收、自然人审计或独立模型一致同意。

根任务随后为 completed，精确合格导出为 51,875 字节，SHA-256
`39819a19406ffbb06332bcf928642e097ffeecafc71891d937b67376c35cf724`。
新进程重开读取及私有副本严格重建通过；175 条事实、含 Worker 身份绑定的语义摘要
保持一致，原 Store 字节未变。参数、脚本及本地连接限制的失败均保留。
全量 2,896 项、治理 49 项、安装后 465 项及三条适配器恢复夹具通过；
这些夹具不冒充真实 provider 失败执行，本次未调用配置的 DeepSeek API。

## 2026-10-23 report107 开发快照

该历史 change105–117 快照的 614 文件代码清单 SHA-256 为
`fd51a47d9a805a77d82489b329e138578456f63020db636ab164ccc7111a2d3c`。
源码全套 3,039 passed、0 skipped、6 条既有 warning，123.78s；Ruff 与 547 文件格式检查通过。
该历史 wheel SHA-256 为 `9859ef509773c722c70d1d9750415e41c8e35b6838a07ff97c470316f7373dbd`，
235 个模块与源码及树外安装逐字节相同；安装后受影响组合原序 334 passed、52.79s。
独立总体复核已恢复有界目标和 dev 开发交付通过。
默认隔离构建因网络代理无法下载依赖的失败保留；已有依赖下 `--no-isolation` 构建成功。

正式 CLI 的真实 deepseek-flash 正常与显式工具故障恢复案例 live08/09 已在 change115
制品完成：动态普通规划、summary→convert 双观察、报告、不同 actor 精确接受、根完成、
导出、重开和严格副本重建。原失败与 claim 保留，恢复确实领取并执行；不是只 scheduled。
不同身份使用同模型不等于独立知识来源；本地 audit 不是外部 checkpoint。
这两次真实执行继续绑定其实际旧制品，不写成当前 wheel 的 provider 运行。

change116 的真实签名双连接差分证明 Watch 冲突后安全刷新可避免浪费已有三次预算，
仍拒绝分叉和持续竞争。其前的集成 slot error、独占 Redis round8 pending 原完整原因未知；
不会倒填为同一机制。116 后全套又发现 `TaskViewQueryMismatch`，change117 将恢复 history、
来源及祖先证明放入同一 SQLite snapshot，关闭副本后仍向原 live Store 签名发布。
独立旧函数差分复现该错误，当前相关 58 项及独占 CLI 两例通过；generic query 匹配和
writer 权限、预算检查保持。详见 [change116](../../changes/116-refresh-redis-source-after-watch-conflict.md)、
[change117](../../changes/117-snapshot-recovery-source-preparation.md)。

源码、安装组合及有界 E 证据均已通过，非作者已恢复独立总体通过结论。
历史失败、验证隔离错误、确定性/真实 provider/安装制品证据与适用快照分别保留在
[开发记录](../../reports/107-cli-dynamic-research-development-2026-10-23.md)及
[机器证据](../../reports/data/107-cli-end-to-end-verification.json)。本轮目标限于有界 CLI 闭环；
一般研究能力、任意目标可靠性和稳定资格仍开放。

日常操作参见 [任务管理](task-board.md)、[配置运行](configured-runs.md)、
[持续 Fleet](local-fleet-service.md) 和 [任务附件](task-attachments.md)。
历史有界验证见 [任务关系验证](../../reports/105-task-revision-lineage-2026-10-11.md)；
历史真实报告的独立接受与导出见
[报告验收记录](../../reports/091-real-repository-report-acceptance-2026-10-07.md)。
历史通过结果不替代当前版本的真实模型、独立接受和导出验收。

## 分支与交付方式

日常开发成果提交到 `dev`。进入 `dev` 前，功能应有相应实现、独立审查、
测试与准确文档，并通过适用的检查和构建；已知未完功能继续列在开发清单中。
开发分支允许尚未达到整个稳定版本要求的增量，不要求等所有未来功能完成才更新。

稳定后再将已验收版本进入 `main` 并打版本标签。稳定资格仍要求当前安装制品
完成有用的真实任务，包含独立接受与导出，以及适用的失败、预算耗尽、恢复、
取消、重开、并发、重建和规模门禁。推送 `dev` 不等于稳定发布或包发布。

report107 已证明一条经普通 CLI、真实 provider、双工具续接和不同身份独立接受的有界
研究报告闭环。复杂任务管理、一般研究可靠性、持续运行及其他稳定发布门禁仍开放。
总体路线见 [ROADMAP](../../ROADMAP.md)，发布证据规则见
[release evidence](release-evidence.md)。

离线请求计量切片已完成隔离实现复核，源码3,257项和树外49项集成检查通过，非作者对准确集成范围、源码包与树外来源复核通过，独立98项及树外49项通过，已交付开发分支 `dev` 的 `b9a87cd`。它在单进程内提供并发请求许可、保守输出容量预留、cooperative deadline 和未知 usage 汇总；该b9a独立交付没有接入direct/Runtime transport，当前direct接线另见下文，不能作为持久重启预算或成本优势证据。详见[工作包计划](../plans/provider-request-meter.md)。

受支持的 pure declaration/allowance 局部读取已通过隔离独立验收和原36组/216次 B 矩阵，已与 D0 Asset 测量包通过 root 组合源码3,342项、树外444项、构建与安装smoke。派生索引与签名事实同事务维护，双连接旧投影由 fresh writer 重验；最终非作者组合验收 Passed，稳定资格仍开放。D0 确认 Asset 路径仍有历史相关读取，claim 与其他消费者也继续开放。详见[声明局部读取](../plans/scoped-declaration-allowance.md)与[Asset 基线](../plans/asset-publication-locality-baseline.md)。

本批准确源、矩阵、失败保留和安装来源见[开发验证](../../reports/112-local-allowance-development-2026-10-25.md)与[机器证据](../../reports/data/112-local-allowance-verification.json)。

D1a 的精确 Asset 投影上下文、G0b 的显式 direct provider adapter、D0c观察器及D1b0输出判定已通过单包及35文件组合开发门禁与独立总体验收；[组合报告](../../reports/113-asset-context-direct-transport-development-2026-10-25.md)单列状态。D0c已测真实generic入口并分别绑定旧/新Runtime源。该183d9c8历史增量没有新增真实provider执行或Runtime计量；新进展见下文。

新增输出判定切片已独立接受：FactReducer按实际pending Asset检查业务/creator/independent政策，防止同批工具观察按名称触发错误complete或绕开子任务取消。历史terminal保持；[D1b0计划](../plans/pending-asset-output-policy.md)与[change125](../../changes/125-pending-asset-output-policy.md)限定证据。它与D1a/G0b/D0c已通过本次root组合门禁及独立总体验收；Runtime共享计量仍独立在制。

已交dev183d9c8的35文件组合源码3,532项、树外656项、Ruff/581文件格式、构建和实际安装驱动检查通过；独立整体Passed；最终验收元数据/源码包按同一717文件代码快照轻闭合。direct原生transport是显式可选入口，Runtime计量尚未合入；本批无真实provider/key调用，不宣称公平成本优势或阶段目标完成。

2026-10-26新组合：G0c普通Runtime共享请求计量与D1b1/D1b2批内分派单包独立接受，724文件源码3,602项、Ruff/585格式通过；树外739项、构建与实际安装驱动通过，非作者对准确组合独立整体Passed；最终验收元数据/源码包按同一724文件代码快照轻闭合。辅助usage、epoch及恢复证据不构成持久预算或账单。E0另确认事件捕获仍有全历史读取及T×N关联成本，该dc0cb4f增量尚未合入E1，后续组合见文末。见[当前验证](../../reports/114-runtime-meter-dispatch-development-2026-10-26.md)。

真实测试另绑定旧183d9c8/旧wheel：两次严格有界请求均HTTP200后native parser拒绝，无工具执行或交付；原失败完整保留，第二次仅以安全类型桶/固定源码fence定位，未保存正文或敏感字段。共已知4,232 tokens，不等于已知费用；不能据此宣称兼容通过、一般质量/成本优势或阶段完成。

G0c/批内分派组合已交devdc0cb4f。后续G0b1 native DTO适配与E1捕获局部头索引的准确16文件开发组合已获非作者整体接受：源码3,635项、585文件格式、actual wheel/sdist通过；树外首次816通过/1失败，保留失败后一次完整观察复跑817通过。独立328通过/8native NotRun、十二次实际安装capture等价；Root源码和树外实际执行native资格与独立NotRun分别记录。原失败根expanded、尚无报告、另有ready工具工作，异常原因仍未知，不因后续通过关闭。E1仍读取完整前缀，活跃工作局部性和新真实provider资格继续开放。见[本批验证](../../reports/115-native-capture-development-2026-10-26.md)。

G0d真实Runtime固定三次日程现已完成证据复核，绑定已交`6b3d7cd`/旧wheel，合格交付0/3。22次HTTP200与117,680已报告tokens仅证明计量传输；前两次输出截断后的恢复资金拒绝、第三次meter耗尽与三个实际失败恢复均保留，费用未知。原始finish reason及逐trial导入来源未保存的限制明确。G0e1固定类别显示和G0e2有界Researcher配置已通过准确组合源码3,639项、树外44模块882项与构建检查，非作者独立152项和准确组合总体接受，最终元数据/源码包按相同代码轻闭合；该配置没有新真实provider资格，恢复资金和重复恢复策略不变。长期活跃局部性仍待完成测量，旧B4未知slot事件继续开放。见[report116](../../reports/116-budget-profile-development-2026-10-26.md)。

G0e3新候选固定三次真实运行的合格交付仍0/3；02/03报告及双观察证据正确，但Verifier描述NonJSON，尚无独立接受。23次HTTP200和120,777已报告tokens不等于交付或账单；三份副本严格重建相符。G0f1修复当前编译器失败子任务与示例适配器的输出命名不匹配，独立组件六项离线、八个guard和三个真实Redis/脚本provider CLI回归通过；原验收仍失败，未放宽政策。Root组合源码3,645项、树外45模块888项与构建已通过，非作者独立101项NoNet及准确十文件组合总体接受，最终六份验收元数据与源码包按同一代码独立轻闭合，见[report117](../../reports/117-verifier-failure-development-2026-10-27.md)。

E2a对旧B4的H0/16/64夹具测量完整资格NotPassed：H16留下未领取ready任务，H64因高开销日志落盘的磁盘余量被停止，耗时不能代表正常性能。三个等水位同步窗口直接证明仍各读6/406/1606条完整历史前缀；有界观测器和热路径活跃局部化仍待推进，不能宣称历史增长问题已经解决。原证据与无损归档均独立复核，见[report118](../../reports/118-configured-history-counts-2026-10-27.md)。后续人工辅助接受仍仅为计划；旧B4未知slot与额外lineage诊断保持开放，阶段未完成。
