# Runtime 真实失败证据与研究配置开发增量

本批保留并公开汇总旧候选三次真实 Runtime 运行的失败证据，修正固定请求预算失败类别的显示，并为校准示例准备一个有界研究配置。阶段目标仍未完成；新配置没有真实 provider 资格，不能把配置修改写成原失败已修复。

## 三次真实运行：0/3 合格交付

实际运行绑定 `dev` 的 `6b3d7cd584b229e7a62af4e757361fe999669a8e`，wheel SHA-256 `8619116fd014a6d3ce9347821c3ddd9f95413fb585d1ba2c72488963589ff92d`。每次使用新 Store、独立进程计量 epoch、请求别名 `deepseek-flash`，共享最多8次物理请求、65,536输出容量预留、180秒 cooperative deadline，单请求输出上限8,192、超时60秒、零重试。因果任务 allowance 与 provider 请求预算分别计算。

| 运行 | 物理 HTTP 200 | 已报告 prompt / completion / total tokens | CLI 结果 | 业务结果 |
| --- | ---: | ---: | --- | --- |
| 01 | 7 | 26,461 / 12,083 / 38,544 | exit1，timed_out，180.757s | 根 expanded，报告/接受/qualification均0 |
| 02 | 7 | 26,447 / 12,707 / 39,154 | exit1，timed_out，180.782s | 根 expanded，报告/接受/qualification均0 |
| 03 | 8 | 27,671 / 12,311 / 39,982 | exit1，timed_out，180.902s | 根 expanded，报告/接受/qualification均0 |

固定三次日程已结束，父启动器 exit0只表示日程执行完成。原样本没有替换，没有追加请求直至成功。较早旧制品的两次 Direct parser失败属于另一组证据，不进入本表分母。

前两次在 Researcher continuation 留下 `worker_error:output_truncated`，最后一次响应各报告8,192 completion tokens。冻结源码将该类别绑定到 `finish_reason=length` 的检查，因此这里只能作源码 fence 推断；原始 finish reason 没有保存，不能补写为原始观测，也不知道 reasoning/answer 分占多少。

这两次均没有已提交恢复 publication。恢复 Candidate 被现有因果 allowance owner 正确拒绝：分别请求6/10，可用0。通用 `recovery_unavailable` 记录没有显式原因字段；资金拒绝来自精确 rejection/trace 证明。不得借用别处余额、补授额或重置 claim 来改写这些失败。

第三次在8次物理请求用尽后留下四个 `worker_error:provider_request_budget_exhausted`。旧 CLI 把它们显示为 unknown，属于已确认的安全类别映射遗漏。三个普通恢复任务确实新建、领取、执行并再次失败，各新增 HTTP 请求为0；最后到达恢复深度3。它们不是成功恢复，也不是四次未知模型错误。该类别本身只是受限的已报告失败分类，物理 admission/transport 结论另由本次 meter 与精确 claim 证据支持。

## 计量、只读复核及限制

22个许可均有一次 admitted→transport→response→finalized，三个epoch独立；所有响应HTTP200，late=false、resultEligible=true且无pending许可。这些字段只说明计量状态，不证明交付或恰好一次外部效果。已报告总量为80,579 prompt＋37,101 completion＝117,680 tokens；保守输出预留180,224不等于生成量或账单。费用、不可变模型revision、cache/reasoning拆分未知。没有报告可交给oracle，正确性为 NotEvaluated_NoReport。

35个普通任务中13个未完成任务都有现有owner投影出的blocker，三个verifier均缺报告与完整业务证据。未发现无blocker的遗留义务。副本strict rebuild均保持摘要。非作者审计先记录原DB/WAL/SHM/log的存在性与字节，再复制已关闭Store，仅打开副本，原文件相对于该审计基线未变。Root首次诊断之前的WAL存在性未记录，仍为Unknown。启动器保留preflight来源与冻结字节，但没有持久化每次子运行的模块来源，不能事后补造历史导入轨迹。

独立真实证据审计 SHA-256 为 `da8f2d5221acd782fefa07ab007b302c26617f6bb173227c026c2ca488994b82`；逐运行安全汇总及门禁状态见[机器证据](data/116-budget-profile-verification.json)。不发布正文、prompt、reasoning、凭据或原始Store。B4先前树外检查中的间歇slot异常继续开放，本次未观察同类异常不构成原因解释或修复。

## 当前开发切片与待验收范围

[change130](../changes/130-provider-request-budget-failure-diagnostic.md)只在现有严格proof及有限allowlist中加入固定类别，不改变失败事实、claim、请求预算或恢复政策。[change131](../changes/131-bounded-calibration-research-profile.md)使G0c Researcher与Planner均关闭thinking，并绑定新profile身份；完整双工具观察、业务证据、独立oracle和普通runtime任务保持。Skill给出有条件的资源规划参考，不规定领域DAG，不增加资金或恢复许可。原预设与G0c计量预设分开描述。

thinking配置是一项待测假设，未证明其为历史截断原因或修复。因果恢复资金、同进程预算耗尽后重复恢复的策略、行动指引、长期活跃局部性、实际质量与总成本仍须各自验证。本开发组合的源码、树外安装检查及非作者总体验收已通过。没有新增真实provider运行或稳定发布。

## 组合开发验证

两项切片均经非作者组件接受。诊断切片独立116项；配置切片独立43项离线、5项真实Redis/fixture provider普通CLI和49项治理通过。配置复核首次native检查被沙箱拒绝本地连接，失败保留，允许同一专用地址后通过；没有替换Redis实现。作者共享Skill范围、测试替身过宽及摘要文件别名等修正均保留原记录。配置复核的成功Store额外检查只证明主DB文件字节不变，未记录WAL/SHM基线，不能冒充完整多文件不变性。

Root准确724文件代码/夹具摘要 `460fbb79f9efe5fa901f512ab4c09dc2cd328e44ec40c62e4fa289bf7c3e366e`，源码3,639项通过、0skip、6条既有warning；Ruff及585文件格式通过。实际wheel SHA-256 `7e56c734b6894988ac08cbd63362768cd1581d30782a887a531e03ef5c750f3c` 的236模块与源码和树外安装逐字节一致。源码包有717个准确payload文件，测试和私有材料排除。树外44模块结果：**882 passed in 106.05s (0:01:46)**；主进程使用真实安装target，子进程src逐字节来自该wheel，examples来自实际源码包。检查使用专用Redis和fixture provider，没有用户凭据或实际模型请求。

非作者独立整体接受准确17文件开发组合。审查报告摘要为 `ca530d5a988ffcebf29522b3db3edf5246381bc040dc683d3a9c3657679f63b6`，绑定1,052文件公开快照 `b038b7ed9fb6c7123ec5af8993bd3f0479ab746625d115820bd9ecb20582505d`、17文件改动 `5ef71b0630f4c84ee0a420c2a70ad7ea5b0ed1bee93fdb5840da01b856f069c7` 和相同724文件代码。其直接独立152项离线检查含49项治理通过，实际安装导入probe通过；独立native Redis为NotRun，没有租约，不冒充Root已实际执行的源码及树外native证据。公开逐运行数组逐项等于原独立G0d安全汇总，公开隐私与145处改动文档链接、根指南29链接/68路径检查通过。

已审源码包 SHA-256 `9439ff8270eafebf2fd284c869297cbbe7bb4e23d6e09ef662b48c8a988717f3` 含717个准确payload，335份测试排除。最终只更新四份验收元数据，再按同一代码快照构建与独立核对源码包；最终清单和源码包身份由开发交付记录另绑定，wheel不变。新配置仍无实际provider执行。不将组件检查、全套测试或fake CLI结果当作真实交付、故障恢复资金修复、长期规模资格或全阶段完成。
