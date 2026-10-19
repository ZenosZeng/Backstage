"""Local-only HTTP entry point. Deliberately exposes no write/control routes."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from subprocess import TimeoutExpired
from urllib.parse import parse_qs, urlsplit

from tools.config import ROOT, local_root
from tools.web.data import Workspace


STATIC = Path(__file__).with_name("static")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'none'")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # The default queue of five adds TCP retries to short concurrent reads.
    request_queue_size = 64

    def __init__(self, address, workspace: Workspace):
        self.workspace = workspace
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # URLs may contain private search terms or document paths.

    def respond(self, status, data, content_type="application/json; charset=utf-8", *, svg=False):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox" if svg else CSP)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Navigation can close a read request before its response.

    def do_GET(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.respond(403, {"error": "请通过本机地址或 SSH 端口转发访问"})
            return
        url = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(url.query).items()}
        workspace = self.server.workspace
        try:
            if url.path == "/api/overview":
                data = workspace.overview()
            elif url.path == "/api/events":
                data = workspace.events(query)
            elif url.path == "/api/event":
                data = workspace.event(query["id"])
            elif url.path == "/api/documents":
                data = {"items": workspace.documents(query.get("business"), query.get("q", ""), query.get("sort", "relevance"))}
            elif url.path == "/api/document":
                data = workspace.document(query["path"])
            elif url.path == "/api/handoff":
                data = workspace.handoff(query["task"], query.get("limit", 5))
            elif url.path == "/api/doctor":
                data = workspace.doctor()
            elif url.path == "/api/services":
                data = workspace.services()
            elif url.path == "/api/monitor":
                data = workspace.monitor(query["kind"])
            elif url.path == "/api/asset":
                path = workspace.document_path(query["path"])
                if path.suffix.lower() == ".md":
                    raise PermissionError("请使用文档预览入口")
                self.respond(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream", svg=path.suffix.lower() == ".svg")
                return
            elif url.path in {"/", "/index.html", "/app.js", "/app.css", "/theme.js"}:
                path = STATIC / (url.path.lstrip("/") or "index.html")
                self.respond(200, path.read_bytes(), (mimetypes.guess_type(path.name)[0] or "text/plain") + "; charset=utf-8")
                return
            else:
                raise FileNotFoundError("页面不存在")
            self.respond(200, data)
        except FileNotFoundError as error:
            self.respond(404, {"error": str(error)})
        except PermissionError as error:
            self.respond(403, {"error": str(error)})
        except (ValueError, KeyError, argparse.ArgumentTypeError):
            self.respond(400, {"error": "请求参数或工作区数据无效"})
        except TimeoutExpired:
            self.respond(504, {"error": "监控读取超时，请稍后刷新"})
        except OSError:
            self.respond(500, {"error": "读取失败，请检查工作区文件权限"})

    def do_POST(self):
        self.respond(405, {"error": "工作台仅支持只读访问"})


def main():
    parser = argparse.ArgumentParser(description="Backstage 只读工作台（本机 / SSH 转发）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--local-root", type=Path)
    args = parser.parse_args()
    workspace = Workspace(args.root, args.local_root or (local_root() if args.root == ROOT else args.root / ".local"))
    try:
        memory_config = workspace.snapshot()["config"]
    except (OSError, ValueError) as error:
        parser.error(f"无法读取工作区：{error}；运行 memory doctor 检查配置和数据。")
    with Server(("127.0.0.1", args.port), workspace) as server:
        print(f"Backstage · {memory_config['workspace']} · http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
