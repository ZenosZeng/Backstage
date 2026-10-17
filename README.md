<p align="center">
  <img src="docs/assets/readme-cover.png" alt="Backstage：多 Agent、多机器、多仓库，共享上下文" width="100%">
</p>

# Backstage

为 Codex、Claude Code 和 Kimi 提供跨仓库记忆、只读工作台、任务监控与飞书遥控。

- **共享记忆**：记录事实、决策与来源，用稳定 task ID 串联跨仓库工作。
- **多机协作**：按需通过私有 S3 同步知识、Skill 和记忆，也可只在本地使用。
- **只读工作台**：浏览项目、记忆、时间线、文档与本机运行状态。
- **监控与遥控**：飞书任务交互，Pi / susser-tod 训练和 B1K 评测监控；其他业务可接适配器。

## 当前版本

最新版本：**v0.6.1** · 当前功能版本：**v0.6.0**。

**v0.6.0 主要功能**

- 只读 Web 工作台：项目导航、跨仓记忆与文档预览、本机服务和监控快照。
- 深浅主题、手机布局与共享短时快照缓存。

`v0.6.1` 提供按需应用的 [Windows 兼容补丁](patches/README.md)，不改变 Linux 生产实现。完整变化见 [版本记录](docs/changelog.md)，补丁不替换功能摘要。

## 快速开始

完整工具环境支持 Linux x86-64，需要 Git 和 [Pixi](https://pixi.sh/)。

```bash
git clone https://github.com/ZenosZeng/backstage.git ~/code/.agents
cd ~/code/.agents
pixi install --locked
```

首次使用按 [最小工作区示例](examples/README.md) 创建配置并验证记忆读写；仅运行记忆脚本需要 Python 3.10+，无需 S3、飞书或 Agent CLI。安装不会自动启动服务。

## 日常使用

```bash
pixi run memory brief --project repo-name
pixi run memory search "接口 契约" --current --limit 10
pixi run tools-status
pixi run web
```

工作台默认地址为 `http://127.0.0.1:8787`，只读、不提供网页遥控；远程工作区需转发端口。共享记忆的读写由 Agent 主动完成，重要结论仍需核对源码、Git 和测试。

监控与飞书遥控须先从 `tools/config/` 复制模板到 `.local/config/` 并填写配置，再按需启动：

| 工具 | 命令 |
| --- | --- |
| Pi 训练监控 | `pixi run watchdog-train` |
| B1K 评测监控 | `pixi run watchdog-b1k` |
| Claude 飞书遥控 | `pixi run claude-feishu` |

配置、启停及飞书指令见 [工具参考](tools/README.md)；S3 初始化与同步见 [记忆参考](docs/memory.md)。这些能力独立配置，不要求全部启用。

## 常用配置

记忆配置使用 `config.json`，服务配置使用 `.local/config/`；工作区知识与记忆位于 `.share/`。这些目录和本机配置不进入 Git，存储与飞书凭据保存在本机私有文件中。

遥控须设置用户白名单；工具权限不是操作系统沙箱。完整约束与配置说明见 [工具参考](tools/README.md) 和 [记忆参考](docs/memory.md)。

## 开发

```bash
pixi install -e dev --locked
pixi run -e dev test
pixi run -e dev lint
```

测试使用临时目录、合成日志和 mock，不需要真实 S3 或 GPU。不要提交私人记忆、完整聊天、配置或凭据。

`main` 为生产分支，改动通过独立分支和 PR 合入，提交采用中文 Conventional Commits；版本与协作规则见 [开发规范](docs/development.md)。

## 文档

[记忆与同步](docs/memory.md) · [工具参考](tools/README.md) · [工作台](tools/web/README.md) · [版本记录](docs/changelog.md)

## 许可

[MIT License](LICENSE)。依赖项遵循各自的许可证。
