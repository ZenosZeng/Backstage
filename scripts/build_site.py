"""Build the public website from an explicit list of public, static inputs.

No workspace config, memory, logs, credentials or runtime services are read.
The local workbench's renderer is reused with a separate synthetic-data client.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
SITE_FILES = (
    "index.html", "styles.css", "main.js", "assets/workspace-preview.png",
    "demo/api.js", "demo/data.json", "demo/demo.css",
)
WORKBENCH_FILES = ("app.js", "app.css", "theme.js", "cluster.js", "cluster.css")


def public_file(path: Path) -> Path:
    relative = path.relative_to(ROOT)
    if any((ROOT / Path(*relative.parts[:index])).is_symlink()
           for index in range(1, len(relative.parts) + 1)):
        raise ValueError("Public site inputs must not be symlinks: " + str(relative))
    if not path.is_file():
        raise ValueError("Missing public site input: " + str(relative))
    return path


def demo_index() -> str:
    html = public_file(ROOT / "tools/web/static/index.html").read_text(encoding="utf-8")
    html = html.replace("<title>Backstage · 工作台</title>", "<title>Backstage · 合成数据 Demo</title>")
    html = html.replace("</head>", '  <link rel="stylesheet" href="demo.css">\n</head>')
    html = html.replace('<div class="shell">', '''<div class="shell">
    <div class="demo-banner" role="note"><strong>DEMO DATA</strong>
      <span>合成记录与模拟状态 · 不连接真实工作区或服务</span>
      <a href="../">← Backstage 官网</a></div>''', 1)
    html = html.replace("本机只读工作台", "合成数据 · 静态演示").replace("<span>LOCAL</span>", "<span>DEMO</span>")
    html = html.replace("数据来自现有工作区 · 手动刷新", "固定示例快照 · 所有状态与记录均为虚构")
    return html


def build(output: Path) -> Path:
    output = output.expanduser().absolute()
    if output.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    copies = [(public_file(ROOT / "site" / name), name) for name in SITE_FILES]
    copies.extend((public_file(ROOT / "tools/web/static" / name), "demo/" + name)
                  for name in WORKBENCH_FILES)
    expected = {destination for _, destination in copies} | {"demo/index.html", ".nojekyll"}
    # A reused output directory may not silently publish unrelated local files.
    if output.exists():
        if not output.is_dir():
            raise ValueError("Output must be a directory")
        existing = list(output.rglob("*"))
        if any(path.is_symlink() for path in existing):
            raise ValueError("Output must not contain symlinks")
        files = {path.relative_to(output).as_posix() for path in existing if path.is_file()}
        if files and (".nojekyll" not in files or not files <= expected):
            raise ValueError("Use an empty output directory, or the existing site build directory")
    output.mkdir(parents=True, exist_ok=True)
    for source, destination in copies:
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (output / "demo/index.html").write_text(demo_index(), encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the public Backstage website and synthetic demo")
    parser.add_argument("--output", type=Path, default=Path(".local/site-preview"))
    args = parser.parse_args()
    try:
        result = build(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print("Built public website: " + str(result))


if __name__ == "__main__":
    main()
