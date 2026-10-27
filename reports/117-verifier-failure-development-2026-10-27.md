# 真实校准运行与失败方法适配开发证据

本批公开新候选的三次固定真实运行结果，并修复示例验收器不能执行当前编译器失败子任务的缺陷。**真实合格交付仍为0/3，阶段目标未完成。** 两份数值正确但未经独立接受的报告不能计为合格交付；本次修复也不改写原运行。

## 三次真实运行与已确认的阻碍

2026-10-26执行的G0e3绑定已交开发候选 `1055c0e95a504ac8e6e44a3b1d7a6fb8e83e4506`，wheel SHA-256 `7e56c734b6894988ac08cbd63362768cd1581d30782a887a531e03ef5c750f3c`。每次新Store与独立计量epoch，最多8次物理admission、65,536输出预留、180秒cooperative deadline；单请求8,192输出上限、60秒超时、零重试。请求别名为`deepseek-flash`。这组候选同时改变研究配置、Skill和安全诊断，不能隔离归因thinking设置。它与[report116](116-budget-profile-development-2026-10-26.md)中的旧G0d三次失败分开，不替换样本或合并计算稳定成功率。

| 运行 | HTTP 200 | 已报告prompt / completion / total tokens | CLI结果 | 业务结果 |
| --- | ---: | ---: | --- | --- |
| 01 | 8 | 37,120 / 3,861 / 40,981 | exit1，timed_out，180.834s | 无报告；三次已发布且实际领取的恢复，其中两次失败、一次展开后耗尽请求预算 |
| 02 | 8 | 35,549 / 6,951 / 42,500 | exit1，timed_out，180.720s | 报告及完整双观察证据存在，精确oracle通过；无接受、qualification或receipt |
| 03 | 7 | 30,218 / 7,078 / 37,296 | exit1，timed_out，180.860s | 报告及完整双观察证据存在，精确oracle通过；无接受、qualification或receipt |

三次验收任务的description都是NonJSON字符串，不是可解析但字段错误的JSON，也不是Python字典字面量。现有证据支持Planner/Skill协议遵从问题；没有观测原蓝图description类型，不能据此认定运行时把字典错误转为字符串。三次均未产生任何attestation、qualification或receipt，受保护导出正确拒绝。

02/03的准确报告字节SHA-256均为 `5d81e61e72291793e2f68da70ba0af6e374c700a1c45db5cc75dfec08bfb4577`，三条合成样本均withinTolerance=true，完整复制证据与两个原工具观察的精确来源成立。02还存在一次实际领取并展开的恢复，它产出正确报告；不能把“存在恢复任务”一概说成成功或失败。两次随后请求1单位恢复资金、可用0，被原allowance owner正确拒绝。通用unavailable事实没有typed reason，资金原因来自对应精确拒绝与trace。

另有一个独立可复现的示例缺陷：当前委派编译器生成`failure_result_<description-hash>`，旧CalibrationVerifier只接受`_fail_result_`前缀，导致合法的系统失败子任务产生`invocation_failure`。这是本批[change132](../changes/132-thermal-calibration-fail-method-adapter.md)修复范围，不能代替对NonJSON主问题的修复。

## 计量、只读检查与限制

23个许可均完整记录一次admitted→transport→response→finalized；HTTP200、非late、结果eligible，无pending许可。已报告102,887 prompt＋17,890 completion＝120,777 tokens；输出保守预留188,416不是生成量或账单。费用、不可变模型revision及cache/reasoning拆分未知。33个严格辅助capture complete。没有观察到本阶段输出截断或旧slot异常，不能据此认定历史问题已解决。

34个任务中11个未完成任务都有owner提供的blocker。独立检查先记录DB/WAL/SHM及日志的存在性和字节，复制闭合Store，仅打开副本；03原有WAL与SHM也保留。三份strict rebuild及reopen摘要一致，包括Worker actor/key绑定，原文件相对该审计基线不变。实际preflight的安装来源及冻结字节有记录；逐trial同进程模块来源与原始finish reason未保存，仍为NotObserved。父启动器exit0只表示三次日程结束。

独立真实证据审计SHA-256为 `1f41de906ef30e02b9360084fef1150c20a30d39fb022230bf84e61e0b251534`；逐项汇总见[机器证据](data/117-verifier-failure-verification.json)。不公开正文、prompt、reasoning、凭据或原始Store。

## 适配器组件验证及组合门禁

Luna作者的六项离线测试构造真实TaskDelegationPlugin产生的Contract，再调用生产CalibrationVerifier，保留旧适配器红灯。独立非作者复验六项，另作八项guard probe。新适配器严格核对系统来源、fail方法、非空reason、精确单一输出及内外parent绑定，只兼容明确的旧示例输入形状。没有plugin:fail时继续不介入；未放宽通用内核、JSON、输入、oracle或政策。

三个普通CLI夹具测试在真实专用Redis上独立通过，耗时19.15秒；Planner/Researcher使用脚本夹具，重现用例使用生产验收器，没有真实模型请求。精确签名publication证明失败子任务正常领取、提交其声明输出并complete，自己的invocation_failure为0；原验收任务仍failed，根仍expanded，接受三项计数全0。三个副本strict rebuild/reopen相符。最初沙箱拒绝及自动审批因非空专用DB拒绝均保留；对新Genesis作用域、删除/重命名范围作只读证明后，同一命令获准执行，没有换资源绕过、flush或广域清理，资源已释放。

补充检查最初错误地要求所有故意失败夹具lineage无issue，在预算耗尽夹具停于既有`declaration_candidate_rejected_unverified`。保留失败，改为如实记录该诊断；适配器重现用例lineage干净。没有改代码、重跑native或宣称这个额外诊断已解决。独立组件接受摘要 `33d7634fe24149a991cda29cc7b01ae98fcf7170f7b77a8015c23387765429a2`。

Root准确725文件代码/夹具摘要 `ef450462520397da7c0915713a9d90c4205c2114ea255565c77de6f4de483014`，源码**3,645 passed**、0skip、6条既有warning，234.75秒；Ruff及586文件格式通过。树外45模块实际**888 passed**，106.09秒。wheel的236模块逐字节与安装target一致，子进程src来自实际wheel，示例来自实际源码包；其722个payload准确、336份测试与私有材料排除。wheel与先前B5相同，因为本次行为变化在源码包中的示例适配器，不能仅用相同wheel证明新示例已验证。实际安装用源码包摘要 `d3c66fb16051c988d7e8ee12bea2800298f6c85b2a933be433536f43f006c44b`；后续元数据更新须重新绑定源码包。

非作者对准确十文件开发组合整体接受，审查摘要 `cd5cf016a3072f15edf755fda53b87b770a20f96ec89d588116612281dffb111`，绑定1,058文件公开快照 `fa169cdddaa4074390462506d5d99fa22ce09b5d58b90d7ef0ad2b10b9d8827d`、十文件改动 `80aaf1f80c1ea1a93f3f45267d5df76bcd2d5a1196c7fd7a970d7d2f7a10c943` 及相同725文件代码。其独立NoNet复验101项（adapter6、oracle46、治理49）通过，Ruff/格式、79链接及根指南29链接/68路径检查通过；实际安装236模块及112模块导入来源检查通过。两次审计设置/文件名发现命令失败保留，不是候选缺陷。独立本轮native为NotRun，没有租约，不替代Root实际源码/安装或组件native证据。

已审源码包摘要 `495c0113c573a8a31a422492ade9ab44077892f3ab602c6cbb147ed2d47140f3`；它与实际安装用包仅六份Root元数据不同，运行时与示例逐字节相同。最终仅更新这六份验收元数据，再独立轻闭合源码包；最终公开清单与源码包由开发交付记录另绑定，wheel/代码不变。阶段与稳定资格仍未通过。后续人工辅助验收仍仅为计划，未执行；不新增真实模型调用，不把原三次结果重标为成功。
