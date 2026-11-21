"""AV_Wall HTTP 服务与路由（v1.8.0 起从 server.py 拆出）。

只依赖 Python 3.10 标准库。路由表：

===========================  ===============================================
``GET  /``                  ``index.html``（程序目录，UTF-8，no-store）
``GET  /avwall/*``          程序目录静态资源（.js/.css/.json/.ico）
``GET  /data/*``            程序目录 ``data/`` 静态资源
``GET  /api/ping``          健康检查
``GET  /api/library``       ``data/library.json``
``GET  /api/status``        服务与扫描状态
``GET  /api/info``          局域网地址 + 二维码矩阵
``GET  /api/root``          读取/校验媒体根
``GET  /api/config``        读取完整配置
``POST /api/scan``          同步扫描
``POST /api/root``          设置媒体根（仅本机）
``POST /api/config``        修改配置（仅本机）
``POST /api/shutdown``      停止服务（仅本机）
``GET  /media/<相对路径>``  视频流，支持 ``Range``（206）与 ``HEAD``
``GET  /thumb/<相对路径>``  图片，支持 ``HEAD``
===========================  ===============================================

生命周期（v1.8.0 起：只能手动关闭）
----------------------------------
服务**不会**因为「浏览器页面被关闭」而自动退出。v1.7.0 之前的
「关页面即停服」看门狗（页面每 5 秒保活 + 服务端 12 秒无保活自杀）
整套机制已删除，原因是它过于脆弱：手机锁屏、切后台、弱网重连、
浏览器节流定时器都会误判为「页面已关闭」而把服务杀掉，用户还得
反复在设置里开关。现在只保留两条明确的关闭途径：

* 在启动服务的命令行窗口里按 ``Ctrl+C``；
* 本机调用 ``POST /api/shutdown``（仅 localhost，且要求同源，见下）。

``POST /api/keepalive`` 端点保留为**兼容占位**（返回 200 但什么都不做），
避免旧脚本调用时收到 404。

写操作的跨站防护
----------------
``POST /api/root``、``POST /api/config``、``POST /api/shutdown``
仅允许本机（localhost）调用，**且**要求请求同源（``Origin`` 头必须与
本服务一致或缺省）。否则局域网里的手机只要打开一个恶意网页，该网页就能
用 ``fetch("http://127.0.0.1:8787/api/shutdown")`` 把本机服务关掉。
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from typing import Any, cast

from . import SERVER_TOKEN, VERSION
from .common import (
    CONTENT_DIRS,
    DEFAULT_MEDIA_ROOT,
    DENY_STATIC_NAMES,
    INDEX_HTML_NAME,
    LIBRARY_REL,
    MAX_BODY_BYTES,
    MAX_STATIC_BYTES,
    STATIC_CONTENT_TYPES,
    STATIC_EXTS,
    PathSecurityError,
    RangeNotSatisfiable,
    derive_root,
    detect_lan_ip,
    load_config,
    parse_range_header,
    paths_equivalent,
    resolve_within,
    save_config,
    validate_media_root,
)
from .media import MediaMixin
from .scan import ScanManager, patch_library_root, wait_for_scan

#: socket 读超时（秒）。v1.8.0：原先没设，局域网里「只连不发」的连接
#: 会各占一个线程永不释放（实测 20 条连接 → 线程数从 3 涨到 22）。
SOCKET_TIMEOUT_SEC = 30


# --------------------------------------------------------------------------
# 服务端
# --------------------------------------------------------------------------


class AvWallServer(ThreadingTCPServer):
    """线程化 TCP 服务；绑定根目录与扫描管理器供 handler 使用。

    生命周期（v1.8.0 起：只能手动关闭）：

    * 服务**不会**因为浏览器页面被关闭而自动退出——原先的看门狗
      （页面每 5 秒保活、服务端 12 秒无保活自杀）已删除：手机锁屏、
      切后台、弱网、浏览器定时器节流都会误判为「页面已关闭」，
      把服务杀掉，用户还得反复在设置里开关，非常难用。
    * 关闭方式只有两种：命令行窗口按 ``Ctrl+C``；或本机调用
      ``POST /api/shutdown``（仅 localhost，且要求同源）。
    """

    # v1.8.0：allow_reuse_address 必须为 False。
    # Windows 的 SO_REUSEADDR 语义与 POSIX 不同：它允许**第二个进程绑定
    # 一个已被 LISTENING 的端口**（实测 WinError 10048 只在 False 时才报），
    # 于是重复双击启动.bat 会出现两个实例同时监听 8787，流量被内核随机
    # 分流到两个进程 -> 请求时好时坏。Linux 上该选项只用于绕过 TIME_WAIT。
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 root: Path, scanner_path: Path) -> None:
        super().__init__(address, handler)
        self.root = root
        self.scanner_path = scanner_path
        self.scan = ScanManager(scanner_path, root)
        self._shutdown_requested = False
        self._lock = threading.Lock()
        # v1.8.0：启动时刻在 __init__ 里同步赋值（原先放在 watchdog 线程里，
        # 与 _should_exit 读取存在竞态）。
        self._boot_at = time.monotonic()

    def set_root(self, root: Path) -> None:
        """运行时切换媒体根（设置面板改目录 / API 调用时用）。

        同步更新 ``ScanManager`` 的根，确保后续扫描扫的是新目录。
        """
        with self._lock:
            self.root = root
        self.scan.set_root(root)

    def request_shutdown(self) -> None:
        """由 ``/api/shutdown`` 调用，平滑退出服务。"""
        with self._lock:
            self._shutdown_requested = True

    @property
    def _should_exit(self) -> bool:
        """是否该退出服务。

        v1.8.0：只有显式 ``POST /api/shutdown`` 才为 True。
        「页面关闭即停服」的保活看门狗已移除。
        """
        with self._lock:
            return self._shutdown_requested

    def _booted_secs(self) -> float:
        return time.monotonic() - self._boot_at


# --------------------------------------------------------------------------
# 请求处理
# --------------------------------------------------------------------------


class AvWallHandler(MediaMixin, BaseHTTPRequestHandler):
    """海报墙路由与静态/流式响应实现。

    媒体流与缩略图部分见 :mod:`avwall.media`（v1.8.0 拆出）。
    """

    server_version = SERVER_TOKEN
    protocol_version = "HTTP/1.1"

    # ---- 服务器上下文 ----

    @property
    def _srv(self) -> AvWallServer:
        return cast(AvWallServer, self.server)

    @property
    def _root(self) -> Path:
        return self._srv.root

    @property
    def _avwall_dir(self) -> Path:
        return self._srv.scanner_path.parent

    # ---- 请求入口 ----

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._send_options()

    def _dispatch(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path.startswith("/avwall/") or path.startswith("/data/"):
                if path.startswith("/avwall/"):
                    rel = path[len("/avwall/"):]
                else:
                    # /data/* 映射到 avwall/data/*（scanner 输出目录）
                    rel = "data/" + path[len("/data/"):]
                self._serve_static(rel)
            elif path == "/api/ping":
                self._send_json(200, {"ok": True})
            elif path == "/api/library":
                self._serve_library()
            elif path == "/api/status":
                self._serve_status()
            elif path == "/api/info":
                self._serve_info()
            elif path == "/api/scan":
                self._serve_scan()
            elif path == "/api/keepalive":
                self._serve_keepalive()
            elif path == "/api/config":
                self._serve_config()
            elif path == "/api/root":
                self._serve_root()
            elif path == "/api/shutdown":
                self._serve_shutdown()
            elif path.startswith("/media/"):
                self._serve_media(path[len("/media/"):])
            elif path.startswith("/thumb/"):
                self._serve_thumb(path[len("/thumb/"):])
            else:
                self._send_json(404, {"error": "not found", "path": path})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 单请求异常不能弄崩服务
            # v1.8.0 修正：若响应头已经发出去了，就**不能再发第二个状态行**
            # （HTTP 响应只能有一个状态行，否则客户端会解析错乱）。
            # 这时只能关闭连接让客户端感知到失败。
            if getattr(self, "_headers_buffer", None) is not None:
                self.close_connection = True
                return
            try:
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def setup(self) -> None:
        """给 socket 设读超时（v1.8.0）。

        原先没有超时：局域网里「只连不发」的连接会各占一个线程永不释放
        （实测 20 条这样的连接 → 线程数从 3 涨到 22）。
        """
        super().setup()
        try:
            self.connection.settimeout(SOCKET_TIMEOUT_SEC)
        except OSError:
            pass

    def handle(self) -> None:
        """包一层，保证 socket 层异常不会冒泡到 socketserver。"""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            self.close_connection = True

    # ---- 响应辅助 ----

    def _send_options(self) -> None:
        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
            # v1.8.0：不再发 `Access-Control-Allow-Origin: *`。
            # 通配符意味着**任何网站**都能用 fetch 读本服务的所有 GET 端点，
            # 配合目录探测接口等于把 NAS 结构暴露给全网。
            # 现在只回显「请求方自己的 Origin」= 同源策略（等于不跨域）。
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
        except OSError:
            self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_html(self, status: int, html: str) -> None:
        self._send_bytes(status, "text/html; charset=utf-8", html.encode("utf-8"))

    def _cors_header_value(self) -> str | None:
        """返回要写进 ``Access-Control-Allow-Origin`` 的值。

        v1.8.0：原先三处都硬编码 ``*``，等于允许**任何网站**跨域读本服务
        的所有端点（配合 ``GET /api/root?path=...`` 就能探测 NAS 目录结构）。
        现在只回显「请求方自己的 Origin」（同源策略，等价于不跨域），
        没有 Origin 头（脚本 / 命令行）则不发这个头。
        """
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            return origin
        return None

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        """发送带完整 ``Content-Length`` 的响应；HEAD 只发头。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = self._cors_header_value()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            if self.command == "HEAD":
                # HEAD 语义：有头无 body，且必须关闭连接，否则客户端会按
                # Content-Length 继续等 body 而挂住
                self.close_connection = True
                return
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    # ---- 权限 ----

    def _is_localhost(self) -> bool:
        """只允许本机触发保活/关机，手机/平板局域网地址不能关闭服务。"""
        addr = self.client_address[0] if self.client_address else ""
        return addr in ("127.0.0.1", "::1", "localhost")

    def _origin_allowed(self) -> bool:
        """写操作的跨站请求防护（CSRF + DNS 重绑定防护）。

        规则：

        * 没有 ``Origin`` 头 → 放行（命令行 / 脚本 / 其它程序调用，不带 Origin）
        * 有 ``Origin`` → 必须是本服务的同源（scheme://host:port 与 ``Host`` 一致）
        * 其它一律拒绝

        没有这一层时，局域网里的手机只要打开一个恶意网页，该网页就能用
        ``fetch("http://127.0.0.1:8787/api/shutdown")`` 把本机服务关掉。

        v1.8.0 追加 **DNS 重绑定** 防护：光比对 Origin 与 Host 是不够的，
        因为这两个头都是**客户端可控**的。攻击者的网页可以把 ``attacker.example``
        的 DNS 解析到 127.0.0.1（DNS rebinding），此时浏览器发的请求是

            Host: attacker.example:8787
            Origin: http://attacker.example:8787

        两者**完全一致**，原先的同源校验会放行 → 写操作被绕过。
        现在额外要求 ``Host`` 必须是**本机真实地址**（127.0.0.1 / ::1 /
        本机局域网 IP / localhost），陌生 Host 一律拒绝。
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not host:
            return True
        if not self._host_is_local(host):
            return False
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if not parsed.netloc:
            return False
        return parsed.netloc.lower() == host.lower()

    def _host_is_local(self, host: str) -> bool:
        """判断 ``Host`` 头指向的是不是本机（防 DNS 重绑定）。

        允许：``127.0.0.1[:port]`` / ``localhost[:port]`` / ``[::1][:port]``
        / 本机任一局域网 IP（含本机主机名）。
        其它一律视为可疑。
        """
        raw = host.strip()
        if not raw:
            return False
        # 去掉端口（IPv6 要先剥方括号）
        if raw.startswith("["):
            end = raw.find("]")
            if end < 0:
                return False
            hostname = raw[1:end]
        else:
            hostname = raw.rsplit(":", 1)[0] if ":" in raw else raw
        hostname = hostname.strip().lower()
        if not hostname:
            return False
        if hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return True
        try:
            addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
        except OSError:
            return False
        try:
            local = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
        except OSError:
            local = set()
        for ip in addrs:
            if ip in local:
                return True
            if ip.startswith("127.") or ip == "::1":
                return True
        return False

    def _reject_write(self, reason: str) -> bool:
        """写操作门禁：非本机 → 403；跨站 → 403。返回 True 表示已拒绝并已响应。

        v1.8.0：**先排空请求体再回 403**。原先直接 return 不读 body，
        于是同一条 keep-alive 连接上的下一个请求会从请求体中间开始解析，
        实测得到 ``HTTP/1.1 501 Unsupported method ('{"x":1}GET')``。
        """
        if not self._is_localhost():
            self._drain_body()
            self._send_json(403, {"error": reason})
            return True
        if not self._origin_allowed():
            self._drain_body()
            self._send_json(403, {"error": "cross-origin request rejected"})
            return True
        return False

    # ---- 路由实现 ----

    def _serve_index(self) -> None:
        # index.html 位于程序目录（与 server.py 同目录）；回退到 root 兼容旧布局
        index = self._avwall_dir / INDEX_HTML_NAME
        if not index.is_file():
            index = self._root / INDEX_HTML_NAME
        if not index.is_file():
            self._send_html(
                503,
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>海报墙尚未就绪</title></head><body style=\"font-family:sans-serif;\">"
                "<h2>海报墙页面尚未生成</h2>"
                f"<p>未找到 <code>{index}</code>，请等待 index.html 生成后刷新。</p>"
                "</body></html>",
            )
            return
        try:
            body = index.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read index.html failed: {exc}"})
            return
        self._send_bytes(200, "text/html; charset=utf-8", body)

    def _serve_static(self, rel: str) -> None:
        try:
            target = resolve_within(self._avwall_dir, rel)
        except PathSecurityError:
            self._send_json(403, {"error": "forbidden"})
            return
        ext = target.suffix.lower()
        if ext not in STATIC_EXTS:
            self._send_json(403, {"error": "file type not allowed"})
            return
        # v1.8.0 修正：config.json 含服务端配置、library.meta.json 含 NAS
        # 绝对路径与缓存内容——原先它们都能通过 /avwall/config.json 直接下载。
        if target.name.lower() in DENY_STATIC_NAMES:
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        try:
            size = target.stat().st_size
            if size > MAX_STATIC_BYTES:
                self._send_json(413, {"error": "static file too large"})
                return
            body = target.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read failed: {exc}"})
            return
        self._send_bytes(200, STATIC_CONTENT_TYPES[ext], body)

    def _serve_library(self) -> None:
        library = self._avwall_dir / LIBRARY_REL
        if not library.is_file():
            self._send_json(
                503,
                {"error": "library.json not found, run scanner.py"},
            )
            return
        try:
            body = library.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read library.json failed: {exc}"})
            return
        self._send_bytes(200, "application/json; charset=utf-8", body)

    def _serve_status(self) -> None:
        library = self._avwall_dir / LIBRARY_REL
        self._send_json(
            200,
            {
                "ok": True,
                "version": VERSION,
                "root": str(self._root),
                "library_exists": library.is_file(),
                "scan": self._srv.scan.snapshot(),
            },
        )

    def _serve_info(self) -> None:
        """返回局域网访问信息（IP、端口、URL 与二维码矩阵）。

        二维码以 0/1 矩阵返回，由前端渲染成图形；qrcode 库缺失时 qr 为 null，
        前端退化为直接显示 URL 文本，仍可手动输入。
        """
        port = int(self._srv.server_address[1])
        lan_ip = detect_lan_ip()
        url = f"http://{lan_ip}:{port}/"
        qr: list[list[int]] | None = None
        try:
            import qrcode  # type: ignore

            matrix = qrcode.QRCode(border=2)
            matrix.add_data(url)
            matrix.make(fit=True)
            qr = [[1 if cell else 0 for cell in row] for row in matrix.get_matrix()]
        except Exception:  # noqa: BLE001 - 二维码只是增强，缺失不影响主功能
            qr = None
        self._send_json(
            200,
            {"ok": True, "lan_ip": lan_ip, "port": port, "url": url, "qr": qr},
        )

    def _serve_scan(self) -> None:
        """触发一次增量扫描。

        v1.8.0 修正（严重）：
        * 原先**没有任何门禁** —— 局域网里任何设备都能 POST 触发扫描，
          而扫描最坏要跑 30 分钟（``SCAN_TIMEOUT_SEC``），等于任何人
          都能把服务占死。改为：仅本机（localhost）+ 同源才允许。
        * 原先**同步执行**（``run_sync``），HTTP 请求会一直挂着直到扫描结束；
          客户端一关就成僵尸请求，服务端线程还占着。改为**异步触发**
          （``run_async``）并立刻返回，扫描进度用 ``GET /api/status`` 查。
        """
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use POST"})
            return
        if self._reject_write("scan only allowed from localhost"):
            return
        if not self._srv.scan.scanner_exists:
            self._send_json(500, {"ok": False,
                                  "error": f"scanner.py not found: {self._srv.scanner_path}"})
            return
        if self._srv.scan.running:
            self._send_json(409, {"ok": False, "error": "scan already running"})
            return
        # 异步触发：立刻返回，不占住 HTTP 线程
        self._srv.scan.run_async()
        self._send_json(202, {"ok": True, "started": True,
                              "message": "scan started; poll GET /api/status for progress"})

    def _serve_keepalive(self) -> None:
        """兼容占位端点（v1.8.0 起不再需要保活）。

        「关页面即停服」机制已删除，服务不会因为没有保活而自杀。
        这里保留端点是为了不让旧脚本收到 404；它**什么都不做**，
        只回一个 ``{"ok": true, "deprecated": true}``。
        """
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use POST"})
            return
        self._drain_body()
        self._send_json(200, {"ok": True, "deprecated": True,
                              "message": "keepalive is no longer used; "
                                         "the service only stops on Ctrl+C or POST /api/shutdown"})

    def _serve_shutdown(self) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use POST"})
            return
        self._drain_body()
        if self._reject_write("shutdown only allowed from localhost"):
            return
        self._srv.request_shutdown()
        self._send_json(200, {"ok": True, "message": "shutting down"})

    def _serve_config(self) -> None:
        """读取或更新服务端配置。

        GET  -> 返回当前配置（含 media_root 与完整 config.json 内容）。
        POST -> 仅本机(localhost)可改，改动立即生效并落盘 config.json。

        v1.8.0 变更：
        * 「关闭页面后自动停服」整套机制已删除（看门狗 + 保活），
          因此 ``auto_shutdown_on_page_close`` / ``keepalive_timeout_sec`` /
          ``watchdog_interval_sec`` 三个键**不再可写**。若仍传这三个键，
          返回 400 并说明原因，避免旧脚本静默失效。
        * 新增可写键 ``media_root``（与 ``POST /api/root`` 等价），
          方便其它程序只认一个配置端点。
        """
        if self.command == "GET":
            cfg = load_config()
            self._send_json(200, {
                "ok": True,
                "media_root": str(self._srv.root),
                "default_media_root": DEFAULT_MEDIA_ROOT,
                # v1.8.0：服务只支持手动停止，这个字段固定为 manual
                "stop_mode": "manual",
                "config": cfg,
            })
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use GET/POST"})
            return
        if self._reject_write("config change only allowed from localhost"):
            return
        body = self._drain_body()
        try:
            payload = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "need a json object"})
            return

        # v1.8.0：已下线的键要明确报错，而不是静默忽略
        removed = ("auto_shutdown_on_page_close", "keepalive_timeout_sec",
                   "watchdog_interval_sec")
        hit = [k for k in removed if k in payload]
        if hit:
            self._send_json(400, {
                "error": "auto stop-on-page-close was removed in v1.8.0; "
                         "the service now stops only on Ctrl+C or POST /api/shutdown",
                "removed_keys": hit,
            })
            return

        if "media_root" not in payload:
            self._send_json(400, {
                "error": "need one of media_root",
            })
            return

        new_path = str(payload.get("media_root") or "").strip()
        result = self._apply_media_root(new_path)
        if not result.get("ok"):
            self._send_json(400, result)
            return
        self._send_json(200, {"ok": True, "saved": result.get("saved"), **result})

    def _apply_media_root(self, new_path: str) -> dict[str, Any]:
        """校验并切换媒体根：落盘 config.json + 实时生效 + 必要时重扫。

        ``POST /api/root`` 与 ``POST /api/config``（media_root 键）共用此实现，
        保证两条路径的行为完全一致。
        """
        ok, err, detail = validate_media_root(new_path)
        if not ok:
            return {"ok": False, "error": err, **detail}

        old_path = str(self._srv.root)
        # 落盘 config.json（保留其它配置项）
        cfg = load_config()
        cfg["media_root"] = new_path
        saved = save_config(cfg)
        # 实时切换：更新 root 与 ScanManager 的根
        self._srv.set_root(Path(new_path))

        # 只是写法不同（同一目录，如大小写变化）→ 不必全量重扫，
        # 直接把数据文件里的 root 字段改成新写法即可。
        same_dir = paths_equivalent(old_path, new_path)
        rescanning = False
        if same_dir:
            patch_library_root(old_path, new_path)
        else:
            rescanning = True
            try:
                self._srv.scan.run_async()
            except Exception:  # noqa: BLE001 - 扫描失败不影响本次切换本身
                pass
        return {
            "ok": True,
            "saved": saved,
            "path": new_path,
            "previous_path": old_path,
            "same_directory": same_dir,
            "rescanning": rescanning,
            "content_dirs": detail.get("content_dirs", {}),
            "message": ("媒体根已切换，正在后台重新扫描" if rescanning
                        else "媒体根写法已更新（同一目录，无需重扫）"),
        }

    def _serve_root(self) -> None:
        """媒体根目录（指定目录）的读取 / 校验 / 设置。

        GET  ?path=<待校验路径>  → 校验该路径是否可用（不修改任何状态）
        GET                      → 返回当前媒体根 + 状态
        POST {"path": "..."}      → 校验并切换媒体根，落盘 config.json，实时生效

        POST 仅允许本机（localhost）调用——与 ``/api/config``、``/api/shutdown``
        同一道门禁，避免手机/平板的局域网地址误改后服务扫不到东西。
        切换成功后自动触发一次重新扫描（异步），页面刷新即可看到新目录。
        """
        if self.command == "GET":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                      keep_blank_values=True)
            # keep_blank_values：?path= （空值）也要参与校验，而不是被当成"没传"
            probe = qs.get("path", [None])[0]
            if probe is not None:
                ok, err, detail = validate_media_root(probe)
                self._send_json(200 if ok else 400, {
                    "ok": ok, "valid": ok, "error": err or None, **detail,
                })
                return
            self._send_json(200, {
                "ok": True,
                "path": str(self._srv.root),
                "content_dirs": [d for d in CONTENT_DIRS if (self._srv.root / d).is_dir()],
                "default": DEFAULT_MEDIA_ROOT,
            })
            return

        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use GET/POST"})
            return
        if self._reject_write("media root change only allowed from localhost"):
            return

        body = self._drain_body()
        try:
            payload = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict) or "path" not in payload:
            self._send_json(400, {"error": "need path (string)"})
            return
        new_path = str(payload.get("path") or "").strip()
        # v1.8.0：与 POST /api/config 的 media_root 共用同一段实现
        result = self._apply_media_root(new_path)
        if not result.get("ok"):
            self._send_json(400, result)
            return
        self._send_json(200, result)

    # ---- 其他 ----

    def _drain_body(self) -> str:
        """读掉并返请求体文本（UTF-8），避免 keep-alive 连接错位。

        v1.8.0：加上长度上限 ``MAX_BODY_BYTES``（1MB）。
        原先直接 ``rfile.read(length)``，客户端声明
        ``Content-Length: 2GB`` 就会让服务端分配/读入巨量数据
        （内存被撑爆，且连接长期占用）。超限直接拒绝并关连接。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            # 太大：不再读（读了也会被撑爆），直接关连接
            print(f"[avwall] 请求体过大（{length} 字节 > {MAX_BODY_BYTES}），拒绝。",
                  flush=True)
            self.close_connection = True
            return ""
        if length > 0:
            try:
                raw = self.rfile.read(length)
                return raw.decode("utf-8", errors="replace")
            except OSError:
                self.close_connection = True
        return ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """紧凑日志；媒体/缩略图请求不打，避免海量噪声。"""
        try:
            path = self.path.split("?", 1)[0]
        except Exception:  # noqa: BLE001
            path = ""
        if path.startswith("/media/") or path.startswith("/thumb/"):
            return
        sys.stderr.write("[avwall] %s - %s\n" % (self.address_string(), format % args))
        sys.stderr.flush()


__all__ = ["AvWallServer", "AvWallHandler", "wait_for_scan", "derive_root"]
