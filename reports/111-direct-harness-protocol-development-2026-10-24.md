# 直接工具循环与 CLI 失败边界：开发验证

日期：2026-10-24。集成基线 `2d8d4b626d4640ce948c7790a08c3e349f480d3b`。
作者 Luna recheck_tools，非作者 integration_review 验收 F1b 单包，
非作者 Luna recheck_planning 验收窄测试维护、F1c 与最终组合，root 集成。
[工作包计划](../docs/plans/thermal-calibration-direct-harness.md)与
[机器证据](data/111-direct-harness-protocol-verification.json)界定证据范围。

## 可运行的直接臂

新增的[离线 CLI](../examples/thermal_calibration/direct-README.md)使用显式 scripted client
驱动原生工具消息循环，调用未修改的校准工具和同一个 Fraction oracle。它不创建
runtime Contract、Candidate、claim、调度或接受事实，不依赖 Redis，不发送 provider 请求。
模型消息子集、顺序单工具调用、参数/final JSON、限额和 cooperative deadline 均有明确契约；
deadline 在 client/工具边界检查，不宣称抢占同步处理器。

每次运行排他创建目录，保留逐次事件、stdout/stderr、结果 manifest。只有明确注入的
transient 工具异常回送模型；未知异常按安全类别记录后终止。fixture 是预排协议输入，
不是动态模型规划或统计质量样本。usage、费用与人工成本未知时保留 null，不能填零。

## 独立检查与保留的失败

首轮七文件摘要 `f8ec6cda4d7f065dc3d72ed6b4710b9c4e43a14d07f060673a476de7fc39bd03`，
独立66项虽通过，独立对抗探针仍发现：初始化异常没有完整失败制品且流未关闭；失败
manifest 的诊断字段为空；JSON 数字 `1e999` 变为非有限值并可到达宽松 handler；
客户端误改嵌套参数会污染历史消息。首轮未通过，原证据保留。

作者增加回归并修复：整个已分配运行目录的初始化/执行进入失败保存边界；准确
code/type/phase，缺失输入哈希可为 null；严格 float 解析拒绝溢出；传给 client 的消息和
工具定义做深复制。没有改 A 的工具、oracle、F1a、runtime、依赖或 provider 实现。

修复后的七文件摘要 `07b4f3e63facbe814d8da640351681197dabc43871548d2ae57d10c9a1af2762`，
独立73项、Ruff及原对抗探针通过。两个 eval 正常运行和一次 transient 运行在实际
sdist 解出的 examples 上通过，共用实际安装的 wheel；三次均 oracle 合格，故障轮保留
一次失败及两次成功工具调用。wheel 的235个模块与源码/安装字节相同，examples
不在 wheel，tests 不在 sdist；不把验证 bundle 伪称已安装接口。

作者初始 red、配置/命令失败和隔离构建依赖获取失败分别保留；既有依赖的
`--no-isolation` 构建成功。一轮早期失败只有当时工具输出、没有独立原始文件，
该限制明确保留，不补造诊断。单包检查不代替最终组合门禁。

树外整合另发现测试副本漏复制 examples 的 package initializer，竞争 bundle 因而仍提供
未删除的 fixture，导致296 passed/1 failed。只修测试布置并验证实际 import 来源，
产品代码未改；非作者树外精确回归1项和完整73项通过。最终 F1b 七文件摘要为
`2b46ca0742c755d8669a6276f2c1d2a2abfcad1983d3d585168ee2525f401563`。

## CLI 三类失败

[F1c 计划](../docs/plans/calibration-cli-failure-boundaries.md)只新增两份测试/fixture。
畸形 raw action 产生精确 candidate_encoding_failure 与 root failed，新 recovery
已发布但未领取，不称恢复完成。缺一条 copied observation 时，两个真实工具观察仍在，
报告/evidence 实际提交，原 verifier 领取后失败，root expanded 且显式 descendant blocker，
没有精确报告的 qualification。child budget=2 的真实资金链用尽后，后继请求1/available0
被拒；同一 continuation source 有唯一 failed 终态，不能把整个 root 余额说成零。
三者均拒绝结果导出且无目的文件，重开/只读检查保持状态与原 Store。

非作者独立运行 F1c/F1a 组合9项通过；随后只增加精确 source failed 断言，独立重跑该
预算用例1项通过。两种快照分别保留，旧9项不冒充新断言证据。最终 F1c 两文件摘要为
`6b757f366cb334ddf22f119fb52e780739287be81a42c6a4c2298889e0428e3a`。
fixture 仅响应普通 Worker action，配置替换全部模型 Worker，并有禁止 provider 构造的
测试 guard；没有领域调度器、直接事实写入或 claim reset。failure_result 普通诊断任务
的完成不代表原失败业务任务复活。早期配置/断言失败保留，不算真实 provider 失败。

## 集成资格

最终组合代码/夹具680文件摘要：
`388fa3f765eadd9b6bed6a0e3194fbb1fe0dfc476dce4a758af494490955c3ef`。
早期 F1b 快照首轮源码3,169 passed、36 skipped：root 验证脚本误写 Redis opt-in 变量，未覆盖36项
显式 Redis 测试，不能作为完整门禁；原结果保留。修正的是验证脚本，产品源码未变。
该早期快照随后3,205项、零跳过通过，不替代后续测试维护和 F1c 的最终组合。
最终组合源码3,208项、零跳过、6条既有warning通过；Ruff与564文件格式通过。
树外组合300项通过，42.53秒；使用实际wheel的235个模块和实际sdist的examples，
AST约束另读取wheel复制模块，未从checkout导入包。996份公开文件单独复制后构建，
sdist的675份非生成文件与源文件逐字节相同；私有 evidence、工作树和 Store 未进入制品。
最终文档治理49项通过；相对链接、根指南直接引用和隐私检查通过。非作者对准确代码、
原始门禁、安装字节和最终文档/制品完成组合验收，结论为开发增量通过。

本批零真实 provider 调用，不证明真实模型动态规划、业务交付质量、恢复优势或总成本。
C3 性能增量仍在隔离开发，未验收的缓存代码没有进入本批。
阶段目标、长期组合与稳定发布资格均未完成；dev 开发交付不等于包发布。
