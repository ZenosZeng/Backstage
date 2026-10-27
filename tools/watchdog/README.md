# Watchdog：通用框架与本机 adapter

公共代码负责轮询、整点状态、静默时段、告警节流、通知重试和服务进程管理。
业务 adapter 负责如何读取项目状态、识别异常及组织卡片内容；公共核心不包含实验清单、
模型身份、业务路径或训练/评测协议。

所有 watchdog 统一在一个常驻 Coder workspace 维护和运行。adapter 可从这里 SSH 读取
远端状态或访问日志；远端不部署 watchdog，也不需要 adapter 分发、跨机同步或安装机制。

## 目录与边界

```text
tools/watchdog/
  __main__.py       # 通用 CLI 与装配
  loader.py         # 本机 adapter 发现与加载
  runner.py         # 轮询、节流和状态通知
  notifier.py       # 飞书 webhook 卡片
  adapters/         # 本机私人业务代码及测试，Git 忽略
```

配置保存在 `.local/config/watchdog-<实例名>.json`，日志、PID 和游标位于
`.local/tools/watchdog-<实例名>/`。这些内容与 adapter 均不通过公共 Git 或 S3 分发。
共享记忆仍沿用工作区已有同步流程，与 adapter 的本机维护无关。

公共测试使用临时 fake adapter，不依赖个人项目。私人 adapter 的测试放在其本机目录内，
业务协议变化时更新对应测试；更新框架也应运行本机正在使用的 adapter 测试。

## 接入一个 adapter

在 `adapters/demo.py` 定义以下工厂函数。
模块导入本身不得启动采集、发送通知或修改状态。

```python
from types import SimpleNamespace


def create_monitor(config, config_path, directory, notifier):
    return SimpleNamespace(
        name=config.get("label", "Demo"),
        alert_interval=600,
        check_alerts=lambda: [],
        status=lambda: "Demo 状态正常",
    )
```

此示例仅用于验证接入链路。实际 adapter 用自己的只读采集替换 `status()` 与
`check_alerts()`；无需再创建一套轮询器或服务管理器。

| 参数或接口 | 约定 |
|---|---|
| `config` | 本机 JSON 对象，包含通用字段及 adapter 自己的业务字段 |
| `config_path` | 配置文件路径；业务相对路径以其父目录解析 |
| `directory` | 此实例的本机运行目录；常驻监控可在这里保存必要游标 |
| `notifier` | 框架提供的通知器；通常由 runner 统一投递 adapter 返回的告警 |
| `monitor.name` | 展示名称 |
| `monitor.alert_interval` | 持续告警两次成功通知之间的最小秒数 |
| `monitor.check_alerts()` | 返回当前告警序列；生命周期事件仅在真实状态变化时生成 |
| `monitor.status()` | 返回只读状态字符串，无活跃数据时可返回空字符串 |
| `monitor.next_poll_seconds()` | 可选，距下次到期检查的秒数；只缩短默认轮询等待 |
| `monitor.active()` | 可选，是否有活跃任务，用于整点例行状态 |
| `monitor.card_content(state)` | 可选，例行状态卡片正文；与 `active()` 配合使用 |

告警使用 `tools.watchdog.contracts.Alert`，包含 `key`、`severity`、`card_type`、`title`、`message`，
可用 `lifecycle=True` 标记开始、完成、
终止等边沿事件。持续条件使用稳定 key；边沿事件在状态变化后只生成一次，通知失败由
runner 保留重试，避免每次扫描重复追加。框架不会因告警而自动重启或重试业务任务。

`status()` 必须保持只读：`--once` 和工作台只调用状态查询，不调用告警扫描，
不发送卡片、不保存游标；构造 monitor 也不能通过副作用绕过此约定。
读取失败应抛出 `ValueError`，使单次检查失败并让工作台显示不可用，不应把错误字符串当作正常状态返回。

## 配置与运行

先保存 `.local/config/watchdog-demo.json`：

```json
{
  "adapter": "demo",
  "label": "Demo",
  "poll_seconds": 60,
  "quiet_hours": [0, 1, 2, 3, 4, 5, 6, 7, 13],
  "notify": false
}
```

```bash
pixi run watchdog --list
pixi run watchdog demo --once --no-notify
pixi run watchdog demo
pixi run watchdog demo --status
pixi run watchdog demo --restart
pixi run watchdog demo --stop
```

也可以使用 `pixi run python -m tools.watchdog <实例名>`。实例名与 adapter 名使用小写字母、
数字和下划线，且以字母开头；默认加载同名 adapter。多个实例可通过各自配置的 `adapter`
字段复用一个 adapter，同时分别保存 PID 与游标。

默认配置路径按实例名选择，也可用 `--config PATH` 显式指定。修改配置后须显式
`--restart`，更新源码不代表常驻进程已加载新代码。`--foreground` 用于前台排查，
同名实例仍受锁保护。

默认每 60 秒轮询，整点发送活跃任务状态；静默时段按进程本地时区判断，
默认 0–7 点和 13 点不发例行状态及监控启停卡，告警仍发送。持续告警由 adapter 的
`alert_interval` 节流；通知成功后才计入节流和去重，发送失败在后续轮询重试。
默认 webhook 相同内容在成功发送后 600 秒内去重。

## 飞书边界

Webhook 通知使用 `FEISHU_WEBHOOK_KEY` 或 `FEISHU_WEBHOOK`，签名可设置 `FEISHU_SECRET`；
建议用配置中的 `credentials_file` 指向本人所有、权限为 0600 的私有凭据文件。
正式启用通知时将 `notify` 改为 `true` 并显式重载。

通知器发送飞书卡片，并检查 HTTP 及飞书业务返回码；这里的 interactive 消息类型
表示卡片格式，目前没有 watchdog 按钮回调或指令接收功能。独立的
[飞书遥控服务](../README.md#飞书里的用法) 使用 App 凭据、用户白名单与自己的进程，
不应将其 Agent 会话和业务控制逻辑混入轮询框架。

只读工作台动态列出本机 adapter 和配置。页面刷新通过 `--once --no-notify` 查询状态，
不启动、停止或重载 watchdog，也不启停业务任务。

## 无人值守交互

通用交互队列使用本机 `.local/interaction.json`。Agent 在当前会话提问时同时登记问题；
问题满 120 秒仍没有回答，提醒服务发送飞书卡片。之后每 120 秒检查并再次提醒，直到
问题被回答或取消。多 Agent 共用此文档，每个问题有独立 ID、所属 Agent 和任务。

```bash
pixi run interaction ask --agent codex --task demo --question '选择哪个方案？' --option A --option B
pixi run interaction list
pixi run interaction show q_012345abcdef
pixi run interaction answer q_012345abcdef '选择 A'
pixi run interaction cancel q_012345abcdef
pixi run interaction watch --once --no-notify
pixi run interaction watch
pixi run interaction watch --status
```

提醒服务复用同一套进程管理和 webhook 通知器，配置位于 `.local/config/interaction.json`，
可参考 [通用配置模板](../config/interaction.example.json)。无需给五个业务 adapter 各建一套问答机制。
空闲时每 120 秒检查文档；有问题临近到期时按最近截止时间提前唤醒，避免首条提醒延迟到接近四分钟。
发送失败等待下个两分钟周期重试，重启服务会保留问题及成功通知时间。
每轮提醒包含轮次与时间，避免相同卡片的默认 600 秒去重吞掉两分钟提醒。

飞书 App 服务加载新代码后，可使用 `/questions` 查询、`/answer <问题ID> <回答>` 作答，
或 `/cancel-question <问题ID>` 取消。命令沿用 App 的人类白名单和消息校验；即使
Agent 正忙，也可以写回答案。Webhook 卡片负责提醒，App 消息入口负责接收回答。
若只配置 webhook，仍可在当前会话回答，或手工编辑本机文档。

文档格式如下，实际问题应由 `ask` 生成，保留其 ID 和时间字段：

```json
{
  "schema": 1,
  "questions": [
    {
      "id": "q_012345abcdef",
      "question": "选择哪个方案？",
      "agent": "codex",
      "context": "决策所需的最少背景",
      "task": "demo",
      "options": ["A", "B"],
      "created_at": 1793100000,
      "status": "pending",
      "last_notified": null,
      "notification_count": 0,
      "answer": ""
    }
  ]
}
```

手工填写非空 `answer` 即视为已回答，不要求同时修改 `status`；查询时不会回写文档。
使用 CLI 或飞书作答时会记录 `status=answered`、`actor` 和 `updated_at`；取消则为
`status=cancelled`。Agent 用 `show <问题ID>` 读取自己的条目并按原问题范围处理答案，当前会话收到回答也应
及时回写；问题不再需要时取消，停止残留提醒。超时与通知投递成功都不代表用户同意。

交互文档、问题和回答只保留在本机，不进入公共 Git 或 S3。多 Agent 的 API 写入使用短事务
文件锁及原子替换；人工编辑时保持合法 JSON，避免与其他写入同时保存。
