# Aigineering

[English](README.md) · [中文](README.zh-CN.md)

**Zero Trust agent runtime for auditable AI-assisted engineering and research.**

Aigineering 面向已有 Agent Harness，提供可持久、可检查的外派执行边界：明确输入与输出，
限制权限，通过领取绑定执行，并按预先确定的政策接受结果。Worker 提交候选结果；
只有通过认证和授权的效果，才能经事务提交成为持久事实。

内容寻址固定被检查的资产版本；记录下来的血缘帮助回查证据、复核和修订可疑结果，
减少重新拼凑模型内部执行现场的需要。内容是否正确，仍需相应业务检查。

## 现在可以做什么

- 从明确文件启动任务，或配置带 Skill 和工具的 Worker Fleet。
- 通过 `/plan`、`/replan` 动态展开为受约束的普通任务。
- 用 CLI 看板查看状态、阻塞、领取、证据和恢复后继。
- 审查精确输出 Asset，导出满足任务接受政策的结果。
- 保留已有 Harness 的模型和工具循环，通过签名 Worker 接口接入。

当前源码为 **v0.5.14**，面向有界本地单机 CLI 工作流。
SQLite 保存事实与领取；应用发现、事件和唤醒**需要 Redis**。版本元数据本身
不构成交付资格，相关结论须有适用场景的证据。一般无人值守交付、生产 MCP
transport 和分布式部署不在当前支持范围内。参见[支持范围与限制](docs/reference/development-status.md)。

## 从一个任务开始

需要 Python 3.11+ 和 Redis。运行仓库自带示例时采用源码 checkout。

```bash
git clone https://github.com/Leartin-Lab/aigineering.git
cd aigineering
python -m venv .venv
source .venv/bin/activate
pip install -e .
docker compose up -d redis
aig redis status --json

export AIGINEERING_API_KEY="your-provider-key"
export AIGINEERING_MODEL="your-model"
export AIGINEERING_BASE_URL="https://your-provider.example/v1"

mkdir -p workspaces/quick
aig -C workspaces/quick run "汇总给定测量数据，说明结果与局限。" \
  --input sample="$PWD/examples/cli_research/sample.json" \
  --output report.md --timeout 60 --max-retries 1 \
  --max-output-tokens 8192 --json
```

选择能够遵循 Worker 动作协议的兼容模型，显式填写 provider 设置；密钥只放在环境变量中。
报告写入 `workspaces/quick/report.md`，已有文件不会被覆盖。这个快速入口不授予工具，
也不会自动要求独立接受。

用返回的 `contract_id` 查看实际任务：

```bash
aig -C workspaces/quick task status TASK_ID --json
aig -C workspaces/quick task board --json
aig -C workspaces/quick task audit TASK_ID --json
```

## 动态规划、工具与独立接受

[CLI 研究示例](examples/cli_research/README.md)只提供根任务、冻结输入与 Skill，由模型规划
工作。两个工具计算样本均值并换算单位，另一授权 Worker 检查精确报告。示例没有自己的
任务调度器。

预设选择 `deepseek-flash`；使用其他兼容 provider 时修改 `workers.toml`。示例适配器也必须
可被 Python 导入：

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p workspaces/research
aig -C workspaces/research run \
  --config "$PWD/examples/cli_research/run.toml" --wait-timeout 300 --json
```

Skill 提供被选中的上下文，不授予权限。`aig mcp add` 只登记能力描述，不连接 MCP 服务。
可以保留已有 Harness 的 MCP client，或配置明确的工具/Worker 适配器。
参见[配置运行](docs/reference/configured-runs.md)和[Harness 接入](docs/reference/agent-harness-migration.md)。

## 停住以后怎么办

先读 `task status`，再针对相关任务和 claim 查询 `runs`、`diagnostics`、`lineage` 或 `logs`。
已提交、工具观察已产生、恢复已安排，都不等于业务完成。沿用配置和 `--resume TASK_ID`
继续可运行任务；已发布的恢复后继有自己的 ID。等待接受时不要重复运行已提交的生产者。

`--timeout` 限制单次 provider 请求；`--wait-timeout` 用于目标/配置运行，不用于快速目标。
provider 重试与新的
runtime 恢复任务不同。`--json` 控制 CLI 输出，不控制 provider 的 JSON 响应模式。
各命令有自己的结果结构和退出码。详细操作见[任务管理](docs/reference/task-board.md)。

## 后续阅读

- [Agent 操作指南](SKILL.md)：简短闭环，详细材料按需加载。
- [示例](examples/README.md)与[实例报告](reports/README.md)：输入、结果和限制。
- [文档入口](docs/README.md)：配置、任务操作和接入。
- [路线](ROADMAP.md) · [贡献指南](CONTRIBUTING.md) · [许可证](LICENSE)。
