# Report 107：CLI 动态执行开发复核

日期：2026-10-23。基线：`825b4bd210b4ca62ae1b8f7cebacdc555cd6491b`。
状态：有界目标、最终源码与安装组合已通过独立总体复核；待 dev 推送。

## 范围与判定

本次复核原始独立 107 报告，而非把报告当作实现方案。报告为 759 行、48,450 bytes，
SHA-256 `cb3640a3a207b829b38cc1af7b2bccf314448652742ae6f1324ce04fa1a4f4b3`。
对照现行知识库的任务执行与状态模型、规划方法与执行者选择、阶段目标与验收，
再检查当前源码和正式 CLI。逐项判定与依赖见
[开发计划](../docs/plans/report107-cli-end-to-end.md)。采用上游 development-workflow
`c626e75` 的两轮审计，通过[本地适配](../docs/development-workflow-adapter.md)执行。

项目定位保持：Zero Trust agent runtime for auditable AI-assisted engineering and research.
规划、工具、续接和恢复仍是普通任务；SQLite commitment 是唯一 durable 写入边界，
Redis 仍为必需协作基础设施。没有示例调度器或手写领域 DAG。

原报告中“所有多跳必失败”“没有恢复”“不能声明任务间关系”并非当前实现的普遍事实。
默认输出是授权断言，tool_scope 是权限，不能单独证明业务质量或要求工具必调。
任意 JSON 中 success=false 的含义仍由适配器 schema/异常表达，不增加内核猜测。
本轮在组合验证中另确认了输入漂移、观察丢失、恢复 wire 比较和并发 relay 等实际缺陷。

## 交付切片与审查职责

| 切片 | 实际改变 | 作者 / 独立审查 |
| --- | --- | --- |
| [105](../changes/105-structured-compile-failure-diagnostics.md) | 既存未提交编译拒绝诊断，保留有界结构字段到普通恢复上下文 | 既存实现 / Luna workflow_plan |
| [106](../changes/106-complete-tool-descriptor-cli.md) | CLI 完整 ToolSpec、返回实际提交 ID、同名歧义拒绝 | Luna recheck_tools / 主协调者 |
| [107](../changes/107-planned-tool-budget-default.md) | 工具子任务省略预算时缺省 2，显式不足和父余额不足仍拒绝 | Luna recheck_planning / Luna recheck_tools、主协调者 |
| [108](../changes/108-disclosed-tool-descriptor-selection.md) | 仅在已披露描述符中唯一精确匹配，不按列表顺序挑版本 | Luna recheck_planning / 主协调者 |
| [109](../changes/109-configured-input-pins.md) | 配置根任务绑定本次已提交输入 ID，resume 不刷新输入 | Luna workflow_plan / Luna recheck_planning、主协调者 |
| [110](../changes/110-continuation-input-and-observation-retention.md) | 新续接保留原输入及有序累积观察；旧合同/已准备上下文不重写 | 主协调者 / Luna workflow_plan 方案、integration_review 实现 |
| [111](../changes/111-recovery-binding-wire-comparison.md) | v6 恢复精确比较仅规范化 tuple/list wire 表示 | Luna workflow_plan / 主协调者、integration_review |
| [112](../changes/112-concurrent-relay-source-refresh.md) | 同 Store 前缀保持下有界重取同步快照；events 只重试既有冻结 proof | Luna workflow_plan / 主协调者、integration_review |
| [113](../changes/113-reuse-task-view-projection-snapshot-for-descendant-risks.md) | 聚合后代待接受风险复用 query projection context | Luna recheck_planning / 主协调者、integration_review |
| [114](../changes/114-isolate-planning-stage-roles-from-method-children.md) | 活动规划角色与工具/失败子任务隔离，旧精确 source 保持可读 | Luna recheck_tools / 主协调者、integration_review |
| [115](../changes/115-thaw-nested-json-action-payloads.md) | 严格 JSON 动作验证后递归恢复 wire 容器，合法嵌套 payload 不再误拒 | Luna recheck_planning / 主协调者、integration_review |
| [116](../changes/116-refresh-redis-source-after-watch-conflict.md) | Watch 冲突后同 Store 严格前进及完整前缀重取，保留原三次尝试预算 | Luna recheck_tools / 主协调者、integration_review |
| [117](../changes/117-snapshot-recovery-source-preparation.md) | 同一 SQLite snapshot 准备来源及祖先证明，关闭副本后仍向 live Store 签名发布 | Luna recheck_tools / 主协调者、integration_review |
| 测试隔离修复 | query 测试改用 pytest 自动撤销 fixture，同序 CLI 导出不再读取遗留 Store | Luna recheck_planning / integration_review、主协调者 |
| CLI 研究样例与夹具 | 输入、Skill、根 Contract、Worker/工具适配；双观察、不同身份接受和精确导出 | 主协调者样例、Luna recheck_tools 夹具 / integration_review |

角色是实际职责，作者自检没有称作独立审查。integration_review 未参与实现，负责完整组合
和最终总体判断；其证据按精确快照绑定。简单固定计划与实现由 Luna 分担，文件所有权和
依赖明确；组合暴露的问题进入新的小切片，早期通过不会用作后来代码的证据。
历史 [105 原证据](report107-codex-review-develop-2026-10-23.md)及其 JSON 保留原结论，
本报告补充其后审查与集成事实。

## 确定性与制品证据

最终 `src/`、`tests/`、`examples/` 的 614 文件清单 SHA-256 为
`fd51a47d9a805a77d82489b329e138578456f63020db636ab164ccc7111a2d3c`，
逐文件摘要和命令结果见[机器证据](data/107-cli-end-to-end-verification.json)。
最终测试树源码全套 3,039 passed、0 skipped、6 条既有依赖警告，123.78s。
change115 快照的完整测试为 3,033 passed、0 skipped、6 条既有依赖警告，127.93s；
安装制品受影响测试 344 passed、52.00s。这些历史绿灯未用于遮盖其后集成失败。

最终集成 E 的一次故障用例失败（50 passed、1 failed），独占 Redis 的诊断运行第 8 轮
也返回同步 pending。前者在合法工具 schema 失败后出现 slot error，后者在协调同步路径
返回 pending；原始异常栈不足，不能确定两者的完整原因，也不能沿用 live05 的共享清库解释。
失败退出码、Store 和诊断输出均保留。

另外建立了确定性真实签名双连接竞争：两次普通 Asset 提交并 relay，使旧循环在第 1、3 次
EXEC 冲突，中间第 2 次只发现 Redis ahead，耗尽三次预算。change116 在 WatchError 后，
有剩余尝试才重取同一 Store；严格前进且原完整前缀保持才使用新 capture。因此相同测试
在第 1、2 次冲突后第 3 次成功，不增加重试次数、不忽略同步异常。非前进、前缀分叉及
持续三次冲突均有独立边界测试，最后一次不额外捕获。独立审查者复现旧方法红灯与当前
方法绿灯，相关 Redis 两模块及 E 共 36 passed、18.39s。这个差分证明该机制缺陷和修复，
不倒推原间歇失败的未知异常栈；持续竞争仍可能明确返回 pending。

change116 完整测试得到 3,036 passed、1 failed、6 条警告，120.76s；故障 E 用例
报 task-view query Store revision has changed。这个新反例与同步 pending 分开调查。
同快照的独占 Redis 有界 E 十轮共 20 passed（185.491s），不能覆盖完整测试红例。
change117 以同一拥有生命周期的 SQLite snapshot 证明 history/source/ancestor，
关闭副本并移除 query 引用后仍由原 live runtime/publisher 发布。合法签名双连接差分
复现旧字面异常，新实现通过；真 grant 错配仍由 source mismatch fence 拒绝且不写入。
副本成功/异常关闭，snapshot 失败不降级。通用 query 严格匹配未改。作者相关 58 项通过，
change117 源码全套 3,039 passed（127.18s），独立相关 58 项（3.76s）、
CLI 两例（19.28s）及有界 E 五轮十例（96.590s）通过。安装组合 332 passed、2 failed
（52.29s）：两次 run 均已完成，但后续导出读到了 query 测试遗留的 board Store。
既有 test_task_view_query 自制 monkeypatch 不撤销全局 CLI 替换；当前按相同测试顺序
修复并复验隔离，不调整顺序掩盖错误。作者安装同序 5 项通过（18.43s），
独立同序 5 项通过（18.39s），原三个 strict query 断言保持。生产 wheel 未因此改变。
最终测试树全套 3,039 项通过，原顺序安装组合 **334 passed、52.79s**。
此前失败的 stdout/stderr/退出码完整保留；非作者已恢复本轮有界目标与开发交付的总体通过结论。
Ruff 检查及 547 文件格式检查通过。新 wheel SHA-256 为
`9859ef509773c722c70d1d9750415e41c8e35b6838a07ff97c470316f7373dbd`，
235 个模块与冻结源码及树外安装逐字节一致。与 live08/09 的 change115 制品比较，
运行时代码仅 Redis 同步及恢复来源准备改变；规划、工具、输出接受与示例资产均保持相同。
真实运行仍归属其实际旧制品，未重标为新 wheel 的 provider 运行。

首次默认隔离 `python -m build` 因网络代理不能下载 hatchling 失败，保留退出码。
使用已有依赖的 `python -m build --no-isolation` 成功生成 wheel 和 sdist；最终文档后
再次生成公开源包并检查无私有工作区、凭据、原始 Store 或 Git 元数据。
源包自身校验值保存在包外，避免把自己的摘要写进被打包证据造成自引用。

正式 CLI 重开 audit、精确导出与严格副本重建均通过。正常例 284 条事实，语义摘要
`dbe626ca64061deda74693e1d067fb38e5aa2dab3755cee6d8928175fc94af8f`；
故障恢复例 328 条事实，摘要
`ba49efffacd313995738ae071fe53416b5b416caf9a0e4802aaa38ab69d755bf`。
两次重建前后语义摘要及 RuntimeRecord 不变；Worker claim/registration 显示时间的物理表
可能规范化，不声称重建文件逐字节相同。非作者的只读 snapshot 审计确认原 Store 字节未变。
本地 audit 的内容地址/物化一致性检查不等于外部 checkpoint 或签名真实性证明。

## 真实 deepseek-flash 执行与失败

测试凭据只经环境变量传给 provider，未写入输入、报告或源文件。只读连通性请求返回
HTTP 200，不能算业务验收。以下每轮有独立 Store，stdout/stderr、退出码、提交记录和
phase-specific diagnostics 均私有保留；没有删除失败后只报告成功。

| 运行 | 配置/制品 | 可观察结果 |
| --- | --- | --- |
| live01 | 源码，thinking disabled | summarize 请求多余 unit，被输入 schema 拒绝；普通恢复再次失败，超时 exit 1，无根资格。 |
| live02 | 源码，native scoped tools | 只提交一个观察及部分证据；报告任务未限制路由，被 verifier 生产，同身份 attestation 正确拒绝。超时 exit 1，无根资格。 |
| live03 | 上述早期安装 wheel，明确 research/verification 路由 | 畸形 dependencies 输出经普通 recovery 实际恢复；仍漏转换观察，attestation payload 缺 evidence IDs，政策拒绝。超时 exit 1，无根资格。 |
| live04 | 同 wheel，thinking enabled/max8192、verifier JSON mode | dependencies 输出达到上限；恢复后 compile 调工具，工具实际成功却因继承 compile 标签反复被当 blueprint 拒绝，恢复深度耗尽。超时 exit 1，无根资格。另只读 task list 暴露 113 的 facade 缺口。 |
| live05 | 113/114 早期安装 wheel，新增独立 planner | draft 后 sloterror；无资格。与会 flushdb 的全套共用 Redis DB13，原异常栈/返回码未捕获，产品归因未确定。 |
| live05-resume | 同根任务，私有仅记录异常栈的包装 | 动态四项业务工作、两个工具与正确报告；verifier 格式失败及普通 receipt 恢复实际执行，又因 staged relay 非1退出。与第二次全套 flushdb 时间重叠，原 Lua 码未知，无资格。 |
| live06 | 114 最终安装 wheel，专用 Redis 实例 | 动态五项业务工作、两个真实观察和正确报告；verifier 的 JSON action 被拒，generic 恢复只完成 receipt，没有 attestation/qualification。超时 exit 1，无 sloterror。 |
| live07 | 同 wheel，子任务显式保留 Skill 和完整 JSON attest 示例 | 过程与动作说明已披露，仍被报 JSON action 失败，超时 exit 1，无资格；随后合法示例直接复现 change115 的 mappingproxy 序列化缺陷。 |
| live08 | change115 安装 wheel、最终 Skill、专用实例，普通 CLI | 完整动态计划、两个工具观察、报告、不同身份 exact attest/qualify、根 complete，exit 0，无 Worker 错误；未由操作者补验收。 |
| live09 | 同制品与资产，仅一次显式工具失败注入 | 首 summary 返回 success=false，schema 拒绝并释放原 claim；新的普通 exact-method recovery 实际执行成功，后续双观察、报告、不同身份接受及根 complete，exit 0。首次 failure 仍在结果中。 |

live04 和 change115 证明原来对“后续失败均是模型问题”的归因不充分：阶段控制标签传播、
合法嵌套 JSON action 序列化均是真实适配缺陷。后续需保持普通 compile 的授权工具路径，不能为让测试通过而一律禁止工具、
放宽输出/证据政策，或由审计代码补写完成。

共享 Redis 的配置是本次验证隔离错误，不作为运行时纯并发缺陷定论。全套中的两个原生
query 用例会清空测试 DB，live05 两次运行与其共用 DB13；准确清库时刻和原次 Lua 返回码
未记录。专用实例中用 320 条已提交事实，在 stage 写后、swap 前注入一次 flushdb，得到
Lua `-4`、sync_contention，live stream 不存在，复现了兼容的干扰机制；这不能倒填原次
原因，也不替代 change112 的签名并发证明。最初 95 条事实的前置实验未进入 staged 路径，
同样保留。live06–09 使用独占实例，与安装后测试的 Redis 完全分开。

严格 JSON action 修复仅对已验证 payload 做 deep_thaw；不修复任意畸形 JSON，不代替
插件语义、权限或接受检查。旧 provider raw 未保留，合法示例的本地差分复现证明适配器
缺陷存在，不等于原每个失败响应的内容已知。generic output repair 只产 receipt 时没有
qualify 根任务；不能把执行完恢复当作业务已恢复。

## 正常与故障闭环的精确结果

live08 根任务为
`task:v6:e0dddfbefbf6e88c97dbe4f9dec9f2db743f122a2cc7da5caedb3ba1c8289bf5`。
模型创建普通 draft/dependencies/compile 与三项业务工作，消费 summary→convert 两个
真实观察，报告 count=4、15°C、59°F。`measurement-researcher` 生产报告，
`measurement-verifier` 绑定原 sample 和同一精确 report 接受；共有 11 个终态任务。
根完成后 expanded compiler 保留 parent_complete 取消栅栏，形成 1 条 rejection trace、
0 条 Candidate rejection record；不是“没有任何拒绝痕迹”。
435 字节精确导出 SHA-256 为
`ebeb32c099442e6b8ead40c396ca24a1c9290f5a07b966e18fb0a1942469a239`。

live09 根任务为
`task:v6:9773e631354d707fe6042202b971097bf545b360d60b7a297c60cebca6998aba`。
测试 marker 仅模拟适配器首个后端失败，不具任务权威。唯一 invocation failure 为
`tool_schema_invalid`；原失败终态与释放 claim 保留。exact failure ID 对应新的普通恢复
Contract，恢复实际领取、执行 ToolWorker 并提交观察，再由模型续接和不同身份接受。
非作者核对 summary 观察的 producer 正是该 recovery Contract，不能把 scheduled 当作执行。
435 字节导出 SHA-256 为
`aa6bc0d1a778c08812eb31b9f411c8cf828fc442538c4add4003327847af3d28`。
该成功结果仍带 1 条历史 Worker failure，未知工具 token usage 保持 null，不记作零。

## 限制与后续标准

确定性夹具证明特定机制，不证明模型自然会遵循每项过程义务。两身份使用同模型不等于
独立的人或独立知识来源。历史空输入续接不会自动修复；需明确新授权工作。一般任务质量、
CatAgent3、长跑、规模、所有 CLI 命令一致性、完整会话产品及稳定发布资格仍按开发状态
分别开放。未知 usage 不记作零；因果额度不代表 token 账单。

本轮有界目标已有正常和故障恢复的真实完成、独立事实审计、导出和重建证据。
先前总体通过结论因新反例暂停；116/117 机制修复、测试隔离修复及最终完整证据经
非作者 integration_review 复核后，已恢复本轮有界目标和 dev 开发交付的通过结论。
开发交付不等于进入 main、打 tag 或发布软件包。
