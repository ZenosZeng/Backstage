---
name: backstage-memory
description: 在使用 Backstage 的多仓工作区中读取和追加可追溯记忆，按本机配置同步 S3，并维护跨仓任务状态。适用于实质性开发或任务接续，不用于简单闲聊。
---

# Backstage 工作区记忆

以下命令从工作区的 `.agents/` 目录执行。工具核心走 Git；共享内容在 `.share/`；
本机配置是 `config.json`，运行状态在 `.local/`。

## 接续任务

读取本机 `config.json`。仅当 `sync.enabled` 和 `sync.initialized` 都为 `true`
时运行 `python3 scripts/sync.py sync`；本地模式直接读取现有记忆。
同步报告冲突时保留本地和远端证据，核对后处理，不用强制覆盖绕过检查。

读取存在的 `.share/knowledge/_workspace.md`。相关 repo 已注册到 `projects` 时优先用
`brief --project <name>`；已知 task ID 时用 `brief --task <id>`。
没有项目或任务范围时用 `recent --current --summary --limit 10`。

```bash
python3 scripts/memory.py brief --task <task-id>
python3 scripts/memory.py search "<相关关键词>" --current --limit 10
python3 scripts/memory.py get <event_id>
```

摘要只用于定位，重要结论核对原始来源与当前代码或真实环境。`--current` 只排除被显式
取代的事件；旧待办和未知任务状态不能当成当前执行授权。

## 追加证据

确认可复用的事实、决策、测试结果、风险或偏好后追加事件，填写实际 Agent：
`codex`、`claude`、`kimi` 或 `dsh`。`--project` 仅填本机已注册的项目，
同一跨仓任务使用同一个 `--task`。保留 `--where` 来源与 `--verified-by` 验证依据。

```bash
python3 scripts/memory.py add --agent <agent> --type fact \
  --topic <topic> --title "<标题>" --what "<已确认结论>" \
  --where "<来源>" --verified-by "<验证依据>"
```

假设使用 `--type hypothesis`；修正已有结论用 `--supersedes <event_id>`。
不保存 secret、完整聊天、原始日志或无复用价值的命令记录。
配置了 S3 同步时执行 `python3 scripts/sync.py push-memory`，仅上传本机 raw memory。

用户明确开始、恢复、暂停或确认完成任务时，用 `memory task` 追加
`active`、`paused` 或 `closed` 状态，并填写 `--reason` 与 `--where`。
多个状态 head 冲突须核对全部来源，再逐个用 `--resolve` 指定；普通进展不能自动重开任务。

## 共享内容

修改 `.share/agent/`、`.share/knowledge/` 等共享内容前运行 `sync-shared` 比较
本地、S3 和同步基线；无分叉时修改并用 `push-shared` 发布。
分叉时先核对 `.local/.sync/conflict/` 的基线与远端快照，合并后用 `resolve-shared`。
本地模式可以维护本地内容；接入 S3 后才使用这些发布命令。
