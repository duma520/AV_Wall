"""AV_Wall 公共模块：配置、媒体根、路径安全、Range 解析。

v1.8.0 起从 ``server.py`` 拆出（原文件 1290 行，超过单文件 1000 行上限）。
只依赖 Python 3.10 标准库。

职责：

* ``config.json`` 的读取与**原子写**（落盘即持久化，重启自动加载）
* 媒体根推导（``--root`` → ``config.json`` → 写死默认值）与可用性校验
* 目录穿越防护（片段黑名单 + ``realpath`` + ``commonpath`` 双保险）
* ``Range`` 头解析（手机播放 / 拖动进度条依赖）
* 局域网 IP 探测
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import VERSION, SERVER_TOKEN  # noqa: F401  (SERVER_TOKEN 供 httpd 复用)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
FALLBACK_LAN_IP = "***已脱敏***-内网IP"

#: 程序目录（本包的上级）：index.html / scanner.py / config.json / data/ 都在这里
PROGRAM_DIR = Path(__file__).resolve().parent.parent

#: 媒体根默认值（UNC 绝对路径）。程序目录与媒体目录已分离（v1.6.0 起），
#: 媒体根不再由「脚本上一级」推导；改这里或 config.json 的 media_root 即可换 NAS。
#: v1.7.0：默认值改用用户指定的写法 \\ds1821\HD\Download\99 AV
#: （与旧写法 \\Ds1821\hd\... 是同一目录，Windows 不区分大小写）。
DEFAULT_MEDIA_ROOT = r"\\ds1821\HD\Download\99 AV"

CHUNK_SIZE = 1024 * 1024               # 1MB 分块，UNC/SMB 流式读取
MAX_STATIC_BYTES = 64 * 1024 * 1024    # 静态资源内存上限，超过返回 413
MAX_BODY_BYTES = 1024 * 1024           # 请求体上限，超过返回 413（防内存吃满）
MAX_THUMB_BYTES = 32 * 1024 * 1024     # 缩略图内存上限，超过返回 413
SCAN_TIMEOUT_SEC = 30 * 60             # 单次扫描超时

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
THUMB_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
#: 扫描器入库的图片扩展名（海报 / 截图）。比 THUMB_EXTS 窄：
#: 库里的 poster/shots 只收 jpg，服务端能取的图片类型更广。
IMAGE_EXTS = frozenset({".jpg"})
STATIC_EXTS = frozenset({".js", ".css", ".json", ".ico"})

#: 静态路由里**禁止**访问的文件名（即便扩展名允许）。
#: config.json 含服务端配置、library.meta.json 含 NAS 绝对路径，
#: 都不该通过 HTTP 暴露给局域网里的任何设备。
DENY_STATIC_NAMES = frozenset({"config.json", "library.meta.json"})

#: 媒体根校验失败时的**统一**文案（防目录存在性 oracle，见 validate_media_root）。
_INVALID_ROOT_MSG = "目录不可用（不存在 / 不是目录 / 不可读 / 没有内容目录）"

STATIC_CONTENT_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}

INDEX_HTML_NAME = "index.html"
LIBRARY_REL = Path("data") / "library.json"

RANGE_RE = re.compile(r"^(\d*)-(\d*)$")
# Windows 保留设备名：命中即拒绝，避免 open() 行为异常。
# 注意必须允许尾随的空格与点——Windows 会把 "CON " / "CON." 也当成设备名，
# 但用严格正则匹配会漏掉它们（Windows 的路径规范化会先去掉尾随空格/点）。
RESERVED_NAME_RE = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$",
    re.IGNORECASE,
)

PRIVATE_PREFIXES = ("192.168.", "10.")
_LINK_LOCAL_PREFIX = "169.254."

#: 媒体根下需要扫描的内容目录（scanner.py 也从这里取，避免两处走偏）。
#: 「图片」目录不在其中（被忽略）。
CONTENT_DIRS: tuple[str, ...] = ("B1", "B2", "下载完成1", "下载完成2", "下载完成3")

# 启动时打印的访问信息分隔线宽度
BANNER_WIDTH = 66

#: config.json 与程序目录同处
_CONFIG_PATH = PROGRAM_DIR / "config.json"


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class PathSecurityError(Exception):
    """请求路径越过根目录或含非法片段时抛出。"""


class RangeNotSatisfiable(Exception):
    """``Range`` 起始偏移越界，应返回 416。"""

    def __init__(self, size: int) -> None:
        super().__init__(f"range start beyond size={size}")
        self.size = size


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def paths_equivalent(a: str, b: str) -> bool:
    """比较两个路径是否指向同一位置（Windows 下大小写不敏感）。"""
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except (OSError, ValueError):
        return a == b


def atomic_write_bytes(path: Path, data: bytes, retries: int = 6) -> bool:
    """原子写文件：先写 ``<name>.tmp`` 再 ``os.replace`` 覆盖。

    避免读到「写了一半」的 JSON / 配置。Windows 上目标文件被其它进程占用时
    ``os.replace`` 可能抛 ``PermissionError``，这里做有限次退避重试。
    成功返回 True，失败返回 False（调用方决定是否报错）。
    """
    tmp = path.with_name(path.name + ".tmp")
    for attempt in range(retries):
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return True
        except PermissionError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if attempt < retries - 1:
                time.sleep(0.2 * (attempt + 1))
            continue
        except OSError:
            return False
    return False


def load_config() -> dict:
    """从 config.json 读取配置；缺失/损坏时返回默认值兜底。"""
    if _CONFIG_PATH.is_file():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                return cfg
        except Exception:  # noqa: BLE001 - 读不到配置不影响运行
            pass
    # 默认值：v1.8.0 起只保留 media_root。
    # 「关闭页面即自动停服」整套机制已删除（看门狗 + 保活），
    # 因此不再有 auto_shutdown_on_page_close / keepalive_timeout_sec /
    # watchdog_interval_sec 三个键；这里若仍返回它们，GET /api/config
    # 会把已下线的开关原样回显，造成误解。
    return {
        "media_root": DEFAULT_MEDIA_ROOT,
    }


def save_config(cfg: dict) -> bool:
    """把配置写回 config.json（原子写）；成功返回 True。"""
    payload = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_bytes(_CONFIG_PATH, payload.encode("utf-8"))


def config_path() -> Path:
    """config.json 的绝对路径（供 API 展示用）。"""
    return _CONFIG_PATH


# --------------------------------------------------------------------------
# 媒体根
# --------------------------------------------------------------------------


def derive_root(explicit: str | None) -> Path:
    """推导媒体根目录（媒体所在处，611G，**不随程序目录移动**）。

    v1.6.0 起程序目录（``AV_Wall/``）与媒体目录分离，媒体根不再由脚本位置推导：

    1. ``--root`` 命令行参数（最高优先级）；
    2. ``config.json`` 的 ``media_root``；
    3. 写死默认值 ``DEFAULT_MEDIA_ROOT``；
    4. 旧布局兜底：``<程序目录>/..``（仅当它确实含内容目录时）。

    v1.8.0 修正：原先服务端缺第 4 步，与 ``scanner.py`` 的
    ``default_root()`` 口径不一致——两边同时缺 media_root 时，
    服务端指向写死默认值、扫描器却指向旧布局，扫描与服务的根目录对不上。
    现在两处共用本函数，兜底顺序完全一致。

    UNC 路径下 ``resolve()`` 保持原样，不会退化；返回的字符串也保持 UNC 原始形式，
    以便与 ``library.meta.json`` 里的 ``root`` 逐字节一致（否则会触发全量重扫）。
    """
    if explicit:
        return Path(explicit).expanduser()
    cfg_root = load_config().get("media_root")
    if isinstance(cfg_root, str) and cfg_root.strip():
        return Path(cfg_root.strip())
    legacy = PROGRAM_DIR.parent
    if any((legacy / d).is_dir() for d in CONTENT_DIRS):
        return legacy
    return Path(DEFAULT_MEDIA_ROOT)


def validate_media_root(raw: str) -> tuple[bool, str, dict[str, Any]]:
    """校验媒体根目录是否可用。

    返回 ``(是否有效, 错误信息, 详情)``。校验项：
    - 非空、是字符串；
    - 是绝对路径；
    - 目录存在、是目录、可读；
    - 至少含一个内容目录（B1 / B2 / 下载完成1~3）——否则扫不出任何番号。

    v1.8.0 修正（目录存在性 oracle）：原先「目录不存在」与「目录下没有内容目录」
    返回**不同的错误文案**，局域网里任何设备都能用
    ``GET /api/root?path=C:\\Windows`` 反复试探出服务器能访问哪些路径
    （存在 / 不存在 / 可读分别对应不同文案）。现在所有失败原因**统一文案**，
    详情里也不再回显目录信息，只有校验通过才返回 ``content_dirs``。

    详情里带 ``content_dirs``（各内容目录下的番号数）与 ``path``，
    供设置面板预览与 API 返回（仅在通过时给出）。
    """
    detail: dict[str, Any] = {}
    text = str(raw or "").strip()
    if not text:
        return False, "目录不能为空", detail
    try:
        path = Path(text)
    except (OSError, ValueError) as exc:
        return False, f"路径无效：{exc}", detail
    if not path.is_absolute():
        return False, "必须是绝对路径（UNC 如 \\\\server\\share 或盘符如 D:\\...）", detail
    if not path.exists():
        return False, _INVALID_ROOT_MSG, detail
    if not path.is_dir():
        return False, _INVALID_ROOT_MSG, detail
    if not os.access(path, os.R_OK):
        return False, _INVALID_ROOT_MSG, detail
    found: dict[str, int] = {}
    for name in CONTENT_DIRS:
        sub = path / name
        if sub.is_dir():
            try:
                found[name] = sum(1 for _ in sub.iterdir())
            except OSError:
                found[name] = -1
    if not found:
        return False, _INVALID_ROOT_MSG, detail
    detail["content_dirs"] = found
    detail["path"] = text
    return True, "", detail


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------


def is_private_ip(ip: str) -> bool:
    """判断是否为常见局域网/内网地址。

    v1.8.0 修正：原先 ``PRIVATE_PREFIXES`` 里含 ``"172."``，导致
    ``172.15.x`` / ``172.32.x`` 这两个**公网**段被误判为内网，
    而 RFC1918 的 ``***已脱敏***-内网IP/12`` 分支成了永远走不到的死代码。
    现在按段精确判断。
    """
    if ip.startswith("127.") or ip.startswith(_LINK_LOCAL_PREFIX):
        return False
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def detect_lan_ip() -> str:
    """动态获取本机局域网 IP；失败回退 ``***已脱敏***-内网IP``。

    优先使用完全本地的 ``gethostbyname_ex``（不产生任何网络流量），
    其次使用 UDP socket 取路由源地址（connect 不实际发包），
    两者都失败才回退写死值。
    """
    candidates: list[str] = []
    try:
        candidates = list(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        candidates = []
    for ip in candidates:
        if is_private_ip(ip):
            return ip
    for ip in candidates:
        if not ip.startswith("127.") and not ip.startswith(_LINK_LOCAL_PREFIX):
            return ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return FALLBACK_LAN_IP


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------


def _is_reserved_device_name(name: str) -> bool:
    """判断一个路径片段是否是 Windows 保留设备名。

    Windows 会先对名字做规范化（**去掉尾随的空格与点**），再交给设备名解析，
    所以下面这些全部是设备名、都能被 ``open()`` 劫持成设备：

    * ``CON``  ``CON.txt``  ``CON.``  ``CON .txt``
    * ``NUL``  ``NUL ``      ``COM1``  ``LPT1``

    做法：去掉尾随空格与点 → 取第一个点之前的**基名** → 匹配设备名表。
    """
    trimmed = name.rstrip(" .")
    if not trimmed:
        return False
    # 基名 = 第一个点之前的内容；再去掉尾随空格
    # （Windows 会把 "CON .txt" 里的 "CON " 也当成设备名 CON）
    base = trimmed.split(".")[0].rstrip(" ")
    return bool(RESERVED_NAME_RE.match(base))


def decode_rel_segments(rel: str) -> list[str]:
    """把 URL 相对路径逐段 unquote 解码为安全片段列表。

    任一片段为 ``.``/``..``、解码后仍含路径分隔符、盘符、NUL，
    或命中 Windows 保留设备名，立即抛 :class:`PathSecurityError`。
    先按 ``/`` 切分再逐段解码，因此 ``%2f`` 无法伪造新目录层级。
    """
    parts = [part for part in rel.split("/") if part != ""]
    if not parts:
        raise PathSecurityError("empty relative path")
    decoded: list[str] = []
    for part in parts:
        seg = urllib.parse.unquote(part)
        if seg in ("", ".", ".."):
            raise PathSecurityError(f"非法路径片段: {seg!r}")
        if "/" in seg or "\\" in seg or ":" in seg or "\x00" in seg:
            raise PathSecurityError(f"解码后仍含分隔符: {seg!r}")
        if _is_reserved_device_name(seg):
            raise PathSecurityError(f"保留设备名: {seg!r}")
        decoded.append(seg)
    return decoded


def resolve_within(root: Path, rel: str) -> Path:
    """解析 ``root`` 内相对路径，越界抛 :class:`PathSecurityError`。

    双保险：先做片段级黑名单校验，再用 ``realpath`` + ``commonpath``
    做最终归一化校验（可抵御已存在的符号链接/联接）。
    """
    segments = decode_rel_segments(rel)
    candidate = root.joinpath(*segments)
    real_root = Path(os.path.realpath(root))
    real_target = Path(os.path.realpath(candidate))
    try:
        common = os.path.commonpath([str(real_target), str(real_root)])
    except ValueError:
        raise PathSecurityError("路径不可比较（跨卷或相对/绝对混用）") from None
    if common != str(real_root):
        raise PathSecurityError(f"越界路径: {real_target}")
    return real_target


# --------------------------------------------------------------------------
# Range
# --------------------------------------------------------------------------


def parse_range_header(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单个 ``Range`` 头，返回闭区间 ``(start, end)``。

    * ``bytes=s-e`` / ``bytes=s-`` / ``bytes=-n``（后缀形式）
    * ``end`` 越界 clamp 到 ``size-1``
    * 格式非法或范围为空返回 ``None``
      （调用方按「带了 Range 却解析失败」处理，返回 416）
    * 起始偏移越界抛 :class:`RangeNotSatisfiable`（调用方返回 416）
    """
    if not header:
        return None
    text = header.strip()
    if text.lower().startswith("bytes="):
        text = text[6:]
    if "," in text:            # 多段 Range 不支持
        return None
    match = RANGE_RE.match(text)
    if not match:
        return None
    start_raw, end_raw = match.group(1), match.group(2)
    if start_raw == "" and end_raw == "":
        return None
    if start_raw == "":        # 后缀范围 bytes=-n
        try:
            suffix = int(end_raw)
        except ValueError:
            return None
        if suffix <= 0:
            return None
        if size <= 0:
            raise RangeNotSatisfiable(size)
        start = max(0, size - suffix)
        end = size - 1
    else:
        try:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
        except ValueError:
            return None
        if end >= size:
            end = size - 1
        if start >= size:
            # 起始偏移越界：规范要求 416，绝不能退化成 200 全量
            # （否则会向手机推 3.6GB 全量流，表现为「卡死」）
            raise RangeNotSatisfiable(size)
        if start < 0 or start > end:
            return None
    if start < 0 or start > end:
        return None
    return start, end
