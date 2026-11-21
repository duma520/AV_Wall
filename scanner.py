"""AV_Wall 海报墙扫描器。

用途
----
遍历**媒体根目录**下 5 个内容目录（B1、B2、下载完成1、下载完成2、下载完成3），
把每个「番号子目录」整理成一条记录，产出供 index.html / server.py 消费的数据文件：

- ``data/library.json``      结构化数据（UTF-8, ensure_ascii=False）
- ``data/library.js``        ``window.__AV_LIBRARY__ = <同一 JSON>;``
                             （供 file:// 下 <script> 标签加载，绕开 fetch CORS 限制）
- ``data/library.meta.json`` 增量缓存（每个番号目录的签名 + 旧记录）

以上三个文件都写在**程序目录**（本脚本所在目录）下的 ``data/``。

目录布局（v1.6.0 起）
--------------------
程序目录与媒体目录**分离**：程序在 ``G:\\Python_Code\\AV_Wall\\``，
媒体在 NAS 的 ``\\ds1821\\HD\\Download\\99 AV\\``（611G，不随程序移动）。

- 媒体根来源优先级：``config.json`` 的 ``media_root`` → 写死默认值
  ``DEFAULT_MEDIA_ROOT`` → 旧布局兜底（脚本上一级恰含内容目录时）。
- 媒体根字符串**保持原样**（UNC 原始形式，不做 abspath / 规范化），
  否则与 ``library.meta.json`` 里的 ``root`` 不一致，会触发全量重扫。
  唯一的例外是**大小写**：比较时用 ``os.path.normcase`` 归一
  （Windows 上 ``\\ds1821\\HD\\...`` 与 ``\\Ds1821\\hd\\...`` 是同一目录，
  但字节比较不同会误判成「换了根目录」而全量重扫）。

规则
----
- 忽略「图片」目录（根本身就不在 CONTENT_DIRS 中）；
  忽略 basename 匹配 ``^[bB]+$`` 的用户空标记子目录（如 b / bb / bbb）。
- 番号目录内只收 ``.jpg`` 与 ``.mp4``/``.mov``/``.m4v``/``.webm``
  （与 ``avwall/common.py`` 的 VIDEO_EXTS 同源，避免服务端能播而扫描器不收）；
  ``Thumbs.db`` 等其它文件忽略。
- 海报 = 同目录内 jpg 按 LastWriteTime 升序的第一张，其余为截图。
- 27 个番号无视频、4 个番号无 jpg 属正常现象，正常输出，不报错不跳过。
- 单目录读取失败：记入 warnings 并跳过该目录，不让整个扫描崩。
- **所有内容目录都读不到时（NAS 掉线等），直接报错退出（返回码 3）且不写任何
  输出文件**（v1.8.0）。原先会把「全部失败」写成一个 count=0 的空库，
  覆盖掉真实数据，页面随之变成空白。

CLI
---
    python scanner.py              # 增量扫描（默认）
    python scanner.py --quiet      # 只打汇总
    python scanner.py --force      # 全量重建（忽略缓存）
    python scanner.py --root PATH  # 覆盖媒体根

仅用标准库；Python 3.10。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

SCANNER_VERSION = "1.0.0"

#: 根目录下需要扫描的内容目录（「图片」目录不在其中，即被忽略）。
#: v1.8.0：改从 avwall.common 统一取，避免与 server 端各写一份走偏。
from avwall.common import (  # noqa: E402
    CONTENT_DIRS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    atomic_write_bytes,
    derive_root,
)

#: 用户空标记子目录（如 b / bb / bbb），内部 0 文件，必须忽略
MARKER_RE = re.compile(r"^[bB]+$")

#: 视频扩展名（小写比较）。v1.8.0：改从 avwall.common 统一取，
#: 与服务端 /media/ 的可播扩展名保持一致——原先服务端能播 .m4v/.webm，
#: 扫描器却只收 .mp4/.mov，导致这类文件「能播但库里没有」。
#: 实际媒体库里现有 .mp4 429 个、.mov 7 个（探针实测）。
#: 图片扩展名仍限定 .jpg（海报/截图按 mtime 排序取第一张），
#: 服务端 /thumb/ 允许更多扩展名属于「能取但未入库」，不影响播放。

LIBRARY_VERSION = 1
META_VERSION = 1

JSON_ENSURE_ASCII = False
JSON_INDENT = 1


# --------------------------------------------------------------------------- #
# 类型别名
# --------------------------------------------------------------------------- #

#: 单条番号记录
Item = dict[str, Any]
#: 目录签名：(LastWriteTime, size, 文件数)
Signature = tuple[float, int, int]
#: 增量缓存条目：{"sig": [mtime, size, nfiles], "item": Item}
CacheEntry = dict[str, Any]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def to_rel(root: str, path: str) -> str:
    """把绝对路径转成相对根目录、以 ``/`` 分隔的字符串（不做路径规范化）。"""
    rel = os.path.relpath(path, root)
    return rel.replace(os.sep, "/")


def now_iso() -> str:
    """本地时间 ISO8601（带秒级精度与时区偏移）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_marker(name: str) -> bool:
    """是否为用户空标记子目录名（b / bb / bbb / ...）。"""
    return MARKER_RE.match(name) is not None


#: 媒体根默认值、配置读取、原子写都从 avwall.common 统一取（v1.8.0），
#: 避免与 server 端各写一份走偏。程序目录与媒体目录已分离（v1.6.0 起），
#: 媒体根不再靠「脚本上一级」推导；改 config.json 的 media_root 即可换 NAS。
#: 默认值（用户指定的写法）= \\ds1821\HD\Download\99 AV
#: （与旧写法 \\Ds1821\hd\... 是同一目录，Windows 不区分大小写）。


def default_root() -> str:
    """推导媒体根目录。

    v1.6.0 起程序目录（``AV_Wall/``）与媒体目录分离，媒体根不再由脚本位置推导：

    1. ``config.json`` 的 ``media_root``（优先，实时生效）；
    2. ``DEFAULT_MEDIA_ROOT`` 写死的 UNC 绝对路径（兜底）；
    3. 旧布局兜底：``<脚本目录>/..``（仅当它确实含内容目录时）。

    v1.8.0 修正：原先第 3 步只在这里有，服务端 ``common.derive_root()`` 没有，
    两边兜底顺序不一致（同时缺 media_root 时，扫描器指向旧布局、
    服务端指向写死默认值，根目录对不上）。现在统一走
    ``avwall.common.derive_root()``，口径完全一致。

    返回值保持 UNC 原始字符串形式（不做 abspath / 规范化），
    否则会与 ``library.meta.json`` 里的 ``root`` 不一致而触发全量重扫。
    """
    return str(derive_root(None))


def program_data_dir() -> Path:
    """数据输出目录：始终是**程序目录**下的 ``data/``（不再跟随媒体根）。"""
    return Path(__file__).resolve().parent / "data"


# --------------------------------------------------------------------------- #
# 单目录枚举
# --------------------------------------------------------------------------- #

def dir_signature(path: str) -> Signature | None:
    """计算番号目录签名 ``(mtime, size, 文件数)``；失败返回 ``None``。

    只做一次 scandir；Windows 上 DirEntry 的 stat 走目录枚举缓存，
    不产生额外网络往返，因此增量路径明显快于全量枚举。
    空标记子目录不计入文件数（与 scan_one 的过滤口径保持一致）。
    """
    try:
        st = os.stat(path)
        n_files = 0
        with os.scandir(path) as it:
            for entry in it:
                if is_marker(entry.name):
                    continue
                try:
                    if entry.is_dir():
                        continue
                    n_files += 1
                except OSError:
                    continue  # 单条目属性不可读：保守跳过，不算整目录失败
        return (st.st_mtime, st.st_size, n_files)
    except OSError:
        return None


def scan_one(root: str, src: str, av_dir: str) -> Item:
    """枚举一个番号目录，产出记录；读取失败抛 OSError（由调用方记 warning）。

    单条目 stat 失败会向上抛，使整个番号目录被记为 warning 后跳过，
    避免产出半截记录污染缓存。
    """
    try:
        entries = list(os.scandir(av_dir))
    except OSError as exc:
        raise OSError(f"{av_dir}: {exc}") from exc

    av_id = os.path.basename(av_dir)
    # (mtime, name, 绝对路径)
    jpgs: list[tuple[float, str, str]] = []
    videos: list[dict[str, Any]] = []

    for entry in entries:
        name = entry.name
        if is_marker(name):
            continue
        try:
            if entry.is_dir():
                continue  # 不递归子目录（b/k 等空标记）
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXTS:
                st = entry.stat()
                jpgs.append((st.st_mtime, name, entry.path))
            elif ext in VIDEO_EXTS:
                st = entry.stat()
                mtime = float(st.st_mtime)
                videos.append({
                    "path": to_rel(root, entry.path),
                    "name": name,
                    "size": int(st.st_size),
                    "mtime": mtime,
                    "mtime_iso": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                })
        except OSError as exc:
            raise OSError(f"{entry.path}: {exc}") from exc

    # jpg 按 LastWriteTime 升序；同名时按名称兜底，保证结果确定
    jpgs.sort(key=lambda t: (t[0], t[1]))
    poster: str | None = to_rel(root, jpgs[0][2]) if jpgs else None
    shots: list[str] = [to_rel(root, p) for _, _, p in jpgs[1:]]
    videos.sort(key=lambda v: v["name"])

    return {
        "id": av_id,
        "src": src,
        "key": f"{src}/{av_id}",
        "poster": poster,
        "shots": shots,
        "videos": videos,
        "vcount": len(videos),
        "has_video": bool(videos),
    }


# --------------------------------------------------------------------------- #
# 增量缓存
# --------------------------------------------------------------------------- #

def load_meta(meta_path: Path, root: str) -> dict[str, CacheEntry]:
    """读取增量缓存；文件缺失 / 损坏 / 根目录不一致时返回空表。

    根目录比对用「路径等价」判定（Windows 下大小写不敏感）：
    ``\\\\ds1821\\HD\\Download\\99 AV`` 与 ``\\\\Ds1821\\hd\\Download\\99 AV``
    是同一目录，若按字节比对会在仅大小写不同时误判为「换了根」→ 触发全量重扫。
    """
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cached_root = raw.get("root")
    same_root = (
        cached_root == root
        if isinstance(cached_root, str)
        else False
    )
    if not same_root and isinstance(cached_root, str):
        try:
            same_root = os.path.normcase(os.path.normpath(cached_root)) == \
                os.path.normcase(os.path.normpath(root))
        except (OSError, ValueError):
            same_root = False
    if raw.get("version") != META_VERSION or not same_root:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def save_meta(meta_path: Path, root: str, entries: dict[str, CacheEntry]) -> None:
    """写入增量缓存（原子写：先写临时文件再 os.replace，避免半截文件）。

    v1.8.0：改用 ``avwall.common.atomic_write_bytes``，与 config.json 的落盘
    走同一套「临时文件 + 退避重试 + os.replace」逻辑，避免 NAS/杀软占用时
    直接写坏缓存文件。
    """
    payload = {"version": META_VERSION, "root": root, "entries": entries}
    data = json.dumps(payload, ensure_ascii=JSON_ENSURE_ASCII, indent=JSON_INDENT).encode("utf-8")
    if not atomic_write_bytes(meta_path, data):
        raise OSError(f"写入缓存失败：{meta_path}")


# --------------------------------------------------------------------------- #
# 扫描主流程
# --------------------------------------------------------------------------- #

def list_av_dirs(content_path: str) -> list[str]:
    """列出一个内容目录下的番号目录绝对路径（忽略空标记与非目录项）。"""
    out: list[str] = []
    with os.scandir(content_path) as it:
        for entry in it:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            if is_marker(entry.name):
                continue
            out.append(entry.path)
    return out


def scan_root(
    root: str,
    force: bool,
    quiet: bool,
) -> tuple[list[Item], list[str], dict[str, int], dict[str, CacheEntry]]:
    """扫描根目录，返回 (items, warnings, 统计信息, 新签名表)。"""
    warnings: list[str] = []
    # v1.6.0：缓存随**程序目录**走，不再拼在媒体根下（程序与媒体已分离）
    meta_path = program_data_dir() / "library.meta.json"
    cached: dict[str, CacheEntry] = {} if force else load_meta(meta_path, root)

    items: list[Item] = []
    new_entries: dict[str, CacheEntry] = {}
    reused = 0
    rescanned = 0
    skipped = 0
    # v1.8.0：统计内容目录的读取成功/失败个数。
    # 全部失败（NAS 掉线、共享不可达）时说明是**环境故障**而不是「库真的空了」，
    # 此时绝不能把 count=0 的空库写出去覆盖真实数据（见 main()）。
    dirs_ok = 0
    dirs_failed = 0

    for src in CONTENT_DIRS:
        content_path = os.path.join(root, src)
        try:
            av_dirs = list_av_dirs(content_path)
        except OSError as exc:
            warnings.append(f"[内容目录读取失败] {content_path}: {exc}")
            dirs_failed += 1
            continue
        dirs_ok += 1

        for av_dir in av_dirs:
            key = f"{src}/{os.path.basename(av_dir)}"
            sig = dir_signature(av_dir)

            if sig is not None:
                old = cached.get(key)
                # v1.8.0 修正：原先写的是 tuple(old.get("sig", ())) == sig，
                # 但缓存里可能存着 "sig": null（见下方写缓存处）→ 抛
                # TypeError: 'NoneType' object is not iterable，
                # 导致**之后每次扫描都崩**（只有 --force 能自愈）。
                # 这里统一用 `or ()` 兜底，null 视为「无签名 → 必须重扫」。
                old_sig = old.get("sig") if isinstance(old, dict) else None
                if old is not None and old_sig is not None:
                    try:
                        same = tuple(old_sig) == sig
                    except TypeError:
                        same = False
                    if same:
                        item = old.get("item")
                        if isinstance(item, dict) and "videos" in item:
                            items.append(item)
                            new_entries[key] = old
                            reused += 1
                            continue

            try:
                item = scan_one(root, src, av_dir)
            except OSError as exc:
                warnings.append(f"[番号目录读取失败] {exc}")
                skipped += 1
                continue

            items.append(item)
            # v1.8.0 修正：签名算不出来（None）时**不写签名**，
            # 只留 item。否则下次扫描读缓存时 tuple(None) 会崩（见上）。
            # 用旧签名兜底也行，但直接不写更简单：下次必定重扫这个目录。
            new_entries[key] = {"sig": list(sig) if sig is not None else None, "item": item}
            rescanned += 1

        if not quiet:
            print(f"  · {src}: {len(av_dirs)} 个番号目录")

    # 按 id 小写排序；同 id（跨内容目录重复）按 src 兜底，保证输出稳定
    items.sort(key=lambda it: (it["id"].lower(), it["src"]))

    stats = {
        "reused": reused,
        "rescanned": rescanned,
        "skipped": skipped,
        "dirs_ok": dirs_ok,
        "dirs_failed": dirs_failed,
    }
    return items, warnings, stats, new_entries


def build_library(root: str, items: list[Item], warnings: list[str]) -> dict[str, Any]:
    """组装 library.json 的顶层结构。"""
    total_videos = sum(int(it["vcount"]) for it in items)
    total_posters = sum(1 for it in items if it["poster"])
    total_shots = sum(len(it["shots"]) for it in items)
    return {
        "version": LIBRARY_VERSION,
        "root": root,
        "generated": now_iso(),
        "count": len(items),
        "total_videos": total_videos,
        "total_posters": total_posters,
        "total_shots": total_shots,
        "items": items,
        "warnings": warnings,
    }


def write_outputs(data_dir: Path, library: dict[str, Any]) -> tuple[Path, Path, int]:
    """写出 library.json 与 library.js，返回 (json 路径, js 路径, json 字节数)。

    v1.8.0：改用原子写（临时文件 + os.replace）。原先直接 ``write_text``，
    扫描过程中若被读取（页面正好在刷新）可能读到半截 JSON。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(library, ensure_ascii=JSON_ENSURE_ASCII, indent=JSON_INDENT)

    json_path = data_dir / "library.json"
    if not atomic_write_bytes(json_path, text.encode("utf-8")):
        raise OSError(f"写入失败：{json_path}")
    json_bytes = json_path.stat().st_size

    js_path = data_dir / "library.js"
    js_text = "window.__AV_LIBRARY__ = " + text + ";"
    if not atomic_write_bytes(js_path, js_text.encode("utf-8")):
        raise OSError(f"写入失败：{js_path}")
    return json_path, js_path, json_bytes


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AV_Wall 海报墙扫描器：遍历媒体根下 5 个内容目录，生成 library.json / library.js",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="媒体根绝对路径（默认取 config.json 的 media_root，其次写死默认值）",
    )
    parser.add_argument("--force", action="store_true", help="全量重建，忽略增量缓存")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总行")
    parser.add_argument("--version", action="version", version=f"avwall-scanner {SCANNER_VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root if args.root else default_root()
    # UNC 路径保持原样，不做 abspath / 规范化
    if not root.startswith("\\\\"):
        root = os.path.abspath(root)

    if not os.path.isdir(root):
        print(f"[错误] 根目录不存在或不可访问: {root}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"AV_Wall 扫描器 v{SCANNER_VERSION} · 媒体根: {root}")
        print(f"模式: {'全量重建' if args.force else '增量扫描'}")

    started = time.perf_counter()
    items, warnings, stats, new_entries = scan_root(root, force=args.force, quiet=args.quiet)

    # v1.8.0 修正（严重）：原先无论扫出多少都照写。
    # NAS 掉线时 5 个内容目录**全部**读失败 → items 为空 → 仍写出
    # count=0 的 library.json 把 369 条真实库覆盖掉，缓存也被清空，
    # 而进程仍然 return 0（=“扫描成功”）→ 页面显示空库。
    # 现在：所有内容目录都读失败时判定为环境故障，
    # **不写任何输出文件**，返回非 0 退出码让服务端知道扫描失败。
    if stats["dirs_ok"] == 0 and stats["dirs_failed"] > 0:
        print(
            f"[错误] 所有内容目录都无法读取（{stats['dirs_failed']} 个失败），"
            f"疑似共享不可达：{root}",
            file=sys.stderr,
        )
        for w in warnings:
            print(f"  ! {w}", file=sys.stderr)
        print("[错误] 已保留原有数据文件，未做覆盖。", file=sys.stderr)
        return 3

    library = build_library(root, items, warnings)

    # v1.6.0：输出到**程序目录**的 data/（不再写进媒体根）
    data_dir = program_data_dir()
    json_path, js_path, json_bytes = write_outputs(data_dir, library)

    try:
        save_meta(data_dir / "library.meta.json", root, new_entries)
    except OSError as exc:
        warnings.append(f"[缓存写入失败] {data_dir / 'library.meta.json'}: {exc}")

    elapsed = time.perf_counter() - started

    if not args.quiet:
        print(
            f"缓存: 复用 {stats['reused']} 个 / 重扫 {stats['rescanned']} 个 / "
            f"跳过 {stats['skipped']} 个；警告 {len(warnings)} 条"
        )
        for w in warnings:
            print(f"  ! {w}")

    print(
        f"扫描完成: 番号 {library['count']} 个 / 视频 {library['total_videos']} 个 / "
        f"海报 {library['total_posters']} 张 / 截图 {library['total_shots']} 张，"
        f"耗时 {elapsed:.1f}s"
    )
    print(f"数据文件: {json_path} ({json_bytes} 字节)")
    print(f"JS 文件:  {js_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
