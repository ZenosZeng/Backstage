# 从空工作区开始

这份示例先验证本地记忆读写，再接入私有 S3。只使用记忆脚本时需要 Python 3.10+；
不需要 Pixi、飞书凭据或 Agent CLI。

`workspace/` 是 `.share/` 的最小模板，包含通用的 `backstage-memory` Skill、
`AGENTS.md` / `CLAUDE.md` 入口和工作区知识首页。它们不包含私人项目、凭据或历史记忆。

## 1. 创建本地示例

以下命令用于一个全新的演示目录。已有工作区应按需合并模板，保留自己的配置和知识。

```bash
mkdir -p ~/backstage-demo
git clone https://github.com/ZenosZeng/backstage.git ~/backstage-demo/.agents
cd ~/backstage-demo/.agents

cp config.template.json config.json
mkdir -p .share/docs .share/setup
cp -R examples/workspace/. .share/

python3 - <<'PY'
import json
from pathlib import Path

path = Path("config.json")
config = json.loads(path.read_text())
config.update(
    machine_id="demo-machine-01",
    machine_role="development",
    workspace_root=str(Path.cwd().parent),
)
config["sync"]["enabled"] = False
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
PY
```

本地模式保留空的 `sync.remote` 和 `sync.initialized=false`，只运行 `memory` 命令。
此阶段不会向 S3 上传内容，也不会修改 Agent 的 Skill 或入口链接。

## 2. 写入并查询第一条记忆

在 `.agents` 目录执行：

```bash
python3 scripts/memory.py validate
python3 scripts/memory.py task --task demo-onboarding --state active --agent codex \
  --reason "验证本地记忆流程" --where "examples/README.md"
python3 scripts/memory.py add --agent codex --task demo-onboarding --type fact \
  --topic demo/onboarding --title "本地工作区已初始化" \
  --what "已从公开模板生成本机配置，记忆校验通过" \
  --where "config.json" --verified-by "python3 scripts/memory.py validate"
python3 scripts/memory.py brief --task demo-onboarding
python3 scripts/memory.py task --task demo-onboarding --state closed --agent codex \
  --reason "本地读写验证完成" --where "memory brief --task demo-onboarding"
```

`brief` 应显示 `active` 状态、刚写入的事实和来源 ID。关闭后显式指定 `--task`
仍能查到历史。这里的任务属于工作区；需要 repo 级记忆时，再按
[项目注册表说明](../docs/memory.md#新机器初始化)配置 `projects`。

## 3. 创建首个共享工作区

准备 MinIO Client（`mc`），在本机配置好私有存储 alias，选择一个专用的空前缀。
下面的 `my-store/my-bucket/backstage` 是示例，需要改成自己的目标；存储凭据由 `mc`
管理，不写进 `config.json`。

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("config.json")
config = json.loads(path.read_text())
config["shared_writer"] = True
config["sync"].update(enabled=True, remote="my-store/my-bucket/backstage")
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
PY

python3 scripts/sync.py init --push
python3 scripts/sync.py sync
```

`init --push` 发布示例和本机记忆，建立同步基线；后续 `sync` 为 Codex、Claude Code
和 Kimi 建立 `backstage-memory` Skill 链接，并将工作区的 `AGENTS.md` / `CLAUDE.md`
链接到模板。已有不同来源的入口会被保留并提示冲突，按需合并后再使用。

共享示例上传前，可以编辑 `.share/agent/` 和 `.share/knowledge/`，加入自己的工作规则
和项目导航。以后更新这些文件时使用 `sync-shared` / `push-shared`，分叉处理见
[记忆参考](../docs/memory.md#shared-分叉保护)。

## 4. 加入另一台机器

在另一台机器安装公共核心、配置相同存储 alias 后，使用另一个唯一机器名：

```bash
python3 scripts/sync.py init --pull \
  --remote my-store/my-bucket/backstage \
  --machine-id dev-machine-02 --machine-role development \
  --workspace-root ~/code
```

新机器默认不是 shared writer；它可以追加并上传自己的 raw memory，读取其他机器的
记忆和共享模板。完整日常操作见 [项目首页](../README.md#日常使用)。
