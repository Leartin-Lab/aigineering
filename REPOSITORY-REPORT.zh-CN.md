# Aigineering 仓库现状报告

> 本报告仅依据四份冻结证据 Asset 撰写：`README.md`（仓库 README）、`SKILL.md`（Aigineering Skill 说明）、`docs/reference/task-board.md`（任务看板与 Redis 操作参考）、`ROADMAP.md`（路线图）。凡证据未明确支持的内容，本报告不作结论或标记为未知。

## 1. 定位与核心边界

Aigineering 是一个面向 AI Worker 的零信任运行时：Worker 输出是 Candidate（候选），不是共享状态；只有通过签名、claim、projection、authority、allowance 与 acceptance 检查的效果才会成为持久事实（见 `README.md` 的 “Aigineering” 引言与 “Core boundary” 章节）。

核心边界流程在 `README.md` 的 “Core boundary” 章节给出：

```text
Contract + disclosed facts
        ↓
 authenticated Worker claim
        ↓
 signed Candidate effects
        ↓
 projection + authority + allowance + acceptance
        ↓
 atomic fact and trace commitment
```

`README.md` 的 “Non-goals” 章节明确列出当前运行时**不**声称的能力，包括：跨机器共识或分布式 Store 语义、公网部署加固、外部安全审计、模型内容的语义真值、跨独立机器的调度公平性、通用工作流语言、生产级 MCP 传输、进程级工具超时/取消/隔离、外部副作用的 exactly-once 投递、跨机器 Store 与 Worker 发现。

## 2. 当前版本与发布状态

- `README.md` 的 “Current local preparation” 章节说明当前源码为 **v0.5.13.dev0**，新增 configured-run 入口、任务操作与必需的 Redis 协作。
- 同一章节指出 v0.5.12 快照完成了确定性、打包、已安装产物、链式与嵌套重规划验证，但这是**本地验证的发布准备，不是已发布或已打标签的版本**。
- `ROADMAP.md` 的 “Next usability slice” 章节同样声明 v0.5.13.dev0 与 v0.5.12 均**不是已发布或已打标签的版本**，稳定资格与打标签仍是独立的收尾工作。

因此，本报告区分：**源码/测试覆盖**（development verification）与**稳定发布承诺**（stable release）。证据中反复出现 “development verification”“bounded development evidence” 等表述，均不等同于稳定发布承诺。

## 3. 已实现能力（implemented）

`README.md` 的 “Reference runtime scope” 章节列出单机参考运行时提供的能力，包括（节选）：

- actor 签名的 Candidate 发布；
- CLI、Worker、Plugin、HTTP 表面共享的单一 Candidate 提交边界；
- 用于 claim、fencing、projection、facts、trace 与幂等性的 SQLite 事务；
- 由 append-only 记录重建的运行时视图；
- claim 绑定的 Worker 包与已认证提交；
- 作为普通可独立 claim 任务的分阶段规划与重规划；
- 递归发布的因果 allowance 约束；
- Contract 要求时的独立输出 attestation；
- 确定性重放、lineage、审计与任务状态视图；
- 同一 SQLite 域上的同机 active-active Worker 仲裁；
- mock 与 OpenAI 兼容的 LLM Worker；
- 通过 `api` extra 的可选 FastAPI 集成；
- 必需的 Redis 协作，用于索引发现、可重放事件与 Worker 唤醒，可从 SQLite 重建；
- 内容身份、签名定义身份与签名关联身份的分离及多对多历史；
- v4 Contract 将标签选择的上下文绑定到精确 Asset ID；
- v5 Contract 将执行需求与委托路由范围分离；
- 由 WorkerHost 签名的图原生普通 Worker 输出；
- 终端/claim fencing 与持久确定性提交拒绝；
- 单一因果 allowance 来源与单一终端事实构造器；
- 提交前的规划标签包含；
- 显式配置、Contract 作用域的工具注册表与独立 ToolWorker；
- 精确可验证的 slice 派生与 claim 门控披露；
- 独立 attestation 的后代输出；
- 独立审查无法覆盖的确定性 JSON 输出形状；
- 可执行的 AI4S 文献与安全数据画像示例；
- 使用独立 SQLite 连接的异构本地 Worker fleet；
- 编译为普通任务加布尔 join 的并行工具调用；
- 从 claim 绑定的结构性输出拒绝中持久恢复；
- 可执行的本地工具契约，含确定性输入/输出 schema、版本绑定、UTF-8 输出上限与描述符漂移拒绝；
- 结构化工具执行元数据与只读 `task audit --json` 生产力投影；
- 在 SQLite 重开后仍存活的 runtime-only AI4S/Fleet 工具观察 → 继续 → 独立验证循环。

`ROADMAP.md` 的 “Earlier local release preparation” 章节以 “Implemented” 列表形式给出 v0.5.12 的已实现项，与上述内容一致，并补充了：语言中立的签名协议一致性向量、可选 Redis 查询投影（含 revision fencing、rebuild 与 SQLite fallback）、v4 Contract 身份中的精确标签上下文绑定、通过 Candidate 边界的建议性语义关系发布、事务性终端/claim 关闭与持久确定性提交拒绝事实、Plugin 拥有的规划脚手架、面向现有 agent harness 的框架中立签名 Candidate 适配器、配置化 LLM 执行作为 CLI 默认（mock 显式用于测试）、Contract-v5 执行需求与委托范围分离、能力与池路由的异构本地 Worker fleet、提交时完成收敛、可执行本地工具契约、结构化工具执行元数据、只读 `task audit --json` 生产力投影，以及 runtime-only AI4S/Fleet 验收循环。

## 4. 常见使用步骤（仅使用已记载的公共 CLI/配置）

### 4.1 环境准备

`README.md` 的 “Quick start” 章节要求 Python 3.11 或更新版本，以及运行中的 Redis 服务：

```bash
git clone https://github.com/Leartin-Lab/aigineering.git
cd aigineering
pip install -e ".[dev]"

# Required local collaboration service (or configure an existing Redis URL)
docker compose up -d redis
aig redis status --json
```

`docs/reference/task-board.md` 的 “Task board and Redis operations” 开头同样说明：正常 CLI 操作需要 Redis，默认地址为 `redis://127.0.0.1:6379/0`，可用 `AIGINEERING_REDIS_URL` 选择已有部署；正常应用查询不会回退到 SQLite；`--help` 在无 Redis 时仍可用。

### 4.2 配置 OpenAI 兼容 LLM Worker

`README.md` 的 “Quick start” 章节给出：

```bash
export AIGINEERING_API_KEY="..."
export AIGINEERING_MODEL="your-model"
export AIGINEERING_BASE_URL="https://provider.example/v1"
```

### 4.3 运行一个带显式来源的通用报告任务

`README.md` 的 “Quick start” 章节给出：

```bash
aig run "build a report with citations" --input evidence=source.md \
  --output report.md --max-output-tokens 8192

aig trace
aig asset ls
```

`SKILL.md` 的 “Choose the integration mode” 章节补充：`aig run GOAL` 只使用目标与通过可重复 `--input NAME=PATH` 显式指定的文件，不扫描工作区、不联网抓取来源；文件必须是非空 UTF-8 且每个不超过 1 MiB；成功的可导出文本输出会以 Markdown 写入 `--output PATH`（默认 `.aig/outputs/<session>.md`），已存在的目标不会被覆盖。

### 4.4 初始化签名域并发布任务

`README.md` 的 “Quick start” 章节给出：

```bash
aig domain init
aig asset add --name source --content "reviewed evidence"
aig task create \
  --name research \
  --description "Produce a report from the disclosed source." \
  --input source \
  --activation source \
  --output report
```

`SKILL.md` 的 “Publish one trustworthy task” 章节给出更完整的四步流程：初始化签名域并发布不可变输入、声明精确结果槽与激活事实、运行配置的 LLM Worker 直到任务达到终端投影、验证完成与已提交输出。其中验证步骤使用：

```bash
aig task status <contract_id> --json
aig asset show boundary_report --json
aig task audit <contract_id> --json
```

`SKILL.md` 明确：除非状态为 `completed`、每个必需输出映射到精确 Asset ID、且审计没有未解释的拒绝或静默失败风险，否则不得报告成功。

### 4.5 任务发现、观察与结果导出

`docs/reference/task-board.md` 的 “Discover and operate work” 章节给出：

```bash
aig task list --json
aig task board
aig task stats --json
aig task show <contract_id> --json
aig run --task <contract_id> --json
aig task wait <contract_id> --timeout 60 --json
aig task result <contract_id> --output report --to report.md --json
```

同一章节说明：`list --status STATUS` 可重复；`list/board --root ID` 选择精确 Contract 及其不可变后代；`result` 要求完成且一致的输出义务，选择精确业务 Asset，独立策略要求对应资格；`--to` 时选择单个输出，UTF-8 字节与已提交内容一致，已存在文件不会被覆盖。

### 4.6 取消、重试与审计

`docs/reference/task-board.md` 的 “Discover and operate work” 章节给出：

```bash
aig task cancel <contract_id> --reason "Inputs withdrawn" --json
aig task retry <contract_id> --json
aig run --resume <new_retry_contract_id> --json
aig task audit <contract_id> --json
```

同一章节说明：取消是签名 Candidate，并通过普通事务释放 claim；重试共享顶层 `aig retry --contract ID` 实现，发布带冻结作用域的确定性新 Contract，而不是重置原 claim；需检查返回的重试 ID 并显式执行。

### 4.7 检查 claim 尝试

`docs/reference/task-board.md` 的 “Inspect claim attempts” 章节给出：

```bash
aig task runs <contract_id> --json
aig task diagnostics <contract_id> --claim <claim_id> --limit 20 --json
aig task diagnostics <contract_id> --claim <claim_id> --limit 20 \
  --cursor <next_cursor> --json
```

同一章节说明：分页默认 20 行，接受 1 到 100 的限制；继续时使用相同命令、任务、claim 与限制；cursor 保留原始 revision 与 UTC 观察时间；`runs` 显示 claim 分配与已证明的提交、拒绝、调用失败、释放或过期；仅分配不证明 Worker 被调用；`submitted` 不使输出合格。

### 4.8 多任务批量操作

`docs/reference/task-board.md` 的 “Apply an operation to several tasks” 章节说明 block、cancel、unblock、promote 接受 1 到 64 个不同任务 ID，每个任务独立提交，后续拒绝不影响先前已接受操作，任何不成功结果使命令以非零退出。示例：

```bash
aig task block FIRST_ID SECOND_ID --reason "Waiting for inputs." \
  --idempotency-key block-round-1 --json
aig task unblock FIRST_ID SECOND_ID \
  --hold-for FIRST_ID=FIRST_HOLD_ID --hold-for SECOND_ID=SECOND_HOLD_ID \
  --reason "Inputs confirmed." --idempotency-key unblock-round-1 --json
aig task cancel FIRST_ID SECOND_ID --reason "Work withdrawn." \
  --idempotency-key cancel-round-1 --json
```

### 4.9 任务对话、持有与手动恢复

`docs/reference/task-board.md` 的 “Task conversations” 章节给出 `task comment` / `task comments` 用法；评论是签名陈述，回复必须引用同一任务上已提交的评论，非空文本上限 65536 UTF-8 字节，评论不改变状态、claim、输出义务、allowance 或 acceptance。

“Hold work before it starts” 章节给出 `task block` / `task holds` / `task unblock` 用法；任何未关闭的 hold 都会阻止 Worker pull 与直接 SQLite claim 获取；释放一个 hold 不影响其他 hold。

“Recover manually held work” 章节给出 `task promote` / `task promotions` 用法；promotion 快照每个任务当前所有打开的 manual hold 并原子释放该精确集合；并发新 hold 或竞争释放会拒绝整个任务操作；依赖、激活、缺失输入 Asset、assignment 与 Worker 容量仍然适用。

### 4.10 依赖排序、交接与编辑

`docs/reference/task-board.md` 的 “Order work with exact prerequisites” 章节给出 `task link` / `task dependencies` / `task unlink` 用法；前置任务必须具有类型化成功完成事实，新 claim 才能成功；失败、取消或不可达的前置任务保持阻塞；链接只排序 claim，不传递前置输出、不改变冻结输入或验收策略。

“Hand off unfinished work” 章节给出 `task handoff` / `task handoffs` 用法；一次签名操作在同一事务中取消旧尝试、释放其活动 claim 并声明有界后继；旧任务保持取消，其 claim 下的迟到提交无法提交；取消不停止进程或逆转外部效果。

“Edit task instructions” 章节给出 `task edit` / `task edits` 用法；提供一个或两个替换字段，省略的文本保持不变，空 body 允许，标题必须非空；每个字段为有效 UTF-8 且最多 65536 字节；使用返回的后继 ID 继续工作，旧任务被取消且其 claim 被 fence。

### 4.11 Worker 注册、分配与优先级

`docs/reference/task-board.md` 的 “Inspect local Worker registration” 章节给出 `aig worker registrations WORKER_ID --json`；该命令返回当前元数据、活动 claim 数、完整注册历史与 profile promotion 记录及精确事实 ID；它不提供密钥或写入 SQLite 事实。

“Assign exact work” 章节给出 `task assign` / `task assignments` / `task reassign` 用法；assignment 命名一个 Worker ID；被选中的 Worker 在 SQLite claim 获取内部被检查，跨本地、harness 与 HTTP 入口；assignment 不改变冻结输入、authority、allowance 或输出资格。

“Prioritize eligible work” 章节给出 `task priority` / `task priorities` 用法；priority 是 -100 到 100 的整数，较高值优先考虑；无历史的任务有效值为零且无合成事实；这是单个扫描快照内的建议性排序，并发进程可能以不同顺序 claim，不保证全局 FIFO、公平性、老化或开始时间。

### 4.12 独立审查与变更请求

`docs/reference/task-board.md` 的 “Request an exact independent review” 章节给出在任务创建时声明独立验收策略的示例（`mode: independent`、`policy_version`、`required_attestations`、`verifier_capabilities`、`rubric_asset_ids`、`evidence_asset_ids`），以及 `task request-review`、`task review decide`、`task reviews` 用法。同一章节说明：审查者完成只创建普通报告，不使源工作合格；决策检查其精确 ID、输出名、不可变审查者祖先与生产者分离，然后使用现有策略绑定 attestation 所有者；操作者的裁决不会从报告文本自动推断。

“Continue after a changes decision” 章节给出 `task request-changes` / `task revisions` 用法；revision 原子取消旧源并发布有界子任务，保留所有仍未合格的输出槽；合格槽保持历史引用；源即使在后继验收后仍保持取消。

### 4.13 工作区升级与事件观察

`docs/reference/task-board.md` 的 “Upgrade an existing workspace” 章节给出 `aig domain upgrade --json` 与 `aig redis rebuild --json`；`upgrade` 只打开已存在的常规 Store，在 `.aig/backups/` 下保存私有权限的 SQLite 备份，然后应用现有迁移；任务事实、域身份与密钥不会被重置。

“Observe events and repair infrastructure” 章节给出 `task events` / `task watch` / `redis status` / `redis sync` / `redis rebuild` / `--offline task audit` 用法；事件 cursor 是 SQLite 记录 revision；watch 发出 JSON 行并在观察截止时停止，从不派发工作；完成的 watch 意味着观察正常结束，不意味着任务成功。

### 4.14 配置化运行与 DeepSeek 兼容端点

`README.md` 的 “Quick start” 章节给出配置化运行示例：

```bash
mkdir -p workspaces/report
export DEEPSEEK_API_KEY="..."
aig -C workspaces/report run --config /absolute/path/to/examples/report-consistency/run.toml \
  --wait-timeout 300 --json
aig -C workspaces/report run --config /absolute/path/to/examples/report-consistency/run.toml \
  --resume TASK_ID --wait-timeout 300 --json
```

同一章节说明：配置显式选择文件、Skill、根任务与 Worker profile；旧 `fleet run` 入口委托给同一应用服务；新启动拒绝历史输出槽，独立运行应使用新工作区。

`SKILL.md` 的 “Choose the integration mode” 章节给出 DeepSeek 兼容端点示例：

```bash
export AIGINEERING_API_KEY="$DEEPSEEK_API_KEY"
export AIGINEERING_MODEL="deepseek-flash"
export AIGINEERING_BASE_URL="https://api.deepseek.com"
aig run "基于 README 中的实现说明，生成中文能力、操作步骤和局限报告。" \
  --input readme=README.md --output report.md \
  --thinking-mode disabled --max-output-tokens 8192 \
  --response-format json_object
```

同一章节说明：`--json` 选择 CLI 输出，`--response-format json_object` 选择 provider 的 JSON wire 模式；适配器仍只编译规范 action wrapper，并拒绝格式错误或未知字段。

### 4.15 迁移现有 agent harness

`SKILL.md` 的 “Wrap an existing agent harness” 章节给出使用 `HarnessCandidateAdapter` 的签名协议操作示例（`claim_candidate`、`result_candidate`、`/worker/claims`、`/worker/submissions`），并说明 harness 必须返回恰好一个协议 action：`/exec` 只发布已声明输出；`/plan` 或 `/replan` 发布可独立 claim 的工作；`/tool`、`/parallel_tool`、`/retry` 或 `/fail` 使对应决策可见；并行调用成为普通工具任务加 `AND` 激活的继续。

## 5. 部分支持（partial）

以下能力在证据中被描述为已有开发验证或部分实现，但明确**尚未**达到稳定发布承诺：

- **Kanban CLI 目标**：`README.md` 的 “Current local preparation” 章节与 `ROADMAP.md` 的 “Next usability slice” 章节多次指出 “更广泛的 Kanban CLI 目标仍然开放”“更广泛的 Kanban 与稳定资格仍然开放”。
- **稳定发布资格与打标签**：`README.md` 与 `ROADMAP.md` 均声明稳定发布资格与打标签是独立收尾工作，尚未完成。
- **任务分配的实际 provider 输出门**：`README.md` 的 “Current local preparation” 章节指出 assignment 的确定性与已安装开发验证与 “待定的实际 provider 输出门” 分离。
- **历史已完成树的修复**：`README.md` 指出共享完成现在在每一层 fence 未完成后代，但 “历史已完成树仍需要授权修复”。
- **捕获的 Worker 日志与 tail**：`docs/reference/task-board.md` 的 “Inspect claim attempts” 章节指出这些是 “future work”。
- **多板管理**：`docs/reference/task-board.md` 的 “Discover and operate work” 章节指出 “Multi-board management remains future work”。
- **上下文与附件**：`ROADMAP.md` 的 “Next usability slice” 章节指出 “Captured logs/tail, context and attachments, followed by board management, remain planned through shared application owners”。
- **持久派发与计划启动、配置化通知/保留/PR 验收适配器**：`ROADMAP.md` 的 “Next usability slice” 章节将其列为后续步骤。
- **v0.6.0 准备**：`ROADMAP.md` 的 “v0.5.11 declarative method baseline” 章节指出更广泛的受控实验与更长链仍是 v0.6.0 准备，v0.6.0 发布未完成。

## 6. 未来方向（future）

`ROADMAP.md` 的 “Candidate directions” 章节列出未来候选方向：

- 生产级 MCP 传输与协议集成；
- 进程级工具超时、取消与隔离；
- 外部副作用的 exactly-once 协调；
- 跨机器 Store 与 Worker 发现；
- 部署安全 profile；
- 可复现的生产力与质量基准；
- 版本化 Contract-v6 迁移，使创建版本显式化，而不是从已填充字段选择 v3/v4/v5；
- 以 revision 水印的后果处理替代整日志恢复与完成扫描，而不引入调度器拥有的状态；
- 在支持的导入窗口后物理移除遗留 JSONL Store 代码。

`ROADMAP.md` 的 “v0.5.8 evidence and operability” 章节指出上下文加载优化、进程隔离、生产 MCP 与外部副作用保证仍是未来工作。

`ROADMAP.md` 的 “v0.5.9 portable business artifacts” 章节指出自动研究编排、已认证全文服务、XML 解析器、替代 OCR/解析器比较、语义 claim 验证、撤稿检查、大 blob 存储与 PDF 坐标叠加是后续工作。

`ROADMAP.md` 的 “v0.5.10 runtime primitive convergence” 章节指出物理 JSONL 所有权移除、增量后果水印与更深 ledger 提取仍是未来重构切片。

`ROADMAP.md` 的 “Next usability slice” 章节给出剩余工作顺序：

1. 修复缺失后代取消的历史已完成树，并在稳定收尾前对并发取消下的恢复调度进行资格验证；
2. 捕获日志/tail、上下文与附件，随后通过共享应用所有者进行看板管理；
3. 添加持久派发与计划启动，然后配置化通知、保留与精确 PR 验收适配器；
4. 将实际工作证据扩展到当前快照的独立验收与更广泛恢复案例，然后完成稳定发布资格；
5. 将受控生产力实验、分布式身份与 schema 迁移与 CLI 可用性目标分开。

## 7. 已知限制与明确非目标

`README.md` 的 “Non-goals” 章节明确当前运行时**不**声称：

- 跨机器共识或分布式 Store 语义；
- 公网部署加固；
- 外部安全审计；
- 模型产生内容的语义真值；
- 跨独立机器的调度公平性；
- 通用工作流语言；
- 生产 MCP 传输；
- 进程级工具超时、取消或隔离；
- 外部副作用的 exactly-once 投递；
- 跨机器 Store 与 Worker 发现。

同一章节指出：可选 HTTP 适配器绑定到本地参考运行时；认证、TLS、限流与敌对网络控制仍是部署责任。

`ROADMAP.md` 的 “Non-goals” 章节补充：Aigineering 不打算成为通用 prompt harness、静态 DAG 工作流引擎、隐藏的多 agent swarm 调度器、可变对话状态容器、Worker 或工具直接写入运行时事实的系统，或正确性依赖单个 Engine 进程存活的系统。

`docs/reference/task-board.md` 的 “Prioritize eligible work” 章节指出：priority 是单个扫描快照内的建议性排序，并发进程可能以不同顺序 claim，不保证全局 FIFO、公平性、老化或开始时间；高优先级到达可能使其他工作饥饿；retry、recovery、handoff、revision 与子任务不继承 priority。

`docs/reference/task-board.md` 的 “Inspect claim attempts” 章节指出：`runs` 显示 claim 分配与已证明的提交、拒绝、调用失败、释放或过期；仅分配不证明 Worker 被调用；`submitted` 不使输出合格；`lease_elapsed` 意味着观察到的租约时间已过但没有持久过期事实；当前 Store 至多授予一个不可变任务一次；诊断省略原始输出、prompt、任意错误文本与 trace 正文；遗留或非规范包值显示为 `null`，这不证明不存在包。

`docs/reference/task-board.md` 的 “Request an exact independent review” 章节指出：审查者完成只创建普通报告，不使源工作合格；操作者的裁决不会从报告文本自动推断；不同签名身份不证明不同的人或独立模型。

`SKILL.md` 的 “Interpret projections” 章节指出：`completed` 仅在已声明输出满足时才是成功；`failed`、`cancelled`、`unreachable` 是终端失败；`expanded`、`ready`、`claimed`、`submitted`、`blocked`、`stalled` 不是成功；超时不是完成；`output_truncated` 与 `empty_response` 是持久 Worker 失败类别；任何拒绝都需要在信任下游结果前审计；provider 失败后缺失 usage 不证明零外部成本；父任务完成取决于已声明输出，而不是每个后代都达到终端状态。

`SKILL.md` 的 “Preserve the boundary” 章节指出：默认 `None` 在外部类型化或元组 handler 上是受信任的遗留兼容性，不是对不可信扩展的隔离；不得直接写入 `.aig/store.db` 或重用已签名 claim 命令；标签在任务构造时解析为精确上下文 Asset ID，标签不授予 authority 也不提供动态重放查找；相似性是建议性的，不能转移 authority；私钥、密封值与未披露 Asset 必须保持在 prompt、trace、日志与 Candidate 元数据之外。

## 8. 测试证据与稳定承诺的区分

证据中反复出现以下区分，本报告据此区分测试证据与稳定承诺：

- `README.md` 的 “Current local preparation” 章节指出 v0.5.12 快照完成了确定性、打包、已安装产物、链式与嵌套重规划验证，但这是 “locally verified release preparation, not a published or tagged release”。
- `ROADMAP.md` 的 “Release gates” 章节列出每个稳定发布必须通过的十项门禁，包括候选/事实、authority、披露、claim 与终端回归测试；Memory/SQLite 一致性与崩溃原子性测试；物化删除与重建且语义摘要匹配；并发 Worker 与陈旧 claim fencing 测试；规范签名 JSON 与协议向量验证；Ruff check 与 format；完整确定性测试套件；wheel 与 sdist 构建及元数据验证；安装状态 CLI 与数据库重开冒烟测试；有界真实 LLM 系统、Worker 与端到端场景。
- `ROADMAP.md` 的 “Next usability slice” 章节指出 “Development does not create a release tag; stable qualification and tagging remain a separate closure”。
- `README.md` 的 “Current local preparation” 章节指出 “Stable-release qualification and tagging remain a separate closure”。

因此，本报告不将任何 development verification 或 bounded development evidence 表述为稳定发布承诺。

## 9. 有界真实仓库报告执行证据及其明确限制

`README.md` 的 “Current local preparation” 章节记录了一次**有界真实仓库报告执行**及其内容审查缺陷：

- 该章节指出 “A bounded real repository-report execution and its content-review defects are recorded in [report 073](reports/073-required-redis-live-report-2026-10-04.md)”。
- `ROADMAP.md` 的 “Next usability slice” 章节进一步说明：一次真实 DeepSeek Flash 仓库报告任务覆盖了 configured execution、exact export、completed-root resume 与 reconstruction；独立内容审查发现了命令遗漏；原始输出与更正证据保留在 `reports/073-required-redis-live-report-2026-10-04.md`。
- 同一章节明确：**不推断任何独立策略 attestation 或一般语义正确性**（“No independent policy attestation or general semantic correctness is inferred”）。

该执行证据的明确限制：

- 它是一次**有界**执行，不是稳定发布资格，也不是对报告内容语义正确性的证明；
- 它记录的是 configured execution、exact export、completed-root resume 与 reconstruction 的覆盖，以及独立内容审查发现的命令遗漏缺陷；
- 它不构成独立策略 attestation，也不构成对模型输出语义真值的保证；
- 它不改变本报告第 2 节关于 v0.5.13.dev0 与 v0.5.12 均非已发布/已打标签版本的结论。

`ROADMAP.md` 的 “Next usability slice” 章节还指出，有界仓库报告循环现在在 report 078 中具有已接受的冻结输入 revision 证据（“The bounded repository-report loop now has accepted frozen-input revision evidence in report 078”），但稳定发布资格与打标签仍是独立收尾工作。

## 10. 明确不作结论的事项

依据四份冻结证据，本报告对以下事项**不作结论**，因为证据未明确支持：

- 任何超出证据的质量提升、成本下降、隔离保证或外部副作用 exactly-once 保证；
- 模型产生内容的语义真值；
- 跨机器共识、分布式 Store 语义或跨机器发现；
- 公网部署加固或外部安全审计；
- 调度公平性；
- 生产级 MCP 传输；
- 进程级工具超时、取消或隔离；
- 外部副作用的 exactly-once 投递；
- 任何私有计划、私有报告、凭据、原始 Store 或外部来源的内容。

## 11. 参考的相对路径与章节

本报告引用的仓库内相对路径与可定位标题/章节：

- `README.md`：章节 “Aigineering”、“Quick start”、“Current local preparation”、“Reference runtime scope”、“Non-goals”、“Core boundary”、“Development”、“License”。
- `SKILL.md`：章节 “Aigineering trustworthy execution”、“Required collaboration service”、“Choose the integration mode”、“Publish one trustworthy task”、“Wrap an existing agent harness”、“Interpret projections”、“Preserve the boundary”。
- `docs/reference/task-board.md`：章节 “Task board and Redis operations”、“Discover and operate work”、“Inspect claim attempts”、“Apply an operation to several tasks”、“Task conversations”、“Hold work before it starts”、“Recover manually held work”、“Order work with exact prerequisites”、“Hand off unfinished work”、“Edit task instructions”、“Inspect local Worker registration”、“Assign exact work”、“Prioritize eligible work”、“Request an exact independent review”、“Continue after a changes decision”、“Upgrade an existing workspace”、“Observe events and repair infrastructure”。
- `ROADMAP.md`：章节 “Next usability slice”、“Earlier local release preparation”、“v0.5.11 declarative method baseline”、“Release gates”、“v0.5.8 evidence and operability”、“v0.5.10 runtime primitive convergence”、“v0.5.9 portable business artifacts”、“Candidate directions”、“Non-goals”。