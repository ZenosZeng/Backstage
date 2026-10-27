# 工作区工具

这些旁路工具独立于业务仓库的 Python 环境。监控框架负责轮询、通知、去重和进程管理，
项目状态解析与告警规则由本机 adapter 提供。监控只观察，不启停训练、仿真、推理或评分。

所有 watchdog 在一个常驻 Coder workspace 维护和运行；adapter 可以通过 SSH 读取远端状态，
也可以读取可访问的日志。远端无需部署 watchdog，也不维护 adapter 分发或同步机制。
私有 adapter 保存在 Git 忽略的 `tools/watchdog/adapters/`，详见 [Watchdog 接入说明](watchdog/README.md)。

Claude 飞书遥控是独立服务，使用 App 凭据处理用户指令；watchdog 使用 webhook 发送状态卡片，
两者不共用进程或凭据。集群资源由独立的 `cluster-monitor` 经 SSH 采集并保存本机快照，
见 [集群监控说明](cluster/README.md)。`pixi run web` 启动只读工作台，页面不提供遥控入口，
见 [工作台说明](web/README.md)。

## 安装与启动

```bash
cd ~/code/.agents
pixi install --locked
# 先创建本机 demo adapter 和配置，再按需启动
pixi run watchdog demo --once --no-notify
pixi run watchdog demo
pixi run tools-status
```

服务默认后台运行，同名服务只允许一个实例；配置变化后用 `--restart` 校验并重载。
无需 sudo、tmux 或业务环境。工具服务统一支持 `--status`、`--stop`、`--restart`、
`--once`、`--foreground` 和 `--config PATH`。

```bash
pixi run watchdog demo --status
pixi run watchdog demo --stop
pixi run watchdog demo --foreground --no-notify
```

`--once` 是无通知、无常驻进程的配置与只读状态检查，不保存监控游标。
遥控的 `--once` 不连接飞书或运行 Agent；集群采集的 `--once` 只读取已有快照，
不连接 SSH、不探活端口。`--no-notify` 仅禁用 watchdog 消息，不控制独立遥控服务。
PID 身份通过启动时间、命令和工作目录核验，不用宽泛 `pkill` 停止其他服务。

## 本机配置

公共模板在 `tools/config/`，真实配置放 `.local/config/<服务名>.json`，不进 Git/S3。
相对路径以配置文件所在目录为基准；业务字段由本机 adapter 解释。

| 服务 | 配置入口 |
|---|---|
| interaction | `.local/config/interaction.json`，两分钟提醒及本机问答见 [无人值守交互](watchdog/README.md#无人值守交互) |
| watchdog | `.local/config/watchdog-<实例名>.json`，通用字段及 adapter 接口见 [接入说明](watchdog/README.md) |
| cluster-monitor | 必填 `hosts`；采集周期和端口配置见 [字段说明](cluster/README.md#本机配置) |
| claude-feishu | 凭据文件、工作目录、CLI、超时和权限档，见公共模板及下文 |

## 飞书凭据

监控从环境读 `FEISHU_WEBHOOK_KEY`（也支持 `FEISHU_WEBHOOK` 完整 URL），签名使用可选
`FEISHU_SECRET`。兼容从 `.zshrc` / `.bashrc` 读取对应赋值，但不执行 shell。
推荐各服务通过 `credentials_file` 指定私有环境文件，文件须归本人所有、权限 0600。
只支持普通 `KEY=value` / `export KEY=value`，不执行命令替换、变量展开或 source。

遥控使用 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_ALLOW_OPEN_IDS`，群聊还需
`FEISHU_BOT_OPEN_ID`。默认凭据路径 `~/.config/claude-feishu/env`；空白名单拒绝启动。
默认使用已安装的 `claude`，也可通过 `claude_cli` 指定。第二个 harness DSH 是可选依赖，
`dsh_cli` > `DSH_CLI` > PATH；未安装时 `/dsh` 拒绝切换，Claude 继续可用。
工具不自动安装 CLI 或更改登录配置。

```bash
pixi run claude-feishu --once
pixi run claude-feishu
pixi run claude-feishu --restart
pixi run claude-feishu --stop
```

### 飞书里的用法

直接发指令即可执行；执行中默认每 5 秒原地刷新同一张进度卡片，显示思考摘要、当前动作、
最新输出（工具回显/报错）与最近步骤，收尾卡片带耗时、轮次、输出 token 与成本。

刷新间隔由 `progress_interval_seconds` 控制（非负整数秒，默认 5），0 关闭中间刷新，
仍发送开始/结束卡片。优先级为 JSON 配置 > `CLAUDE_FEISHU_PROGRESS_INTERVAL` >
旧变量 `CLAUDE_FEISHU_STREAM_INTERVAL` > 默认值。修改后需重启服务。
旧模板的 600 曾未控制流式刷新；现在会真正每 600 秒刷新，需要实时进度时改为 5。

| 命令 | 作用 |
|---|---|
| `/help` | 列出全部命令与打断词 |
| `/claude` / `/dsh` | 为当前 chat 粘性切换 harness，分别保存上下文与统计 |
| `/new [claude\|dsh\|all]` | 清空指定 harness 上下文，默认当前，并清空本聊天待执行队列；保留目录与累计统计 |
| `/status` | 当前 harness、两套会话及统计、CLI 可用性、目录、上下文、队列 |
| `/restart` | 校验新代码/配置，由宿主 bridge 自行重载；保持会话，就绪后回复原消息 |
| `/cd <目录>` | 切换该会话的工作目录 |
| `/cost` | 当前 harness 累计轮次、输出 token、时长；DSH 不报告成本 |
| `/questions` | 列出本机待决策问题 |
| `/answer <id> <回答>` | 写回交互文档，Agent 忙碌时仍可回答 |
| `/cancel-question <id>` | 取消问题并停止提醒 |
| `/queue <指令>` | 排队执行（当前任务结束后自动开始，上限 5 条） |

执行中发「停」「停止」「打断」「取消」「stop」「cancel」打断当前任务；发其他内容会
提示忙并建议用 `/queue`。命令与统计按 chat 隔离。DSH 的权限、限制与部署注意见
[DSH harness 参考](claude_feishu/README.md)。

权限档由本机配置的 `permission_profile` 选择，新机器默认 `read-only`。
经用户明确授权，可设为 `workspace`，保留原遥控的 Read/Glob/Grep/Bash/Edit/Write/WebFetch
以及原危险命令 deny 列表。不会添加跳过权限检查的参数，也不接受任意 settings 路径。
这只是 Claude 工具权限规则，不是操作系统沙箱；Bash 具有当前用户权限，应仅对白名单可信用户开放。
不要在消息或上下文中暴露凭据。配置改变后可在飞书发 `/restart`，当前任务或队列未完成时拒绝重启。
原生命令通过现有用户白名单，直接由 bridge 处理，不进入 Claude/DSH 的模型执行。
校验失败保留旧服务并回复失败；成功回执等待新实例的 websocket 就绪，保留两套原生会话。
沙箱内的 agent 使用 `scripts/restart-claude-feishu.sh --detach --delay 30`，只请求服务自行重载，
不会从 agent 的 Landlock 上下文启动新服务。`setsid` 本身不能解除沙箱。
旧版 daemon 首次升级需从宿主机停起一次；旧版或未运行服务收到新重启请求时会拒绝，不自动 stop/start。
保留消息/event/fingerprint 防重放、过期消息拒绝、聊天会话映射、串行执行、超时进程组清理。

## 运行状态与迁移

每个服务独立写 `.local/tools/<服务名>/service.log`、`pid.json`、锁和游标。
遥控的 `seen.json` / `sessions.json` 只保留去重 ID 和原生会话映射，不共享聊天原文。
旧遥控服务的会话映射/防重放文件只能在旧服务停止后迁移，不同时运行两个消费者。
2026-09-07 评测机已完成切换：旧独立仓库归档至
`.local/migrations/claude-feishu-20260907/`（含 `.git`），不再作为运行依赖。
原状态文件保留作备份，当前服务只写 `.local/tools/claude-feishu/`。
`service.log` 的 `Feishu websocket connected` 表示 SDK 已完成连接，不打印带签名的连接 URL。

核心代码、模板与 Pixi lock 走 Git；凭据、日志、PID、游标仅本机；Memory/Skill/项目资料沿用 S3。
集群监控的私有配置、快照、SSH 配置与密钥均仅本机，不通过 Git/S3 发布。
旧独立 `cluster-monitor` 的切换须先停止旧采集，再启动 Backstage 采集，避免重复采样；
原仓库保留历史、不删除，后续代码维护以 Backstage `tools/cluster/` 为入口，见 [迁移说明](cluster/README.md#从独立-cluster-monitor-迁移)。
业务监控由本机 adapter 接入；公共仓库不维护实验清单、业务路径或项目日志协议。

## 验证

```bash
pixi install -e dev --locked
pixi run -e dev test
pixi run -e dev lint
```

公共测试使用 mock 飞书、临时 fake adapter 和隔离服务，不依赖被 Git 忽略的项目代码。
业务 adapter 的测试与其私有代码一起维护，不访问 GPU，不启动训练或评测。
`lint` 只启用 `E9,F63,F7,F82`（语法错误、未定义名一类），不代表完整风格检查；要更严的规则需显式 `--select`。
