# 声明额度局部读取与 Asset 基线开发验证

日期：2026-10-25。范围：C3a/C3b 与 D0 的开发候选；独立组合验收 Passed；最后仅闭合验收元数据和源码包，不是稳定发布或阶段目标完成。机器证据见[验证数据](data/112-local-allowance-verification.json)。

受支持的纯 `contract.declare` 投影与原两次 writer allowance 校验现在读取明确来源和完整相关证明。SQLite 同事务维护可重建的 raw allowance checkpoint 与 publication member 关系，签名事实和原 owner 仍决定权利。只读尾部不推进缓存；缺失或已知无效的缓存走原验证，未知存储错误传播。混合、opaque、legacy、无法安全缩窄的 replay 及其他消费者保留慢路径。见 [ADR-047](../docs/adr/ADR-047-rebuildable-allowance-checkpoints.md)、[ADR-048](../docs/adr/ADR-048-rebuildable-publication-relations.md) 和[工作包计划](../docs/plans/scoped-declaration-allowance.md)。

原 B driver 未改，36组、108次 timing 和108次 instrumentation 全部成功，实际236模块与候选 wheel 逐字节相同。逐项发布64个任务时，公方法返回的 runtime rows 如下；它不包含全部内部 SQL 或物理扫描。

| 无关闭合历史 H | C1/C2 返回行 | 当前返回行 |
| --- | ---: | ---: |
| 0 | 25,920 | 64 |
| 16 | 71,360 | 64 |
| 64 | 206,528 | 64 |

所有样本的全量 Contract/Asset 公方法调用均为0。补充检查单列真实 SQL 返回、解码、fold 与 VM 工作：固定来源批次在 H0/16/64 中逐阶段计数相同；投影加两次 writer 为每来源3次 raw 读取，warm 旧 prefix fold 为0，新事实 fold 保留精确次数。头尾 R0/16/64/256 和 tail1/2 保持选集及索引范围查询门槛；Python 预处理的显式集合/映射避免重复线性成员搜索。时间只作描述性证据，不计算未经控制对照的速度倍率或以有限样本证明整个系统复杂度。

双连接先取得同一旧投影后，先前提交的消费或终态会被后一次真实 writer 看到并拒绝；旧 binary 在升级前保持打开并随后签名追加的两种尾部也被正确读取。缓存维护失败/外层回滚、缺表、坏状态不自修、reference 决策、重开与严格重建均有独立证据。C3a/C3b 的132项与179项是各自多条定向命令的检查，不能当作一次独立全套。

D0 是测量包。其原235模块基线完成78次 timing 与78次 instrumentation twin，全部决定、语义和私有重建一致；独立核对全部cell及30组汇总。78次 timing 合计1,122个接受 Candidate、2,166个 Asset effects；78次 instrumentation 另有相同合计，均不是模型调用。固定活跃集合下，P64/sample0 的 SQL 返回物化行 batch 随 H0/16/64 为91/638/2,270，sequential 为18,046/53,054/157,502。因此 Asset 路径仍与历史相关，尚未优化；[D0计划](../docs/plans/asset-publication-locality-baseline.md)保留该边界。

组合代码/夹具/脚本/向量711文件摘要 `9dbc004e376e2fb3fa8e6e7c597cbdfb4958edd2a222f1d0b6798697224cff65`：源码 **3,342项通过**、0 skipped、6项既有依赖警告，Ruff check/format通过；树外安装组合 **444项通过**。实际安装包、源码包中的 examples 和 wheel 提取的架构源码均核对来源；两份 driver 的树外 smoke 也核完整236模块。候选 wheel SHA-256 为 `196b0721dd8ffd82930ebed6177f737de3cbf97a364b9edd9c70d86fa9ddbbd4`。候选源码包逐684个payload校验，不含私有目录、凭据或原始 Store；最后报告与元数据更新后的源码包另行闭合，避免把包的自引用 hash 嵌入自身。

首轮组合3,341项通过、1项失败：模拟v5升级的夹具删除事实后留下现代缓存断言。仅在夹具降级准备移除真实v5不存在的三张表，原迁移事实、digest/rebuild、claim、幂等和replacement断言保持；非作者整文件3项及两个损坏现代缓存仍拒绝的反向探针通过。生产迁移和审计没有放宽。早期实现红例、误导入旧源的sanity运行、验证脚本状态/hash断言错误及Redis配置失败均分开保留，不替换或重标签原报告。

本批未执行新的真实 provider 评测或使用会话测试 key；CLI业务证据仍是明确的确定性夹具。质量、恢复总成本、两臂公平预算、Asset/claim等其余热路径和长期一般规模资格继续开放。最终公开文档治理49项、144条相对链接及根指南29条链接/68个路径检查通过，新增材料无私有路径或凭据。非作者对准确32文件、1015文件公开集合、制品、完整测量及角色/失败/限制独立总体验收 Passed；另运行53项offline检查和49项最终治理，未将root完整套件称为独立全套。最后只更新本验收元数据并重建源码包，独立轻闭合后交付开发分支。
