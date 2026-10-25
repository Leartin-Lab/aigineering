# Asset 投影、输出判定与 direct transport 开发验证

日期：2026-10-25。预声明组合为 corrected D1a、G0b、D0c、D1b0，加三个测试文件的加载入口维护，共35文件。各组件已独立接受，root源码、安装、构建和驱动检查通过；非作者对准确组合独立总体验收Passed，最终验收元数据及源码包按相同717文件代码快照轻闭合。不是阶段目标完成或稳定发布。机器证据见[验证数据](data/113-asset-context-direct-transport-verification.json)。

纯 `asset.propose` 现在可通过通用上下文工厂，只选择精确claim parent及单effect完整replay证明；混合、graph、injected、未知形状和无可选reader的Store保留完整上下文。SQL留在SQLite adapter，原授权、重放证明和writer-time claim/key/output检查保持。本批不新增事实表、缓存权威或调度器。见[change123](../changes/123-scoped-asset-proposal-context.md)。

D1a组件的独立216项（167受影响及49治理）、33个正常native-graph差分、5个实际method前缀拒绝和12个selector观测通过。H0/16/64的正常签名完成历史含0/32/128历史Assets；实际SQL返回/解码如下，interval-one VM分别稳定为55/6/37/125。这只覆盖该组件上下文选择阶段，不是整个Asset提交链路或物理扫描复杂度证明。

| 选择形状 | 各H实际返回行 | SELECT数 |
| --- | ---: | ---: |
| fresh single | 0 | 2 |
| unbound P64 | 0 | 0 |
| exact parent | 1 | 1 |
| receipt/head/two-member replay | 4 | 3 |

D0c把真实generic factory纳入观察，避免漏计新路径；旧版本仍测实际旧入口。非作者在两套真实Runtime包各运行6项，包括smoke三对/nine微案例、六组context pairs和单列H16 replay；所有4份成功报告核对完整236模块。新pure P64 context为0，mixed仍full，replay精确4条证明，legacy返回完整历史并另列相关证明。原D0的78次timing和78次instrumentation、原driver摘要保持历史来源，不重标签为本轮结果。见[观察器计划](../docs/plans/asset-projection-benchmark-compatibility.md)。

D1b0修复了既有正确性缺陷：同批business Asset及tool/mcp/label_placeholder会被名称快捷路径误计入完成，bare3d2与旧D1a均能复现。现在完成和子任务关闭按实际pending Assets检查共享业务、creator及independent政策，activation仍可使用观察名称。非作者49项、四个真实签名向量和跨版本历史Store检查通过；旧代码生成的错误complete在新代码重开/严格重建后原ID、事实和digest保持，不以rebuild修历史。原作者48项红例含16个新API缺失与7个实际行为失败，不算23种缺陷；最终49项绑定新源。见[change125](../changes/125-pending-asset-output-policy.md)。

G0b增加显式 `direct-live.py` 原生provider入口，保留默认严格离线wire和 `direct-run.py`。它复用单进程RunMeter，分别记录permit、transport和response，原样保留完整bounded JSON中的usage；未知不作0。非作者209项owned tests、10项边界探针和1项报告I/O探针通过。早期tool-call ID日志泄漏、fixed/chunked提前EOF、cap+1及HTTP oversize退出分类红例保留并修正；准确ID/reasoning仅留内存续接。它不连接Runtime transport，不是公平预算或真实模型结果。见[change124](../changes/124-direct-deepseek-transport.md)。

最终717个源码/测试/脚本/示例/conformance文件摘要 `e87654c18b07100d555247e2e15989ffaa28c598e8e602d43158f1c2ee9da9ca`：源码 **3,532passed**、0skip、6项既有依赖warning、200.89s，Ruff/581文件format通过；树外34模块组合 **656passed**、70.15s。实际安装wheel SHA-256 `fd6130ef1dbd77649b4f9ae22f203403004919c5e7515d0e21052c0437bcbc52`，236模块逐字节核对；examples来自实际候选sdist，架构源码取实际wheel。安装target的planning六cells、Asset三对/nine微案例及context六对/单列replay也通过，各核完整236模块。Runtime源码摘要为 `a420a4ea9e02d5033afcc176a848849feb1237464d0e7591ad81af38d27f8b08`。

首次governance发现协调器311行，违反原<300所有权约束；分派移回context owner后297行，约束未放宽。首轮全套3,527passed/5failed、树外651passed/5failed均因三个旧测试hook在writer前引用已移除别名。仅更新这三个夹具到真实factory，并证明旧snapshot实际进入、真实pure reduction接受后由fresh writer拒绝；原余额、claim、relay、ID与审计断言保留，非作者五例通过。生产19文件和wheel字节未为测试变动。首轮698项代码清单未含conformance19向量；最终717项明确包含。原红例、探索性setup及私有报告检查器路径/hash约定错误分开保留，不替换产品证据。

候选源包697个payload与1030公开文件集合逐项核验，不含tests、私有目录或原始Store；最终源码包按最新文档另行闭合，不把自己的hash嵌入自身。本批未读取或使用会话真实测试key；全部模型测试为明确的dummy/fake设施。Runtime共享计量、受控真实provider协议、恢复与交付总成本、Asset/claim等历史和CPU热点继续开放；开发增量通过不替代这些阶段义务。

非作者整体复核绑定1030公开文件与35文件差异，准确公开快照摘要 `3779e00bdc15d8f075dc9c543f3f7897527ab06a84206df8095e538ff02610d6`，报告摘要 `4637c77b14cde5877c2901779af937cad4fb5cac4b4aa652773c8a4da1462eac`。独立123项离线检查、11项边界探针、四个正常签名向量通过；一个hosted-claim例显式deselect，其组件/整合/安装证据另核。实际wheel与安装236模块、候选源包697个payload、69份原组件证据摘要及文档引用逐项核验。审查者自己的网络guard误触与检查器setup红例亦保留，未作产品失败或资格通过；本批不含G0c或后续队列优化。最后闭合仅更新验收状态和公开元数据、复核治理/引用/隐私并重制源码包，不改变受测717文件；最终源码包及闭合结论随准确交付快照验证，避免自引用hash。
