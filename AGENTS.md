# Backstage 开发约定

## 分支与版本

- `main` 是Linux生产分支。编辑前检查工作区和分支；若在main，先创建 `feat/`、`fix/`、`docs/`、`chore/` 或 `release/` 分支，保留已有改动。
- 禁止直接向main提交、推送或强推。所有功能、修复、文档、依赖和版本更新都通过PR合入；遵守 [开发与版本管理](docs/development.md)。
- 应用版本以 `pixi.toml` 为准。README分别显示“最新版本”和“当前功能版本”，patch只更新最新版本号，不覆盖当前major/minor的feature摘要。
- 完整变更和升级影响写入 `docs/changelog.md`，按新到旧排列；文档或流程调整可先放“未发布”，不强制升版。
- 提交与PR标题使用中文Conventional Commits。保留历史提交，不因流程调整重写已有main历史。
- 推送分支、创建/合并PR、打标签和创建Release需明确授权；标签只能指向已合入并验收的main提交。

## 边界与检查

- 公共源码、模板和依赖锁走Git；`config.json`、`.share/`、`.local/`、凭据、真实日志和完整聊天不得进入Git。
- 记忆和同步操作须遵守本机工作区的许可约束；仓库支持S3不代表本机已授权同步，不得擅自上传本机记忆。
- 保留Linux公共实现；Windows兼容改动按 `patches/README.md` 管理，不能把本机已应用补丁后的源码误合入生产分支。
- 行为修改补回归；Linux环境运行现有测试和lint，文档或版本约定检查不依赖真实S3、飞书或GPU。
- 不自动启动生产服务、执行真实同步或重启遥控；验证范围与未验证项要如实说明。
