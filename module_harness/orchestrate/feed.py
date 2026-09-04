"""stdlib 可视化开关：零第三方依赖的极简运行 feed（roadmap 独立线）。

用 ``http.server`` 起一个只读 HTTP 服务，把已落盘的运行数据以 JSON feed
暴露给浏览器/脚本轮询查看——运行中即可看（每 tick 落盘 run.sqlite），
运行后完整看。数据组合全部走共享查询层（``module_harness.query`` /
``status.query_run_status``），本文件只做 HTTP 适配，不重实现查询逻辑。

富交互编辑器/完整 Web UX 属于生态项目 ``SpecModule_webview``；本开关只
提供"极简可见"的最低形态，供库使用者零依赖快速查看。

用法（CLI 接线见 cli.py ``feed`` 子命令）::

    specmodule feed [--host 127.0.0.1] [--port 8000] [--run-id <id>]

端点：
    GET /                      极简 HTML 页面（原生 JS 每 2s 轮询 feed）
    GET /feed.json?run_id=...  组合 JSON：status / timeline / checkpoints
                               （缺省 run_id = 最新修改的运行）
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..infra.query import (
    build_checkpoints,
    build_timeline,
    checkpoints_to_dict,
    timeline_to_dict,
)
from ..infra.status import query_run_status

log = logging.getLogger(__name__)

_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>SpecModule 运行 feed</title>
<style>
  body { font-family: ui-monospace, Consolas, monospace; margin: 2em; }
  h1 { font-size: 1.2em; }
  .phase { font-weight: bold; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid #ccc; padding: 4px 8px; text-align: left; font-size: 0.9em; }
  .err { color: #c00; }
  .ok { color: #080; }
</style>
</head>
<body>
<h1>SpecModule 运行 feed</h1>
<p>run_id: <span id="run-id">?</span> · 阶段: <span id="phase" class="phase">?</span>
   · tick: <span id="tick">?</span> · 刷新: <span id="stamp">?</span></p>
<div id="outputs"></div>
<h2>时间线</h2>
<table id="timeline"><tr><th>tick</th><th>节点</th><th>状态</th><th>输出/错误</th></tr></table>
<h2>检查点</h2>
<table id="checkpoints"><tr><th>tick</th><th>类型</th><th>fired 节点</th></tr></table>
<script>
async function refresh() {
  try {
    const r = await fetch("feed.json" + location.search);
    const d = await r.json();
    const st = d.status;
    document.getElementById("run-id").textContent = d.run_id;
    document.getElementById("phase").textContent = st ? st.phase : "无运行";
    document.getElementById("tick").textContent = st ? (st.tick ?? "—") : "—";
    document.getElementById("stamp").textContent = new Date().toLocaleTimeString();
    let oh = "";
    if (st && st.outputs) {
      for (const [node, out] of Object.entries(st.outputs)) {
        oh += "<p><b>" + node + "</b>: "
            + (typeof out === "string" ? out : JSON.stringify(out)) + "</p>";
      }
    }
    document.getElementById("outputs").innerHTML = oh;
    const tl = document.getElementById("timeline");
    while (tl.rows.length > 1) tl.deleteRow(1);
    for (const e of (d.timeline ? d.timeline.entries : [])) {
      const row = tl.insertRow();
      row.insertCell().textContent = e.tick;
      row.insertCell().textContent = e.node;
      row.insertCell().textContent = e.status || (e.error ? "✗" : "✓");
      row.insertCell().textContent = e.error || (e.output ? JSON.stringify(e.output) : "");
    }
    const cp = document.getElementById("checkpoints");
    while (cp.rows.length > 1) cp.deleteRow(1);
    for (const e of (d.checkpoints ? d.checkpoints.checkpoints : [])) {
      const row = cp.insertRow();
      row.insertCell().textContent = e.tick;
      row.insertCell().textContent = e.label ? e.label : "tick";
      row.insertCell().textContent = (e.fired || []).join(", ");
    }
  } catch (err) {
    document.getElementById("phase").textContent = "feed 读取失败: " + err;
  }
}
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""


class RunFeedHandler(BaseHTTPRequestHandler):
    """只读 feed：/ 为页面，/feed.json 为 JSON 组合。"""

    server: "RunFeedServer"  # type: ignore[misc]  # ThreadingHTTPServer 实例

    def do_GET(self) -> None:  # noqa: N802（http.server 命名约定）
        parsed = urlparse(self.path)
        if parsed.path == "/feed.json":
            self._serve_feed(parsed)
        elif parsed.path in ("/", "/index.html"):
            self._serve_html()
        else:
            self.send_error(404, "not found")

    # -- feed -----------------------------------------------------------

    def _serve_feed(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        run_id = (qs.get("run_id") or [None])[0] or self.server.latest_run_id()
        base = self.server.base_dir
        if run_id is None:
            self._send_json({"run_id": None, "error": "无运行记录"}, 404)
            return

        status = query_run_status(run_id, base_dir=base)
        timeline = build_timeline(run_id, base_dir=base)
        checkpoints = build_checkpoints(run_id, base_dir=base)
        if status is None and timeline is None:
            self._send_json({"run_id": run_id, "error": "无运行记录"}, 404)
            return
        self._send_json({
            "run_id": run_id,
            "status": status and {
                "phase": status.phase,
                "tick": status.tick,
                "fired": status.fired,
                "outputs": status.outputs,
                "error": status.error,
            },
            "timeline": timeline_to_dict(timeline) if timeline else None,
            "checkpoints": checkpoints_to_dict(checkpoints) if checkpoints else None,
        })

    def _serve_html(self) -> None:
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)


class RunFeedServer(ThreadingHTTPServer):
    """极简运行 feed 服务（零第三方依赖，stdlib http.server）。"""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        base_dir: Path | None = None,
    ) -> None:
        self.base_dir = base_dir
        super().__init__(server_address, RunFeedHandler)

    def latest_run_id(self) -> str | None:
        """与 CLI ``_latest_run_id`` 同语义：扫描 runs/ 取最新修改子目录。"""
        runs = (self.base_dir or Path.cwd()) / ".specmodule" / "runs"
        if not runs.is_dir():
            return None
        candidates = [
            p for p in runs.iterdir()
            if p.is_dir() and (p / "status.json").exists()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime).name
