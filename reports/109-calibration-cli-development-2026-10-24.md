# 校准分析 CLI：开发验证

日期：2026-10-24。工作包 F1a，开发基线 `55b32ed6054a617a20280cf1bb9bf23ef4b6d829`。
本批提供[可运行配置](../examples/thermal_calibration/CLI.md)，以协议夹具验证普通任务闭环；
真实模型、直接 Harness 对照、长期规模和全成本评测仍未完成。
范围和独立审计见[阶段计划](../docs/plans/sustained-engineering-delivery.md)，
准确文件和检查口径见[机器证据](data/109-calibration-cli-verification.json)。

## 已验证行为

CLI 发布两份冻结合成输入、预算32的根义务、Skill 和显式 Worker 配置。
生产配置指定 deepseek-flash Planner/Researcher、两项 ToolWorker 工具和不同 actor 的
确定性 LocalVerifier；没有 mock 生产默认。本批测试将模型参与者显式替换为固定协议
Worker，普通 planning、tool continuation、恢复、claim 和 commitment 仍由 runtime 执行。
夹具 blueprint 不代表真实模型会稳定生成正确计划。

非作者独立执行三条路径，连同 adapter/config 边界共6项通过：

- 正常规划展开，normalize→analyze 两次工具观察，报告、精确独立接受、根完成和导出。
- 测试 registry 首次工具 schema 故障，保留失败及原 claim；新恢复任务实际领取、
  提交并产出成功 normalize 观察，再完成原义务。故障 marker 只模拟后端状态。
- 缺少 verifier 时根未完成、报告未 qualified 且导出拒绝；恢复同一 verifier 身份配置后，
  仅 resume 原根，保留原报告与输入，不重置旧 claim。

三条运行的269/291/269条事实另经只读独立复核：工具和报告各有唯一有效 receipt/head、
完整成员与对应 claim；故障恢复的观察直接属于已调度的精确新恢复任务。
Attestation/qualification 绑定同一根输出、报告 Asset、排序后的两份源证据和不同 actor；
根为 task:v6 并冻结 exact input bindings。导出等于合格 Asset 原字节；重开和私有副本
strict rebuild 摘要一致，原数据库字节未变。只读审计没有发布完成。

LocalVerifier 使用既有独立 Fraction oracle 复算报告，核完整归一结果、严格 JSON 类型、
双观察顺序、精确源 IDs 和固定政策目标，拒绝重复 JSON 键。非作者另验证11种篡改被拒，
包括缺/多字段、改行/hash、bool/int 混淆、反序、重复键及换目标/来源。
这些纯 adapter 检查只证明 Candidate 选择；成功发布另由上述真实签名运行证明。
复制的观察内容和自报 IDs 不能证明 signed history；生产 verifier 的内容校验与测试审计
的历史证明分别报告。正确的 outside-tolerance 报告仍可合格交付。

## 准确快照与完整检查

新增9文件由 Luna 实现，root 负责导航和集成；integration_review 未参与作者工作。
9文件独立清单摘要为 `84ff6ab3701d9a88ed75eee08da4f20e2d0f361d3da26d398a73dbd5d628bac0`。
组合代码/夹具清单669文件摘要为
`b01c8e9a5d451e93d6f457ff504c5f0f50df0ef24ce7be0c20fe9c8baddd0571`。
既有 A 工具、oracle、输入及 runtime 源码未改。

- 源码全套：3,095 passed、0 skipped、6条既有 warning，141.99秒。
- Ruff check 与555个 Python 文件格式检查通过。
- 非作者定向6 passed，19.01秒；作者最终6 passed，20.11秒，分别保留。
- 显式公开源码副本使用已有依赖构建 wheel；源码树外安装后106项通过，18.84秒，
  包含上述三条 CLI、独立 oracle、SQLite、commit snapshot 和 Candidate 检查。
- Wheel SHA-256 为 `ef8d92d66ced152dcebe216e5b6c01238fb2c7c7f8e26e4c568e4e75a87ec4fe`，
  235个模块与源码及实际树外导入逐字节相同。本批仅新增示例/测试/文档，故与首批 wheel
  相同；新示例随公开源码交付，按 CLI 文档显式配置 checkout import path。

全部 CLI 检查使用本批专用临时 Redis 串行运行；完整套件将旧 CLI 测试固定端口通过
私有资源适配重定向到该实例，不修改测试断言或业务实现。Redis 的 flush 测试另用专属库。
最终治理49项通过；本批46个相对链接及根指南直接引用均无缺失，14个交付文件隐私
扫描无发现。源码包660个非生成成员与975文件显式公开源副本逐项匹配，包含新示例和
验证记录，排除凭据、私有路径、原始 Store 和无关工作树。非作者独立组合验收通过，
重核准确清单、原始日志、源码包和实际安装。闭合仅更新验收元数据并重建、复核最终
源码包；结论仅关闭本批开发增量。

## 保留的失败与未完成范围

首轮沙箱不允许 Redis socket 连接，3 failed/3 passed；隔离实例及提权连接后继续诊断。
第二轮3 failed/3 passed，原 stdout 保留；外层 session 被中断，pytest 自身退出码未知，
不倒填为退出1。最终续接 tool_scope 为空时夹具漏发布报告，已修复；第三轮3 failed/
3 passed，工具 Asset origin 的断言错误已更正。第四轮2 failed/4 passed，实际拒绝是
attestation evidence ID 顺序不匹配冻结政策；直接生成 Candidate 不等于提交成功，修正
为排序后的精确源 IDs 后第五轮及独立复验通过。初审发现的 Skill 契约遗漏、复制观察
校验缺口与相关红灯也保留，不以最终成功删去失败。

本批零真实 provider 调用；没有新模型 usage 或费用测量，未知量不计零。
未验证真实模型规划成功率、畸形 provider 输出/额度耗尽的全部组合、直接 Harness 臂、
业务收益或长期持续运行。C1/C2 等性能工作另行验收；本批不关闭整体 P² 缺陷，
也不是稳定发布、包发布或一般自主交付保证。
