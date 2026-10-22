# 记忆与同步参考

[返回 Backstage 首页](../README.md)

本文说明记忆布局、机器初始化、查询与同步一致性。独立监控和飞书遥控见
[工具说明](../tools/README.md)。

公共仓库只保存可复用代码；工作区专属的 Skill、Prompt、项目知识和记忆保存在私有 S3 兼容存储中。Git、源码、配置、测试结果和实际环境始终是真实来源，memory 只作为可追溯的导航和认知缓存。

## 存储边界

| 层级 | 内容 | 真实来源 |
|---|---|---|
| 公共核心 | `config.template.json`、`scripts/`、`tools/`、`tests/`、Pixi 环境、CI 和文档 | Git |
| 共享工作区状态（本地收纳于 `.agents/.share/`） | `agent/prompts/`、`agent/skills/`、`knowledge/`、`docs/`、`setup/` | S3 |
| 原始记忆事件 | `memory/<machine>/<agent>/<UTC-date>.json` | S3 |
| 本机运行状态（本地收纳于 `.agents/.local/`，`config.json` 保留根目录） | `config.json`、`.locks/`、`.sync/`、`config/`、`tools/` 服务状态 | 仅本机 |

`.share/` 各目录的分工（2026-09-11 重排，旧名见下表）：

| 目录 | 内容 |
|---|---|
| `agent/prompts/` | `AGENTS.md` / `CLAUDE.md`——各 Agent 的入口 Prompt |
| `agent/skills/` | Skill 库（`common/ eval/ inference/ memory/ train/`） |
| `knowledge/` | 策展长期知识：`_workspace.md`、`projects/`、`timeline/` |
| `docs/` | 项目工作文档：`b1k/`、`goai/` |
| `setup/` | 机器部署手册 `onboarding/` 与工作目录速查 |

原始事件按机器和 Agent 分区，每台机器只上传自己的前缀。共享状态采用完整目录同步，并使用本地基线检测本地与 S3 是否发生分叉。

**2026-09-11 结构重排**（`SHARED_DIRS` 随之更新，远端目录名同步变更）：

| 旧 | 新 |
|---|---|
| `config/prompts/` | `agent/prompts/` |
| `skills/` | `agent/skills/` |
| `long-term/` | `knowledge/` |
| `shared_files/` | `docs/` |
| `shared_files/b1k-docs/` | `docs/b1k/` |
| `shared_files/goai-docs-sim/` | `docs/goai/` |
| `shared_files/goai-docs-real/data_audit_20260826/` | `docs/goai/data-audit-20260826/` |
| `shared_files/onboarding/` | `setup/onboarding/` |

旧远端前缀不再同步；其他机器 pull 后本地残留的旧目录可手动清理。`memory/` 中的历史事件
保持原样（记录的是写入当时的路径），迁移本身不追改历史。

记忆核心只依赖 Python 标准库和 MinIO Client（`mc`），不需要数据库、MCP Server 或本地搜索服务。
工具环境另由 Pixi 锁定 Python 和飞书 SDK，不借用业务仓库环境。

## 公共仓库结构

```text
.
├── .github/workflows/tests.yml
├── config.template.json
├── pixi.toml / pixi.lock
├── tools/                    # watchdog、飞书遥控、独立服务管理
├── scripts/
│   ├── memory.py
│   └── sync.py
└── tests/
```

`config.template.json` 随 Git 发布。执行 S3 初始化后，本地会出现 `.share/`（`agent/prompts/`、`agent/skills/`、`knowledge/`、`docs/`、`setup/`、`memory/`）和 `.local/`（`.locks/`、`.sync/`）；这些工作区专属内容均被 Git 忽略，顶层只保留 `config.json`。

## S3 结构

```text
<remote>/
├── agent/
│   ├── prompts/
│   └── skills/
├── knowledge/
├── docs/
├── setup/
└── memory/
    └── <machine>/<agent>/<UTC-date>.json
```

S3 目录与本地 `.share/` 下的目录一一对应（远端目录名不变，仅本地收纳位置统一在 `.share/` 下）。S3 不设置 release 目录，也不额外实现对象版本管理。Git 中的 `config.template.json` 是新机器初始化的唯一模板；它不包含 remote、项目名称或仓库路径。初始化时会基于它生成本机专属、不会上传的 `config.json`。

## 本地初始化（无需 S3）

在已克隆的 `.agents/` 内运行 `python3 scripts/sync.py --dry-run init --local --machine-id <唯一机器名>`
预览，再去掉 `--dry-run` 安装。默认工作区为仓库父目录；可传 `--workspace-root`。
它补齐公开示例模板、本地配置、三个 Agent 的 Skill 链接和工作区入口；不启动服务。
已有文件保留，入口冲突会在安装前阻止写入；重复运行不改配置，不关闭已有 S3 同步。
完整操作及手工验证路径见 [最小工作区示例](../examples/README.md)。

## 新机器初始化

前置条件：

- Python 3.10 或更高版本
- Git
- MinIO Client（`mc`），并已在本机配置私有 S3 alias
- 工作区根目录，例如 `~/code`

先克隆公共核心：

```bash
git clone https://github.com/ZenosZeng/backstage.git ~/code/.agents
```

再从 S3 初始化：

以下命令用于加入已有共享工作区。远端须已包含自己的 Skill（至少一个
`agent/skills/<分类>/<name>/SKILL.md`）和 `agent/prompts/{AGENTS,CLAUDE}.md`。
公共仓库不附带私人 Skill 或项目知识，空 S3 前缀不能直接作为已初始化工作区使用。

```bash
~/code/.agents/scripts/sync.py init --pull \
  --remote <mc-alias>/<bucket>/<prefix> \
  --machine-id <唯一机器名> \
  --machine-role <机器用途> \
  --workspace-root ~/code \
  --clear-proxy
```

只有负责发布共享状态的指定机器才添加 `--shared-writer`。复用机器 ID 前必须确认旧实例已经停止，并显式添加 `--reuse-machine`。

初始化过程会：

1. 读取 Git 中的 `config.template.json`，并下载 S3 shared 内容；
2. 生成本机 `config.json`；
3. 拉取其他机器的 raw memory；
4. 将全部共享 Skill 同源链接到 Codex、Claude 和 Kimi，遇到同名不同来源的已有 Skill 时拒绝覆盖；
5. 创建工作区 `AGENTS.md` 和 `CLAUDE.md` Prompt 链接；
6. 校验 memory 数据。

旧分类目录中的同名 Skill 副本不会盖过其嵌套规范入口。不同分类下无层级关系的
同名 Skill 会被拒绝，避免链接到不确定来源；不会覆盖 Agent 已有的独立配置。

公开模板不包含 `projects`。需要 repo 级记忆时，在本机 `config.json` 中按需加入项目注册表；该信息不会上传到公共 Git：

```json
{
  "projects": {
    "repo-name": {
      "path": "relative/path",
      "description": "仓库用途"
    }
  }
}
```

不配置 `projects` 时，workspace 级 memory 仍可正常使用。

### 创建首个共享工作区

第一次使用可从 [公开最小示例](../examples/README.md) 开始：先运行本地记忆流程，
再接入自己的存储。`examples/workspace/` 提供通用 Skill、Prompt 和知识入口，
复制到新工作区的 `.share/` 后按需编辑；已有工作区应合并所需内容。

1. 从根目录 `config.template.json` 创建本机 `config.json`，填写唯一 `machine_id`、
   `machine_role`、`workspace_root` 和 `sync.remote`，将 `shared_writer` 设为 `true`。
2. 在 `.share/agent/skills/` 放入自己的 Skill，在 `.share/agent/prompts/` 准备
   `AGENTS.md` 和 `CLAUDE.md`；项目长期知识放入 `.share/knowledge/`。
3. 校验后向专用空前缀初始化上传。此操作会发布共享文件，须先确认内容不含凭据。

```bash
pixi run memory validate
pixi run sync init --push
pixi run sync sync
```

后续机器使用上面的 `init --pull`。不要直接复制原机器的 `config.json`，也不要为了
绕过已有远端保护而添加 `--force`。同步凭据由本机 `mc` 管理，不填写到 JSON 中。

## 常用命令

```bash
# 查询和校验 raw memory
python3 scripts/memory.py status
python3 scripts/memory.py recent --limit 20
python3 scripts/memory.py search "关键词"
python3 scripts/memory.py recent --project repo-name --machine machine-name --current --limit 10
python3 scripts/memory.py recent --since 2026-08-01 --until 2026-09-01 --limit 100
python3 scripts/memory.py get <event_id>
python3 scripts/memory.py audit --project repo-name
python3 scripts/memory.py validate

# 只上传本机 raw memory
python3 scripts/sync.py push-memory

# 日常同步 raw memory 和 shared
python3 scripts/sync.py sync

# 单独处理 shared
python3 scripts/sync.py pull-shared
python3 scripts/sync.py push-shared
python3 scripts/sync.py sync-shared
python3 scripts/sync.py resolve-shared

# 发布单个有明确所有权的共享文件，并拉取其他远端更新
python3 scripts/sync.py publish-shared-file \
  docs/project-docs/report.md
```

`--machine`、`--project`、`--task`、`--agent`、`--topic` 是精确筛选，关键词搜索为
所有词同时匹配。时间区间含起点、不含终点，日期按 UTC；带时区时间会换算后比较。
`--current` 排除显式 superseded 的事件，不代表已验证，也不会自动合并同 topic 的不同结论。
写周报/月报时保留历史，不加 `--current`。`get` 返回完整事件和真实来源路径。

`search` 默认按相关性排序：标题、topic、task ID、project 命中优先于正文；同分按时间
倒序。空格分隔关键词仍为 AND 匹配，`--sort recent` 恢复旧排序。文本默认短摘要
（`--summary-chars 240`），`--full` 查看完整正文；`--json` 仍返回完整 event array。
`recent` 保持原有时间倒序及全文默认值，可加 `--summary`。

## 精简上下文与任务生命周期

```bash
pixi run memory brief --project repo-name --max-chars 6000
pixi run memory brief --task release-check
pixi run memory search "接口" --sort recent --full
pixi run memory search "接口" --json
```

`brief` 展示约定与决策、事实与验证记录、进展、风险和待办候选。完全相同的内容合并
展示但保留全部来源；不同结论并列，不自动把最新说法当作正确说法。默认最多 20 组，
在前四类内容间轮流分配名额，各类内部按时间倒序，避免进展挤掉约束；每条 240 字符，
文本总计 6000 字符。可用 `get <event_id>` 展开完整依据。

`brief --project <name>` / `brief --task <id>` 提供按来源摘录的精简上下文，始终排除
显式取代事件。任务状态在日期、作者和项目过滤之前从全部事件解析，不能用旧日期窗口
绕过后来的暂停/关闭。`--limit` 控制记录组数，`--summary-chars` 控制单条摘要，
`--max-chars` 控制文本总长度（JSON 不做总长度截断）。未展示条目数和文本截断会提示。
这不是长期记忆整理器：不读写长期 Markdown、不调用模型、不根据标题猜任务是否结束。

```bash
pixi run memory task --task release-check --state active --agent codex \
  --project repo-a --project repo-b --reason "验证跨仓库接口" --where "需求单 #123"
pixi run memory task --task release-check --state paused --agent claude \
  --reason "等待测试机器" --where "用户暂停指令"
pixi run memory task --task release-check --state closed --agent codex \
  --reason "验收完成" --where "测试报告与 PR #456"
```

恢复用 `--state active`。已有任务可直接暂停/关闭，无需补建；后续状态事件继承 repo
关联。状态记录不执行暂停、取消作业或合并 PR 等外部操作。

`task --task <id> --state active|paused|closed --agent <agent> --reason <原因>
--where <依据>` 追加生命周期决定，首次创建可重复传 `--project` 关联多个仓库。
使用 schema v1 的 `decision` + `content.task_state`，`supersedes` 仅引用同任务先前
的生命周期记录；不会取代事实、风险或测试记录。任务 ID 是工作区内的稳定唯一标识。
普通事件也不能取代生命周期事件。追加/导入会对现有事件与整个批次合并校验，允许批次
内父子乱序；生命周期引用缺失、跨任务、跨类型或成环时整批拒绝写入。同步安装前对本机
与远端事件联合校验，支持跨机器引用；发现非法引用不会部分安装新文件。
同机操作在现有 memory-write 锁内读取 heads 并原子追加；跨机同时追加会留下多个 heads，
显示冲突，不按时间戳裁决。人工核对后，用重复 `--resolve` 指定全部 heads 追加解决记录。

默认项目 brief 隐藏 paused/closed 任务；显式 `--task` 或 `--include-inactive` 可展示，
但不推荐其待办。冲突任务也不推荐待办。没有生命周期记录的旧任务保留为 unknown；
仅有稳定 task ID 且状态 active/unknown 的 active 事件，其 next 字段才列为待办候选。
它仍可能过时，不代表执行授权。普通 `add` 不改变任务生命周期，重开必须显式设 active。
跨机器使用前先同步，CLI 不自动发布到 S3；旧工具能读取事件但不理解生命周期投影。

## 审计与退出码

`audit` 默认检查全部事件，支持相同筛选和 `--stale-days`（默认 60 天）。它只报告
缺来源、过期候选、悬空/循环 supersedes、并行取代候选及历史时间格式，不重写记忆。
生命周期非法引用也会使 `validate` 失败；`audit` 报告 `invalid_lifecycle_reference` 或
`lifecycle_cycle`。多个有效状态 head 报告 `task_state_conflict`，包括没有共同父记录的
独立创建；合法并发冲突可同步保存，由 Agent 核对后解决，不按时间自动裁决。
正常查询成功返回 0；`get` 未找到、`audit` 有待核验项、`validate` 校验失败返回 1；
参数、I/O 或冲突错误返回 2。历史非 UTC 时间和日期错位保留原始来源，仅审计提示。

## Raw 一致性

- 同 ID、同内容导入幂等；同 ID、不同内容（包括不同机器或日期）拒绝，整批先检查再写入。
- 同机写入与安装共用短时本地锁，每个 daily JSON 原子替换；写入期间不等待 S3。
- 同机 push/pull 串行。push 上传已验证的 daily JSON 快照，不上传原子写临时文件；
  上传期间新增事件留到下一次 push，失败不删除本地事件。
- pull 先下载、完整校验，再原子安装每个文件。远端缺少已知事件或同 ID 内容变化时
  拒绝整批安装，`.local/.sync/raw-conflict.json` 只记录 ID，不备份事件正文。
- 正常 pull 不覆盖本机目录；恢复机器用 `init --pull --reuse-machine` 才导入本机历史。

同 ID 的脱敏修改也会触发保护。先核对产生机器、S3 和本地内容，得到明确授权后进行
定向脱敏或替换；不要自动 union、删除本地文件绕过检查，或复制凭据到冲突日志。
新事件仍使用 UTC，历史记录不自动搬家。

## 维护边界

继续使用 daily JSON + 中文 Markdown，不引入数据库/MCP，也不迁移已有事件。
Agent 日常只读相关摘要和少量事件，按 ID 取证；整理长期记忆时按项目分节，机器信息
写清 machine_id、证据日期和事件 ID。周报/月报使用时间筛选，不把运行状态当成永久事实。

工具代码升级走 Git，S3 只同步共享内容，不会自动把新 CLI 分发到其他机器；新 Skill
在旧 CLI 上应降级到原有查询。**目录结构变更（如 2026-09-11 的 `.share/` 重排）需要
各机器先升级 CLI（`git pull`）再同步**，否则旧 CLI 仍按 `SHARED_DIRS` 找旧目录。

已知限制：本地锁不能协调两台机器复用同一个 machine_id；shared 基线比较不是远端
CAS 事务，检查后到上传期间仍有竞态，应遵守指定 writer/文件所有权，避免同时发布。
raw 仍全量下载，安装仅保证每个 JSON 原子性，不保证跨文件事务；中断后可重新同步。
secret 检查只是结构化敏感键和常见凭据模式的防线，不能证明自然语言一定不含密码。
本轮不自动删除、压缩或语义裁决历史事件；容量明显增长后再评估增量同步或检索索引。

## Shared 分叉保护

最近一次成功对齐的 shared 完整副本缓存在 `.local/.sync/base/`。同步前会比较三份内容：

1. 上次共同基线；
2. 当前本地 shared；
3. 当前 S3 shared。

下载远端后、安装或文件发布前会重新核对本地哈希；期间有编辑则保留本地并保存远端
冲突快照，重新同步或合并后再发布。共享锁只协调同步命令，普通编辑器不受锁约束；
哈希复核不提供文件系统级事务。

每个文件按相对路径和 SHA256 比较。只有一侧变化时自动同步；本地和 S3 都相对基线发生变化时，不覆盖任何一方，而是将远端快照和差异报告保存到 `.local/.sync/conflict/`。

人工将 `.local/.sync/conflict/remote/` 合并到当前本地 shared 后运行：

```bash
python3 scripts/memory.py validate
python3 -m unittest discover -s tests -v
python3 scripts/sync.py resolve-shared
```

`resolve-shared` 上传前会再次检查 S3。如果人工合并期间远端再次变化，操作会停止并要求重新合并。

整包上传还会拒绝三类同路径风险：本地仍为基线旧版、双方修改且内容不同、远端删除而
本地仍保留基线原文。哈希不能判定第三版是否正确合并；请保留本地修改，先从冲突快照
对齐受阻路径（含采纳删除）并 resolve，再重新应用、核对和发布本地修改。本地单侧修改、
双方内容一致可正常通过；远端删除而本地已编辑的文件仍由人工决定是否保留。

`--allow-non-writer` 仅用于用户明确授权的维护迁移，不会绕过分叉检测。

`publish-shared-file` 用于 request/report 这类分属不同机器维护的文件：只上传
指定文件，同时把远端其他文件合并到本地；若远端也修改了同一个文件则拒绝
覆盖。它不适合发布 Skill、长期知识或一组相互依赖的共享文件。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 安全约束

禁止向 Git、长期记忆或 raw memory 写入 API Key、Access Key、Token、密码、私钥、完整聊天记录或其他凭据。S3 凭据只保存在本机 `mc` 配置中。

## 摘要、检索与交接（v0.7）

`brief` 文本先为不同类别选择完整短条目，再用剩余字符预算展开。任务概览最多展示
8 项，最多占总字符预算约五分之一；其余任务只提示数量。每条至少保留一个完整
来源 ID，任务概览保留完整生命周期 head ID，空间允许时显示状态原因及依据；
省略时提示使用任务 JSON 查看。同内容的其他来源仍在 JSON 中。记录标明类型和状态。
`--max-chars` 包含文本末尾换行，是字符上限，不是模型 token
上限；极小预算可能只能容纳提示。`--json` 保持完整已选记录，不应用文本预算。

网页记忆搜索有关键词时默认相关性排序，无关键词时保持时间倒序；可以手工切换。
CLI 与网页使用同一词法匹配和字段权重。多个空格分隔词仍为 AND，每个词的别名为 OR，
中文支持子串匹配，使用 Unicode NFKC 与大小写归一。不自动推断同义词。

可在本机 `config.json` 配置自己的项目或术语别名；公共模板不包含业务名称：

```json
{
  "search_aliases": {
    "atlas": ["starboard", "atlas-core"]
  }
}
```

别名只扩展检索，不改写事件、仓库名或项目归属。每个键和别名建议为单个检索词；
带空格的短语不会作为一个整体扩展。

```bash
pixi run memory search "atlas 缓冲区" --current
pixi run memory search "缓冲区" --documents
pixi run memory handoff --task delivery-check
pixi run memory handoff --task delivery-check --json
pixi run memory doctor --json
```

## 事件上下文与批量证据（v0.8）

日常可以直接要求 Agent“围绕这条记忆核对前因后果和后续修正”。底层先读索引，再取所需证据：

```bash
pixi run memory context <event_id> --before 3 --after 3 --max-chars 4000
pixi run memory context <event_id> --json
pixi run memory get <event_id> <另一个_event_id>
```

`context` 默认展示同 task 的前后各 3 条（可分别调整到 0–10），并优先纳入修正链，
总共最多 20 条。跨仓、跨机器的同任务记录保留原来源；无 task 时仅选同工作区、同 topic、
同项目集合的无 task 记录。按实际时间排序，被取代记录明确标识，任务状态无法裁决时给出提示。
索引只包含短摘要与来源，完整 `where`、`verified_by` 等证据用 `get` 读取。
文本按字符预算省略整条记录；JSON 不截断所选条目，按数量限制；省略的邻域和修正链会提示。
历史上下文不推荐执行旧待办，也不改变任务生命周期。

批量 `get` 每次最多 20 个唯一 ID，保持请求顺序并去重，输出 `items` 和 `missing`；
任意 ID 不存在时退出 1，已找到条目仍可读取。单个 ID 的 `{source,event}` 输出保持不变。

## 文档检索与任务交接

文档模式搜索允许目录的标题、路径和 Markdown 正文，返回路径及命中行摘录；
摘录使用 NFKC 显示文本，Unicode 展开不影响命中位置；文档预览保留原文。
空查询列出文档。此模式不加载 raw memory，损坏的事件文件不阻断文档检索。
`search` / `recent` 在无 `config.json` 的 raw 副本上仍可读取。独立检索只读取可选
`search_aliases`，不要求完整工作区配置；无关键词时不读取配置。配置或别名损坏、
不可读时，带关键词的 CLI 检索向 stderr 明确告警，再按原词匹配，JSON 输出仍可解析。
SVG 只搜索标题和路径，不解析其 XML 正文。文档没有 Agent、task、生命周期等事件
字段，因此 `--documents` 与事件筛选组合时拒绝执行；业务文档范围可在网页选择。
这些命令不创建索引、数据库或后台同步服务。

交接是基于当前有效事件的重复读取，不会消费、领取或关闭任务。显示任务状态来源、
进展、决策、失败与修复、验证、事实、未决问题和后续候选，每类默认最多 5 条，
`--limit` 可调整，省略项可在任务全部记忆中读取。只有 active/unknown 任务的 active
记录会推荐 next，暂停、关闭、冲突任务仍可查经验而不推荐旧待办。记录中的分支和
提交代表写入时快照，当前本机仓库状态另行只读获取；未注册或不可读状态明确标识。

`doctor` 只检查本机配置、记忆合法性/审计候选、共享 Skill 和入口链接、两份 Prompt
一致性、同步初始化/基线/冲突及 PATH 中的 mc。它不读取凭据、不调用远端、不安装
或修复文件，也不因为某 Agent 缺少近期记忆就断言采集失效。退出码为 0（通过）、
1（待核对）、2（错误）；未启用 S3 是正常本地模式。最后事件时间不代表机器在线，
shared 基线时间不能证明最近一次 raw 同步成功。
自检还提供本机核心版本、Git 提交与改动状态，记忆审计明细最多展示 50 项，剩余数量明确提示；
来源不足、长期未核对或并行修正属于核验候选，不自动判定事实失效。
公开示例 Skill 在 YAML frontmatter 的 `metadata.backstage_min_version` 声明最低核心版本，
例如 `backstage_min_version: "0.8.0"`（两个空格缩进）。未声明或核心版本不可读时兼容性为未知，
声明要求高于本机核心时提示更新。此检查只比较声明，不保证外部 Agent 已正确加载 Skill。
网页自检的基线和冲突取自 `--local-root`，结果标明本机状态目录；CLI 默认使用
`<root>/.local`。符号链接循环按异常链接报告。Git 缺失或不可执行时，交接仍可读取，
当前仓库状态标为不可用。
配置身份字段必须是非空字符串，每个项目项必须是对象并含非空字符串 `path`；
自检、交接和网页共用格式校验，网页启动失败时提示运行 `memory doctor`。
别名格式也在严格加载时检查。doctor 另核对本机工作区目录是否可用；工作台允许浏览
离线工作区已有的记忆和文档，仓库不可达时 Git 状态显示为不可用。

## 固定质量评测

```bash
pixi run memory-eval
pixi run memory-eval --baseline-ref HEAD
pixi run memory-eval --baseline-ref 298effa
```

30 个合成样例及预期来源在 `tests/fixtures/memory-quality.json`，不含真实工作区记忆。
评测在临时目录调用实际 CLI，覆盖中文子串、AND、别名、跨仓、旧结论排除、显式历史、
关闭任务经验、摘要覆盖与来源、交接和文档正文。输出每例结果、前五条命中率、证据召回率
及 CLI 耗时（含进程启动）。旧版本不支持的命令计失败，应按 kind 分开比较；固定样例
分数不能外推成实际全部任务准确率。工具不调用模型或远端，临时数据在退出时清理。
自定义 fixture 的文档只能写入临时工作区允许的文档目录，绝对路径和目录逃逸会报错。
检索用例的 `expected: []` 要求结果为空；没有正例时，命中率及召回率为 `null`。
基线从指定本地 Git 提交导出整个 `scripts/`，使用该版本的 CLI 和依赖模块。当前侧和
基线侧子进程都从各自源码目录解析模块，覆盖外部 `PYTHONPATH`，保证两侧一致。
`HEAD` 可用于自对照，`298effa` 是旧 v0.6.1
提交示例；引用需已存在于本地 Git。基线读取失败、畸形 fixture 或 CLI 超时属于运行
错误，向 stderr 报错并退出 2，不生成质量得分；用例未通过仍退出 1，全部通过退出 0。
`cases` 必须非空；评分字段、各类必需参数、配置和事件数据在执行前校验，CLI 参数
复用当前命令定义。允许合法的空检索词，`--help` 等文本也按查询内容处理。
`brief` 用例必须至少提供 `project` 或 `task`，预算必须是正整数。
评测强制使用临时 `workspace_root` 和空项目注册表，fixture 中这两项不会用于读取宿主仓库。
旧基线不支持的新命令仍计为质量失败，不当作 fixture 格式错误。
