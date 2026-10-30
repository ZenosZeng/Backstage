# 官网与静态演示

[返回首页](../README.md) · [本机工作台](../tools/web/README.md)

官网用于介绍 Backstage；静态 Demo 用于在安装前体验项目时间线、任务和文档交互。
两者都可以由普通静态文件服务器提供，不需要 Python API、Agent、S3、SSH 或飞书凭据。

## 访问与本地预览

预期 GitHub Pages 入口：

- 官网：<https://zenoszeng.github.io/Backstage/>
- Demo：<https://zenoszeng.github.io/Backstage/demo/>

这些是站点的部署目标，新增源码或合并 PR 不代表站点已发布。未发布时可在仓库根目录预览：

```bash
python3 scripts/build_site.py --output .local/site-preview
python3 -m http.server 8791 --bind 127.0.0.1 --directory .local/site-preview
```

打开 `http://127.0.0.1:8791/` 或 `http://127.0.0.1:8791/demo/`。预览服务在前台运行，按 Ctrl+C 停止；
远程工作区需转发 `8791` 端口。构建只需要 Python 标准库，不要求先执行 `sync init` 或创建本机配置。

## Demo 展示什么

Demo 使用通用的合成项目、任务与文档，展示任务决策、验证结果、来源依据和监控快照。
支持项目筛选、任务详情、来源抽屉、文档互链、交接与审查材料复制，以及亮暗主题切换。
所有画面明确标注 `DEMO DATA`；示例中的状态、时间、机器名称和指标不代表真实运行情况。
项目时间条仍表示记录覆盖跨度，不表示计划工期或完成百分比。

Demo 复用 `tools/web/static/` 中的工作台界面，以静态合成响应提供交互。
它不读取当前机器的工作区，也不提供网页遥控、任务派发、记忆写入、SSH 或飞书操作。
真实工作台仍由 `pixi run web` 在本机 `127.0.0.1:8787` 提供，使用方法见[工作台说明](../tools/web/README.md)。

## 构建与发布边界

- `site/` 保存公开官网、演示数据及静态演示入口；`tools/web/static/` 提供复用的工作台资源。
- 构建器只读取这两处公开目录，不读取 `.share/`、`.local/`、`config.json`、私有 adapter、SSH 配置、凭据或真实日志。
- `.local/site-preview` 是本机预览产物，不加入 Git；发布时只上传构建出的静态站点目录。
- 公开示例采用通用合成概念。引用开源项目时仅链接其公开资料，不声称相关项目使用、集成或背书 Backstage。

正式发布需仓库维护者在 Settings → Pages 将 Source 设为 GitHub Actions，
然后在 Actions → website 中选择 `main`，手动运行 Run workflow。
PR 和 push 只检查静态构建；只有在 `main` 手动运行时才上传站点并部署。
流程依据见 [GitHub Pages 官方说明](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
本次实现不自动修改仓库设置、创建发布或推送代码，也不部署和重启本机工具服务。
脱敏覆盖当前公开文件，保留作者与版权信息，不重写 Git 历史或修改私有工作区数据。
