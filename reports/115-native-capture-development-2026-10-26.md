# 原生工具响应与捕获关联索引开发验证

日期：2026-10-26。预声明组合为G0b1与E1，起点devdc0cb4f。各组件及准确16文件开发组合均获非作者接受，源码和构建门禁通过；安装首次816通过/1失败，保留失败后一次完整观察复跑817通过。间歇Fleet错误继续开放。不含E2a或新真实模型运行，不是阶段完成或稳定发布。详见[机器证据](data/115-native-capture-verification.json)。

G0b1将native工具调用及function对象的必要成员校验后投影到原canonical DTO，不消费的metadata丢弃。只有tool_calls的literal empty content规范化为None，原mapping不变；空白/非空/错型内容仍拒绝，ID/参数/启用时reasoning保持精确。top/choice/message、single-call、严格offline envelope、工具参数/schema及重试/权限owners不变。独立230项及Ruff/format通过。此前两次真实请求仍失败：729行只证明调用对象的类型或exact-key结构fence，未保存的实际键和值不能推断；本次适配不是针对某个猜测键的特判，也没有新真实provider通过。见[change128](../changes/128-native-tool-response-compatibility.md)。

E1在每次Redis capture的effective by-ID map上建立局部多值头索引，保留所有存活head及跨ID全局顺序，成员仍经原递归reader。非exact-str candidate ID标记fallback，只在原commitment分支做旧惰性全扫，包括零提取ID的原TypeError；empty/None、重复record-ID覆盖、visited和四个未迁caller的legacy路径保持。上下文只借用本次capture所有者不修改的输入map，不跨refresh，也没有新事实表、持久cache、Store/SQL/Redis协议或prefix免检。独立49项、八个native NotRun与真实Redis资格分别报告；六个正常签名fixture各两次actual capture与legacy完整fields/digest/revision相同。见[计划与计数表](../docs/plans/typed-trace-reference-index.md)及[change129](../changes/129-capture-trace-reference-index.md)。

顺序声明历史H0/16/64时，旧T×N values访问48/7,904/108,272变为单次index输入16/416/1,616，实际bucket lookup/head view为3/19/67。另有exact-member gets12/204/780、worker-closure gets27/1,179/4,635。批声明family另列，不能混算。这里是Python capture阶段计数，不是SQL物理扫描、全capture O(N)或Redis wall-time证明；完整前缀枚举/materialization、RlogR排序、递归/worker及全prefix proof成本仍在。

准确724文件摘要 `ecad12f5ef8cdaebc1e9fe73039a44eef42ef6ed3cc03d655f83821b60b72795` 已通过源码 **3,635passed**、0skip、6项既有依赖warning、233.04s；Ruff与585文件format通过。此前八个native Redis NotRun在本次隔离专用Redis门禁实际执行，不能将单包49+8skip重写成全部通过。作者的API-absent red、probe调用者分类/复用失败路径setup、旧editable导入失败均分别保留；不冒充产品缺陷或新候选成功。

Root代码与组件旧源、真实wheel/sdist及后续安装来源另绑定，独立整体已在准确组合完成；最终验收元数据与源码包另按同一代码快照轻闭合。新真实Runtime三次freshStore验证将另冻结候选和请求/output/time预算；不把fake或这次源码绿变成业务质量、费用或长期同Store证据。

实际 wheel SHA-256 `8619116fd014a6d3ce9347821c3ddd9f95413fb585d1ba2c72488963589ff92d` 的236个模块与当前Runtime摘要 `99502251c197fb2ae86dc3e1da98c8eaf9b402442318792b1976d473404b478b` 一致。首次源码包包含713个公开payload文件，对应初始1,048文件公开快照；测试不进入源码包。安装主进程从实际target导入，子进程src来自同wheel的逐字节副本，examples来自实际源码包，335份测试来自冻结源。

首次树外42模块检查为 **816通过、1失败，98.68s**。缺少验收者用例预期timed_out，却返回worker_error；原Store、日志和失败退出完整保留，测试当时没有保存slot异常栈。之后同包单例安全观察通过（timed_out后同根任务完成）；预声明旧B3/新B4各两次对照全部通过，未观察slot异常。再对原42模块做一次带返回/异常保持观察器的完整复跑，**817通过、0skip，104.67s**，包括实际native Redis用例。没有修改产品或测试代码，也没有将原失败删除、归因于环境或宣称修复；间歇slot错误原因仍未知，是可靠性限制，不能用后续通过关闭。

另以实际新wheel逐模块验证236处导入来源，执行六组签名fixture各两次capture，十二次与legacy完整事件字段一致，digest/revision保持。该安装probe无网络或Redis，其结果不能代替源码与树外native Redis资格，也不是独立整体验收。独立审查已接受准确组合并明确保留该失败；阶段和稳定资格继续开放。

非作者独立整体报告摘要 `c1e71b293eab0bfb86e599a86b9bb890f535ce06ee857a72ac4611c1aabfee22` 接受本开发增量。其审查绑定1,048文件快照 `829cfb343e244bbd417afa79b2391e8075de8fd1ed3645b8108f6917a5a560fa` 和16文件改动 `df75132e4831473f3224ef3c61ba046428944440cde23a12aa350ee1cb7c0cea`；同代码的已审源码包摘要 `f9a377f6c464d56aac7d280e0d59d6da01972a0bacdefcc4a631af408c6c871a` 有713个准确payload文件。后续仅验收元数据轻闭合，不改724文件代码或236模块wheel。

独立 **328项通过、8项native Redis明确NotRun**，包含49项文档治理；一项pytest配置warning来自关闭插件autoload后的asyncio_mode，单列而非产品错误。另有六组/十二次独立实际安装capture等价比对；核实全部236模块文件字节，capture实际加载100个模块，不能说独立导入全部236。Root另行归属的Luna安装preflight才导入全部236。独立只读检查后续五个成功fake CLI Store的54个终态任务、四个真正执行并独立接受的新恢复、36个fake许可及每许可四事件；这些成功Store不能替代原失败Store，也不算真实provider运行。

独立检查原失败Store确认：根为expanded、非terminal、claim:submitted；报告、attestation、qualification与业务invocation_failed均为零，五份精确claim capture结束为returned，另有无blocker的ready工具任务。因此原业务尚未完成，存在可由普通流程续跑的工作，不能简化为只缺验收者。当前canonical检查、仅副本strict rebuild和当前indexed/legacy捕获相符，原DB/WAL/log字节未变；审计没有续跑原Store或发布恢复。它们不能还原当时slot异常、历史Redis状态，也不能排除B4回归。

私有诊断观察器只在健康落盘时保持返回/异常identity，并有时序与栈开销；写盘错误可能覆盖原异常。独立合成检查确认健康sink行为，实际诊断未观察sink失败或slot异常。保留这些限制，不将观察运行等同无扰动证明。Root前置49项治理只有原工具transcript和转录binding，未另存stdout；独立49项对实际冻结文件执行的证据补足该局限，不声称不存在的raw文件。

独立结论支持本次有限dev增量，没有发现这两切片的产品或事实资格边界阻断。未知间歇故障仍是开放可靠性事件；不宣称所有尝试全绿、故障早于本批或已修复，更不建立无人值守、长期活跃局部性、真实模型兼容、阶段完成或稳定资格。
