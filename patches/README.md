# Windows 可运行补丁

让本仓库的记忆与同步工具在 Windows 上运行的补丁。**不并入 `main`**：`main` 面向
Linux 生产环境，本补丁只在 Windows 机器上按需应用，避免影响正在运行的服务。

- 补丁文件：`windows-portability.patch`，对应 **v0.6.1** 级别的修复内容（小版本，只修 bug）
- 当前应用上下文：**v0.7.0**；Windows 修复内容沿用 v0.6.1，仍只保留在补丁文件中。

## 使用方法

在 Windows 机器的仓库根目录：

```bash
git apply patches/windows-portability.patch
```

回退：

```bash
git apply --reverse patches/windows-portability.patch
```

补丁以**未提交的工作区改动**形式存在，不要 commit。注意 `git pull` / `git merge`
在upstream 触及这 5 个文件时会因工作区不干净而失败：先反向回退，拉取后重新应用。

## 补丁内容

**1. 文件锁**（`scripts/memory.py`、`scripts/sync.py`）
Windows 没有 `fcntl`，缺少时改用 `msvcrt.locking` 实现同语义的 `flock`；非 Windows
仍走 `fcntl`。

**2. 链接与 reparse point**（`scripts/sync.py`）
`Path.is_symlink()` 不认 Windows junction，因此识别 reparse point、剥离 `readlink`
返回的 `\\?\` / `\\?\UNC\` 扩展前缀、删除链接失败时回退 `rmdir`。没有符号链接权限
时按目标类型降级：目录用 `mklink /J`（无需管理员），文件用复制；已存在的同名真实
配置仍然拒绝覆盖。

**3. 编码**（`scripts/memory.py`、`scripts/sync.py`、`tests/`）
子进程统一按 UTF-8 解码，子进程 Python 固定 UTF-8 模式，示例引导文档显式按 UTF-8
读取，解释器路径按 shell 规则引用。中文 Windows 默认 GBK，缺 `encoding` 时
`subprocess` 的读取线程会在内部抛 `UnicodeDecodeError` 并被吞掉——表现为 `stdout`
为 `None` 和一个毫不相干的 `TypeError`，排查成本很高。

**4. `os.replace` 重试**（`scripts/sync.py`）
Windows 上刚写完的文件或目录可能被杀毒或索引进程瞬时持有句柄，此时 `MoveFileEx`
返回 WinError 5（拒绝访问）而目标其实已不存在。实测 40 项同步回归中约 1/15 次命中，
真实同步会停在“部分内容已替换”的状态。新增 `replace_with_retry`（重试 5 次，退避
`0.05 × attempt` 秒），覆盖 `atomic_write_json`、`replace_directory`、
`update_shared_base`、`save_shared_conflict` 四处调用。

**5. 测试平台断言**（`tests/test_sync.py`、`tests/test_onboarding.py`）
符号链接与 junction/副本按平台分别断言，Windows 分支比对内容而非链接目标；Linux
分支保留原有断言不变。

## 验证情况

- v0.7.0：更新 Git 状态读取处的补丁上下文，Linux 上验证应用/反向回退及源码字节还原。
  新版尚未在 Windows 实机运行；下述 Windows 回归数字只对应 v0.6.1，不代表新增功能已验证。

- 应用后 `tests/` 全量 **86 项在 Windows 上连续 3 轮通过**；修复 `os.replace` 之前，
  仅 `test_sync` 一项 6 轮里就有 5 轮失败，且每轮失败的用例都不同。
- **v0.6.1 原记录：Linux 未实测**。对 Linux 无行为影响的结论来自代码审查：所有 Windows 分支均由
  `os.name` 或 `ImportError` 守卫，测试的 `else` 分支保留原有断言。若要上游，建议先
  在 Linux CI 上跑一遍。
- `tools/tests/` **未覆盖**：本机缺 `lark_oapi`、`pytest` 依赖，且部分用例依赖 Linux
  的 `/proc` 与符号链接权限；`tools/` 下的服务本身部署在 Linux，不在本补丁范围。
