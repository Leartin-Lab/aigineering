# Runtime计量与Asset批内分派开发验证

日期：2026-10-26。预声明组合为G0c candidate3、D1b1 FIFO前置与D1b2 candidate2；E1及后续provider兼容修复不混入。本批各组件独立接受，Root源码、安装、构建及实际安装驱动通过；非作者对准确组合独立整体Passed；最后验收元数据/源码包按同一724文件代码快照轻闭合。不是阶段完成或稳定发布。详见[机器证据](data/114-runtime-meter-dispatch-verification.json)。

G0c以显式source-checkout配置把Planner/Researcher普通LLMWorker接到一个单进程RunMeter，零内部retry。四个严格typed辅助事件区分permit、transport、bounded reply/sentinel与finish；unknown usage不算0，日志不成为事实、资格或账单。新进程新epoch，不恢复旧预算。独立51单元、13边界、13CLI通过；五个fake Stores/54任务/36 fake请求逐项核对，四个新恢复任务实际claim、产出、不同actor接受并complete，原failure保持。C1/C2未通过与C3通过各保原快照。见[计量计划](../docs/plans/runtime-provider-meter.md)与[change127](../changes/127-local-runtime-provider-meter.md)。

D1b1以deque维持原FIFO，独立树外29项通过；旧source-path失败保留。D1b2单次构造不可变多值dispatch视图，原始split token、同name全值、事件顺序、creator与输出政策保持；output候选桶不替代descendant完整图。独立60项、五签名H/P cell真实precommit/writer窗口的完整event/materialized-ID差分及重开/重建通过。C1原55pass因证据不齐NotPassed，新C2补齐而未改生产；不重标签旧结果。局部C-pass不等于reducer全线性，helper、qualification、history与ancestor仍开放。见[分派计划](../docs/plans/asset-batch-dispatch.md)与[change126](../changes/126-batch-asset-dispatch.md)。

准确724文件摘要 `b5de9ab45a66175667fedd8a6488e609acf615a64416e10e9ac9d0bf53149366` 已通过源码 **3,602passed**、0skip、6项既有依赖warning、237.69s；Ruff与585文件format通过。三单包的旧独立源、当前组合与实际安装制品分别绑定，不把旧1028文件G0c源写成新版Root来源。

E0只读库存另确认待修成本：无源变化capture仍取全部记录；正常签名逐任务闭合历史H0/16/64、固定A2时，N16/416/1616与T3/19/67导致实际48/7,904/108,272次index-values访问，恰为T×N。这是capture内Python成本，不是SQL物理扫描数或完整Redis warm量化。原Store与无包装event capture相等；前缀完整性、foreign source、双Engine WATCH与atomic swap语义不能为提速免检。E1单独规划，当前未修。

真实provider测试单独绑定此前dev183d9c8和wheel `fd6130ef1dbd77649b4f9ae22f203403004919c5e7515d0e21052c0437bcbc52`。两次预注册实验每次最多一次请求、输出预留1024、零retry，均HTTP200后native解析拒绝，exit6、无工具或交付。首轮已知usage1860/264/2124；第二轮1860/248/2108。第二轮透明私有observer先经非作者7项离线/no-network检查，实际只观察固定结构桶与原parser729行fence：single tool-call对象不满足原严格结构规则，content为空字符串。任意键和值、正文、推理和凭据未保存；不能断言某个额外键或回推首轮body原因。已知token总数4,232不等于已知费用，结果不计成功率或成本优势。

官方[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)与[thinking/tool规则](https://api-docs.deepseek.com/guides/thinking_mode/)在运行前复核；文档兼容不替代上述失败。后续native适配及普通Runtime真实重复交付需新准确候选/有界协议与独立验收，不能重跑覆盖这些失败。长期同Store增长、受控故障、外部质量及总成本目标仍开放。

树外真实wheel安装组合 **739passed**、37模块、103.28s，实际wheel SHA-256 `553a7de770d11fd7ac1a7443d70c75f4fc56e4e8f043cf3f86b0cd5f57366134`。主测试导入actual installed target，CLI子进程的src副本逐字节取该wheel；examples来自真实sdist，335份public tests绑定源码。实际安装Asset driver三对/nine微案例、context六对/单列H16 replay通过，各核完整236模块；未重复完整78组D0矩阵。候选源码包708payload恰为1043公开文件减335tests，私有目录、原始Store不入包；最终验收元数据按同一724代码快照另做源码包闭合，避免包hash自引用。

非作者整体报告摘要 `7948595061b2299255b4d62666dbd9b97c69143b989f0b4e141c61a058cc329c` 绑定23文件及1043公开文件快照 `c0dc6d559b5ada6b4d46662ae6d7e26412f30a400874532616474c465de834a9`，独立173项及Ruff通过。另对实际安装测试产出的五Store/54任务只读规范复核：四个新recovery具有精确claim/submission/qualified output/不同actor接受/complete，原failed保持，36fake permits四事件恰一次，原DB/log字节未改。实际wheel、sdist708成员、全236模块、Root3602/739及两驱动原始证据和旧两次真实失败均核对；审查者两次probe搭建错误保留为setup证据，不算产品失败。最终仅收验收状态，重核治理/链接/隐私和源码包，不改变724代码、236Runtime或wheel；E1/G0b1及阶段/稳定资格仍不在此Passed中。
