"""AV_Wall 服务端内部包（v1.8.0 起）。

v1.7.0 时全部代码挤在单个 ``server.py``（1290 行），超过「单文件不超过 1000 行」
的项目规则。v1.8.0 拆成下列模块：

- ``avwall.common`` —— 配置读写、媒体根推导与校验、路径安全、Range 解析、局域网 IP
- ``avwall.scan``   —— 扫描进程管理（``scanner.py`` 的调度）
- ``avwall.httpd``  —— HTTP 服务与路由
- ``avwall.banner`` —— 启动横幅与 ASCII 二维码

顶层 ``server.py`` 只剩入口（参数解析 + 启动），``scanner.py`` 复用
``avwall.common`` 里的常量，避免两处各写一份 ``CONTENT_DIRS`` 走偏。
"""

from __future__ import annotations

VERSION = "1.9.0"
SERVER_TOKEN = f"AvWall/{VERSION}"

__all__ = ["VERSION", "SERVER_TOKEN"]
