#!/usr/bin/env python3
"""本地模拟包源服务:同时模拟 npm registry 和 Go module proxy。

用法:
    python3 server.py --port 18081 --npm data/public_npm.json --go data/public_go.json
    python3 server.py --port 18082 --npm data/private_npm.json --go data/private_go.json

端点:
    GET /npm/<name>            npm 包元数据 (scoped 包写作 /@scope%2Fname 或 /@scope/name)
    GET /npm/-/all             全部 npm 包名清单
    GET /go/<module>/@v/list   Go 模块版本列表
    GET /go/<module>/@latest   Go 模块最新版本
    GET /go/-/modules          全部 Go 模块清单
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Handler(BaseHTTPRequestHandler):
    npm_data = {}
    go_data = {}

    def log_message(self, fmt, *args):  # 静默日志
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path == "/npm/-/all":
            return self._send(200, sorted(self.npm_data.keys()))
        if path == "/go/-/modules":
            return self._send(200, sorted(self.go_data.keys()))

        if path == "/npm" or path.startswith("/npm/"):
            name = path[len("/npm/"):].lstrip("/")
            # npm 官方约定 scoped 包编码为 @scope%2fname
            name = name.replace("%2f", "/").replace("%2F", "/")
            pkg = self.npm_data.get(name)
            if not pkg:
                return self._send(404, {"error": "Not found"})
            versions = {v: {"name": name, "version": v} for v in pkg["versions"]}
            latest = pkg["versions"][-1]
            return self._send(200, {
                "name": name,
                "description": pkg.get("description", ""),
                "versions": versions,
                "dist-tags": {"latest": latest},
                "time": {v: pkg.get("published", "2026-01-01T00:00:00Z") for v in pkg["versions"]},
            })

        m = re.match(r"^/go/(.+)/@v/(list)$", path)
        if m:
            module = m.group(1)
            mod = self.go_data.get(module)
            if not mod:
                return self._send(404, {"error": "not found"})
            return self._send(200, mod["versions"])

        m = re.match(r"^/go/(.+)/@latest$", path)
        if m:
            module = m.group(1)
            mod = self.go_data.get(module)
            if not mod:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"Version": mod["versions"][-1]})

        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--npm", required=True)
    ap.add_argument("--go", required=True)
    ap.add_argument("--label", default="registry")
    args = ap.parse_args()

    Handler.npm_data = load(args.npm)
    Handler.go_data = load(args.go)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[{args.label}] 127.0.0.1:{args.port}  npm包 {len(Handler.npm_data)} 个, go模块 {len(Handler.go_data)} 个")
    srv.serve_forever()


if __name__ == "__main__":
    main()
