# 集群监控

以本机 SSH 别名连接已配置的机器，采集 CPU、内存、GPU 等资源状态，保存到
`.local/tools/cluster-monitor/snapshot.json`。Backstage 工作台在 **8787** 的“监控 → 集群资源”
中只读展示快照；网页读取不会触发 SSH、端口扫描或启动采集。

工具复用 Backstage 的服务管理，不需要独立网页、数据库或业务仓库环境。未配置或未启动时
不会自动采集，现有训练/评测 watchdog 与飞书遥控继续独立工作。

## 本机配置

将 [`cluster-monitor.example.json`](../config/cluster-monitor.example.json) 作为起点，
在本机准备 `.local/config/cluster-monitor.json`。示例中的 `gpu-node-a` / `gpu-node-b`
为占位 SSH 别名，需要换成本机已配置的别名：

```json
{
  "hosts": [
    {"alias": "gpu-node-a", "label": "GPU 节点 A", "group": "GPU"},
    {"alias": "gpu-node-b", "label": "GPU 节点 B", "group": "GPU"}
  ],
  "interval_seconds": 5,
  "health_interval_seconds": 30
}
```

| 字段 | 含义 |
| --- | --- |
| `hosts` | 必填的非空列表，每项包含唯一的 `alias`，可选 `label` 与 `group`。别名用于 SSH，只含字母、数字、点、下划线和连字符，以字母或数字开头；名称默认别名，分组默认“其他”。 |
| `interval_seconds` | 机器资源快照更新间隔，默认 5 秒，最小 2 秒。 |
| `health_interval_seconds` | 可选端口/健康检查间隔，默认 30 秒，最小 10 秒。 |
| `ports` | 可选的端口与健康检查配置；省略时不检查这些入口。 |

GPU 连续采样时每 0.5 秒读取，汇总最近 60 秒窗口；这与网页读取或全机资源快照的刷新间隔不同。
连续采样不可用时回退为单次查询，页面标明瞬时值，不将其计入窗口峰值活跃卡统计。
SSH 由系统客户端读取当前用户的 `~/.ssh/config` 和密钥，目标需已支持无交互连接；
工具不会复制 SSH 配置、分发密钥或替你登录。
远端需要 Linux `/proc` 和 `python3`；GPU 指标使用可选的 `nvidia-smi`，缺少时仍采集
CPU、内存和网络并标明 GPU 不可用。启用端口检查时，远端还需 `ss`。

本机配置、快照、`~/.ssh/config` 和密钥均不进入 Git 或 S3。公共模板只包含通用占位别名，
不保存真实机器地址、用户名和凭据。

### 可选端口与健康检查

只有需要查看推理入口或 bridge 状态时才添加 `ports`，例如：

```json
{
  "ports": {
    "host": "gpu-node-a",
    "start": 8000,
    "end": 8007,
    "health_path": "/healthz",
    "bridge_log": "auto"
  }
}
```

这是添加到完整配置中的片段，`host` 必须与 `hosts` 中的某个 `alias` 一致。
`start` / `end` 为 1–65535 范围内的闭区间，`end >= start`，一次最多 256 个端口。
`health_path` 默认 `/healthz`；`bridge_log` 可设为 `auto` 或远端绝对路径，省略时不读取 bridge 日志。
如探活地址与 SSH 别名不同，可填写 `public_host`，仅写主机名或地址，不包含协议和端口。
检查结果是采样时的可达性和健康信息，不能据此证明所有推理服务或请求都正常。
探活仅接受配置入口直接返回的 `200 OK`（正文为 `OK`）；不跟随重定向，协议异常按该
端口失败记录，后续探活继续。收发速率按相邻两次成功 TCP 采样的实际时间差计算，
采样间隔超过 30 秒时同样适用；短连接仍可能漏采。

## 启停与检查

在 Backstage 仓库根目录使用：

```bash
pixi run cluster-monitor --once
pixi run cluster-monitor
pixi run cluster-monitor --status
pixi run cluster-monitor --restart
pixi run cluster-monitor --stop
```

`--once` 仅校验本机配置并读取已有快照，不执行 SSH 或探活，也不产生新样本。
不带动作参数才按配置启动后台采集；`--foreground` 在前台运行。
自定义配置使用 `--config PATH`，服务状态也可通过 `pixi run tools-status` 查看。

配置与状态沿用 Backstage 的私有目录：

| 路径 | 用途 |
| --- | --- |
| `.local/config/cluster-monitor.json` | 本机机器清单、采样间隔与可选检查入口 |
| `.local/tools/cluster-monitor/snapshot.json` | 供工作台读取的最近采样结果 |
| `.local/tools/cluster-monitor/service.log` | 本机服务日志 |
| `.local/tools/cluster-monitor/pid.json` | 服务身份、就绪及启动代码版本记录 |

工作台分别显示采样时间和读取时间。节点超过 `interval_seconds × 3 + 5` 秒没有新样本时，
其旧样本标记为陈旧，不计入当前 CPU/GPU 汇总；采集失败保留最后样本并明确错误状态。
缺少快照或快照陈旧时，应结合提示核对，旧数值不能视为实时数据。端口/健康检查的成败
与机器是否有新样本分别显示，不能互相替代。采集命令只读，不启停训练、推理或评测。

## 从独立 cluster-monitor 迁移

更新源码不会自动停止旧服务、迁移私有配置或切换生产入口。
已有独立 8790 服务可继续运行，明确部署新采集器时再执行以下切换：

1. 在运行 Backstage 采集器的本机准备新的私有配置，核对 SSH 别名；按字段手工整理，
   不把旧配置或 SSH 凭据自动复制到公共仓库或共享目录。
2. 用 `pixi run cluster-monitor --once` 检查本机配置；该步骤不产生新采样，不代表远端连接已验收。
3. 按旧服务的实际运行方式停止旧采集进程，确认它已退出，再启动 `pixi run cluster-monitor`，
   避免两套采集器重复采样。
4. 用 `--status` 核对新采集器，在 8787“监控 → 集群资源”核对快照时间、节点与失败状态。
   旧 8790 网页不再作为新采集器的入口。

迁移后由 Backstage `tools/cluster/` 维护采集实现，旧独立仓库保留历史，不删除。
本流程不要求改变训练/评测、飞书 bridge 或远端任务的运行方式。
