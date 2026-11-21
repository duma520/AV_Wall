"""AV_Wall/server.py —— 99 AV 海报墙本地 HTTP 服务端（入口）。

本模块只依赖 Python 3.10 标准库。v1.8.0 起实现拆分为包 ``avwall/``：

- ``avwall/common.py`` —— 配置、媒体根、路径安全、Range 解析
- ``avwall/scan.py``   —— 扫描进程管理
- ``avwall/httpd.py``  —— HTTP 服务与路由
- ``avwall/banner.py`` —— 启动横幅与 ASCII 二维码

本文件只保留**入口**（参数解析 → 读配置 → 起服务 → 跑横幅），便于阅读与维护。

对外接口（v1.7.0 起，详见同目录《API 说明.md》）
----------------------------------------------------------------
* ``GET  /``                ``index.html``（与 server.py 同目录，UTF-8，no-store）
* ``GET  /avwall/*``        本目录静态资源（.js/.css/.json/.ico，no-store）
* ``GET  /data/*``          本目录 ``data/`` 静态资源（等价 /avwall/data/*）
* ``GET  /api/ping``        健康检查 ``{"ok": true}``
* ``GET  /api/library``     ``data/library.json``（缺失返回 503）
* ``GET  /api/status``      服务与扫描状态
* ``GET  /api/info``        局域网地址 + 二维码矩阵
* ``GET  /api/root``        读取当前媒体根（指定目录）
* ``GET  /api/root?path=``  校验某目录是否可用（不修改任何状态）
* ``POST /api/root``        设置媒体根（落盘 + 实时生效 + 自动重扫）
* ``GET  /api/config``      读取完整配置（含 media_root）
* ``POST /api/config``      修改配置（media_root）
* ``POST /api/scan``        触发扫描（**仅本机**；异步触发，立刻返回 202，
  进度用 ``GET /api/status`` 的 ``scan`` 字段查）
* ``POST /api/shutdown``    停止服务（仅 localhost）
* ``POST /api/keepalive``   **兼容占位**，返回 200 但什么都不做（见下）
* ``GET  /media/<相对路径>`` 视频流，支持 ``Range``（206）与 ``HEAD``
* ``GET  /thumb/<相对路径>`` 图片，支持 ``HEAD``，不做 Range

**写操作（``POST /api/root``、``POST /api/config``、``POST /api/scan``、
``POST /api/shutdown``）仅允许本机（localhost）调用**；手机/平板的局域网
地址只能读。写操作还要求请求**同源**（``Origin`` 头必须与本服务一致或缺省），
防止局域网里的手机打开恶意网页时被跨站请求关掉服务。
读操作（``GET``）局域网可用，方便手机端或其它程序查询。

生命周期（v1.8.0 起：**只能手动关闭**）
------------------------------------------------
原先「打开页面 → 每 5 秒 ``POST /api/keepalive`` 保活 → 服务端看门狗发现
12 秒无人保活就自杀」这套机制**已整套删除**。原因：过于脆弱 —— 手机锁屏、
切到后台、弱网重连、浏览器后台节流定时器，都会被误判成「页面已关闭」而
把服务杀掉，用户还得反复在设置里开关。

现在只剩两条关闭途径：

1. 在运行服务的命令行窗口按 ``Ctrl+C``；
2. 本机调用 ``POST /api/shutdown``（局域网地址无权调用）。

``POST /api/keepalive`` 端点保留为**兼容占位**（返回 200 但什么都不做），
避免旧脚本收到 404。页面也不再发保活请求。

目录布局（v1.6.0 起）
--------------------
程序目录与**媒体目录分离**：程序在 ``G:\\Python_Code\\AV_Wall\\``，
媒体在 NAS 的 ``\\\\ds1821\\HD\\Download\\99 AV\\``（611G，**不随程序移动**）。

- 媒体根（``/media``、``/thumb`` 的解析基准）来源优先级：
  ``--root`` 参数 → ``config.json`` 的 ``media_root`` → 写死 ``DEFAULT_MEDIA_ROOT``。
  运行中可在「设置 → 指定目录」或 ``POST /api/root`` 实时切换。
- 程序自己的文件（``index.html`` / ``data/`` / ``config.json``）一律取
  「与 server.py 同目录」，与媒体根无关。
- ``scanner.py`` 会被显式传入 ``--root <媒体根>``，输出写回程序目录的 ``data/``。

设计要点（均为实测验证过的能力，改动时不可回退）：

1. ``http.server`` 手写 ``Range`` 解析，返回 ``206 Partial Content`` +
   ``Content-Range: bytes s-e/size`` + ``Accept-Ranges: bytes``，
   body 字节数精确——手机播放/拖动进度条依赖此能力。
2. ``ThreadingTCPServer`` + ``allow_reuse_address=True`` +
   ``daemon_threads=True``。
3. UNC/SMB 下流式读取大文件使用 1MB 分块；客户端断开时
   ``BrokenPipeError`` / ``ConnectionResetError`` 必须静默吞掉。
4. 目录穿越防护：逐段 ``urllib.parse.unquote`` 解码 → 拒绝 ``.``/``..``
   段与解码后仍含分隔符的段 → ``os.path.realpath`` 归一化 →
   ``os.path.commonpath`` 校验必须仍位于根目录内。
5. **服务只能手动关闭（v1.8.0 起）**：原先「关页面即停服」的保活 + 看门狗
   整套机制已删除（手机锁屏/切后台/弱网都会被误判为「页面已关闭」）。
   现在只有两条关闭途径：命令行窗口 ``Ctrl+C``、本机 ``POST /api/shutdown``。
   ``POST /api/keepalive`` 保留为兼容占位（返回 200 但什么都不做），
   页面也不再发保活请求；``config.json`` 里与自动停服相关的三个键已移除。
6. 启动横幅打印局域网地址与 ASCII 二维码，方便手机/平板扫码访问。
7. **Range 覆盖全文件时返回 200 而非 206**：当请求的 ``Range`` 等价于整文件
   （``bytes=0-`` 或 ``bytes=0-(size-1)``），按 RFC 7233 返回 ``200 OK`` 全量流，
   避免「206 但 Content-Range 覆盖整文件」的歧义响应。
   视频 MIME 统一标准化（.mp4=video/mp4/.mov=video/quicktime），避免被猜成
   application/octet-stream 后遭安卓媒体栈拒绝。
   **v1.3.0 更正**：v1.2.1 曾把「播放三角形闪烁 / 起不了播」归因于此处的 200 响应，
   该结论已被实测证伪。真实根因在前端 ``index.html``：``progress`` 事件回调里再次
   调用 ``video.load()``，形成 progress→load→abort→loadstart 的无限自打断死循环
   （详见《软件升级迭代记录》v1.3.0）。本条 200/206 分档经实测对 Chromium 足够：
   浏览器收到 ``bytes=0-`` 的 200 后，会自行再发尾部 Range（如
   ``bytes=3638394880-``）取回位于文件末尾的 moov 索引，起播正常。
8. **媒体流必须先开文件再发头（v1.8.0 修正）**：原先「先发 ``Content-Length``
   再 ``open()``」，若文件在 stat 之后消失（NAS 抖动）就会发出**永远填不满的
   响应**，客户端表现为视频卡住转圈（永久挂死）。现在先 open 成功才发头；
   读取提前结束时**强制关闭连接**，客户端立刻感知失败而不是无限等待。
9. **写操作门禁（v1.8.0 加固）**：
   * ``POST /api/scan`` 改为**仅本机**且**异步触发**（原先无门禁 + 同步执行，
     局域网任何人都能把服务占死 30 分钟）。
   * 403 之前**先排空请求体**，否则同一条 keep-alive 连接上的下一个请求
     会从请求体中间开始解析（实测得到 ``501 Unsupported method``）。
   * ``Origin`` 与 ``Host`` 必须同源，且 ``Host`` 必须指向本机
     （防 DNS 重绑定绕过写保护）。
   * 不再发送 ``Access-Control-Allow-Origin: *``，改为回显请求方自己的
     ``Origin``（等价于不跨域）。

命令行::

   python server.py [--host 0.0.0.0] [--port 8787] [--root <根目录>]
                    [--no-browser]

作者：server-dev；许可：随项目内部分发。
"""

from __future__ import annotations

import argparse
import errno
import mimetypes
import sys
import webbrowser
from pathlib import Path

from avwall import VERSION
from avwall.banner import print_banner
from avwall.common import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    derive_root,
    detect_lan_ip,
    load_config,
)
from avwall.httpd import AvWallHandler, AvWallServer

#: 供外部脚本读取（兼容旧版 `from server import VERSION` 的用法）
__version__ = VERSION


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="99 AV 海报墙本地 HTTP 服务端（仅标准库）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"绑定地址，默认 {DEFAULT_HOST}（手机/平板需要）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--root", default=None,
                        help="媒体根绝对路径（默认取 config.json 的 media_root，其次写死默认值）")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    parser.add_argument("--version", action="version", version=f"avwall-server {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    mimetypes.init()
    args = build_arg_parser().parse_args(argv)
    root = derive_root(args.root)
    if not root.is_dir():
        print(f"[avwall] 媒体根目录不存在：{root}", file=sys.stderr, flush=True)
        print("[avwall] 请在页面「设置 → 指定目录」里改，或用 --root 指定。",
              file=sys.stderr, flush=True)
        return 2
    # scanner.py 始终取本文件同目录，与 --root 无关
    program_dir = Path(__file__).resolve().parent
    scanner_path = program_dir / "scanner.py"

    # 读取配置：媒体根（media_root）
    cfg = load_config()

    try:
        server = AvWallServer(
            (args.host, args.port), AvWallHandler, root, scanner_path,
        )
    except OSError as exc:
        # v1.8.0：端口已被占用时给出可执行的提示，而不是抛栈。
        if exc.errno in (errno.EADDRINUSE, 10048):
            print(f"[avwall] 端口 {args.port} 已被占用——可能服务已经启动了。",
                  file=sys.stderr, flush=True)
            print("[avwall] 请先关掉那个窗口（或在它里面按 Ctrl+C），再重新启动。",
                  file=sys.stderr, flush=True)
            return 3
        raise
    port = int(server.server_address[1])
    lan_ip = detect_lan_ip()

    # 首次启动必须跑一次增量扫描；scanner.py 缺失时只提示不崩溃
    scanning = server.scan.scanner_exists
    if scanning:
        server.scan.run_async()

    print_banner(args.host, port, root, lan_ip, scanning, program_dir=program_dir)

    if not args.no_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            print("[avwall] 自动打开浏览器失败，请手动访问上面的地址。", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n服务已停止（Ctrl+C）", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
