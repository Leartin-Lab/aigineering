# 声明投影与预算证明：局部性能验证

日期：2026-10-24。整合基线 `f47155c320cc7a813a66f33b81930ef01faf6df2`。
C1/C2 由两位 Luna 在隔离源副本并行实现，integration_review 非作者独立审查；
root 整合并验证与既有 CLI 的组合。[阶段计划](../docs/plans/sustained-engineering-delivery.md)
与[机器证据](data/110-local-projection-verification.json)区分局部通过和整体未完成项。

## 两项局部结果

C1 为实际同质内置 `contract.declare` Candidate 选择精确既存任务、输入/context Assets、
相关额度事实及完整 replay receipt/head/member。SQLite 使用原事实表的关系索引；
不新增表或缓存权威。已知预解析错误回原完整 context，再由原 reducer 认证和审计；
mixed/injected 或缺可选 port 的 Store 保留原路径，writer 原子校验保持。
原 SQL 参数分块未扣除五个固定 type 参数，独立复现后修复；Memory 对畸形无关字段
引入的 hash 异常也先保留红灯再修复。见[change120](../changes/120-scoped-contract-declaration-projection.md)。

固定同一声明、精确引用、相关 source 历史和 replay bundle，只增加真实签名闭合的
无关 H，独立测量如下。它只测 SQLite 窄读取，不是整个发布、Fleet 或 claim 成本。

| 无关闭合 H | 返回 Contracts / Assets / RuntimeRecords | 实际 SQLite 物化行 | RuntimeRecord 解码 | SELECT |
| --- | --- | ---: | ---: | ---: |
| 0 | 2 / 2 / 6 | 11 | 7 | 7 |
| 16 | 2 / 2 / 6 | 11 | 7 | 7 |
| 64 | 2 / 2 / 6 | 11 | 7 | 7 |

EXPLAIN 命中五路关系索引。组合缺失 source selector 的 VM callback 为102，H64移除
相关索引后860，减少88.1%；callback 是数据库工作代理，不是精确扫描行或 wall time。
另独立验证五个非空额度关系、199/200 source IDs、不同 SQLite 参数限额、四层签名链、
缺失 head 成员、精确重放、重开重建、forged 与 signed domain rejection 的不同 claim 语义。
Memory 实现仍遍历历史后选择，旧/custom fallback 也不自动加速。

C2 在一次 allowance grant 校验内建立多值关系索引，复用成功的完整 head/receipt
envelope/hash 与结构证明，成员/父关系用一次构建的集合查询。原歧义、精确声明、
资金配对与 writer 规则保持；无跨调用或持久缓存。见
[change119](../changes/119-indexed-allowance-grant-proofs.md)。独立27个旧/新差分向量的
接受结果或异常 class/message 完全一致，包含缺配对、重复事实、错值、错闭包和 opaque 历史。

| 同批 P | Head 完整校验：旧→新 | Receipt 完整校验：旧→新 | 长成员/父容器观测访问：旧→新 |
| --- | ---: | ---: | ---: |
| 8 | 40 → 1 | 88 → 1 | 15,504 → 342 |
| 16 | 80 → 1 | 176 → 1 | 61,344 → 678 |
| 32 | 160 → 1 | 352 → 1 | 244,032 → 1,350 |
| 64 | 320 → 1 | 704 → 1 | 973,440 → 2,694 |

这些是测试包装器观测与循环结构复核，不能当作所有 CPU 指令。正常可索引历史的
本次证明成本为 O(H+M+P)，另计实际验证字节 V；opaque 畸形容器保留原扫描谓词与
错误时序。每次 publication 仍构建输入索引，同父余额与 writer 的历史重扫尚未关闭。

## 组合证据与失败保留

C1 的8文件单包独立摘要 `d7a3919f66fec6f5581357ee098572b1245408b368b53cbb03344a14d7986a25`，
139项独立检查通过；C2 的2文件摘要 `dc871327611c898ebf3230358c096dd494a111d2259a40d6fffcc1601d96dbf7`，
131项独立组合通过。其首次4项网络失败均有 sandbox socket PermissionError 栈；
显式专用 Redis 后通过，不把未知失败随意归为环境。原单包红灯与探针诊断均保留。

首次完整整合为3,129 passed/3 failed，退出1：旧 hold race 测试向已迁移的初始 loader
属性注入 stale context，AttributeError 发生在运行时裁决之前。作者仅改两份测试各一行
注入入口，原 commit-snapshot stale patch 和业务断言不变。独立26项通过，并用只读 spy
证明三条 race 都进入 `BEGIN IMMEDIATE`，通过通用 hold 证明后，由原 claim-history
writer fence 拒绝；没有用更早的投影拒绝冒充该边界。

修正后准确组合代码/夹具671文件 SHA-256：
`97e5f44e6b5fcc6c5570b380aeec63df261523a619b7691c607a36b1619f60bc`。
源码3,132 passed、0 skipped、6条既有 warning，144.08秒；Ruff与557文件格式通过。
中间 C2 独立整合的3,100项源码和166项安装验证另绑其自身快照，不代替最终组合。

最终 wheel SHA-256 为 `2deaceb64608b1a0ef8092b2c4ba36c56360f5483bc45c94c7a40ec53922598f`。
构建使用排除私有目录的显式公开源副本和已有依赖；235个模块与源码及实际树外安装
逐字节相同，安装后224项通过、22.97秒，涵盖两项优化、资金/重建/竞争及 CLI 闭环。
治理49项通过，38个相对链接及根指南直接引用无缺失，18个交付文件隐私扫描无发现。
源码包664个非生成成员逐字节匹配981文件显式公开快照，包含两份 change 和本批记录；
原始 Store、凭据及无关工作树不进入制品。非作者最终组合独立验收通过；
核对准确快照、原始计数、差分、失败记录与制品，并独立复跑治理49项。
AST 约束在树外读取从该 wheel 复制的模块，运行时 imports 指向实际安装目标。
测试专用 Redis、旧 CLI 端口资源重定向和隔离 Store 纪律沿用上批；不修改业务断言。

本批零真实 provider 调用，不提供新的 usage、费用、交付成功率或质量优势证据。
SQLite 签名事实/claim 权威、纯投影、writer 重验和 Redis 默认必需保持。
余额跨次增量化、writer/Asset/maintenance 局部化、直接 Harness、公平成本对照和长期
组合资格仍开放；这两项局部结果不能证明整体 n² 已消除，也不是稳定发布或包发布。
