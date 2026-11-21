"""AV_Wall 扫描进程管理（v1.8.0 起从 server.py 拆出）。

职责：调度同目录的 ``scanner.py``（增量扫描），把状态暴露给 HTTP 层。

v1.8.0 修掉的并发问题
--------------------
* **锁分离**：原先 ``set_root()`` 与「跑整个扫描过程」共用同一把锁。
  扫描最长可跑 30 分钟（``SCAN_TIMEOUT_SEC``），于是扫描期间任何
  ``POST /api/root``（改媒体根）都会被阻塞到扫描结束，表现为「改了目录一直转圈」。
  现在拆成两把锁：``_state_lock``（短临界区，护状态与媒体根）与
  ``_scan_lock``（长临界区，串行化扫描进程）。
* **重复触发**：已有扫描在跑时，再次触发不再并发拉起第二个 scanner 进程，
  而是复用进行中的那次结果。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .common import SCAN_TIMEOUT_SEC

#: 等待「正在跑的扫描」结束的最长时间（秒）。超时视为扫描卡死，
#: 让调用方自己报错，而不是无限期挂起 HTTP 请求。
SCAN_WAIT_TIMEOUT_SEC = 5.0


def now_text() -> str:
    """本地时间字符串（用于状态里的 finished_at）。"""
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ScanManager:
    """管理同目录 ``scanner.py`` 的同步执行（启动刷新 + 手动触发）。"""

    def __init__(self, scanner_path: Path, root: Path) -> None:
        self._scanner_path = scanner_path
        self._root = root
        # 短临界区：护 _root 与状态字段
        self._state_lock = threading.Lock()
        # 长临界区：串行化扫描进程（一次只跑一个 scanner）
        self._scan_lock = threading.Lock()
        self._running = False
        self._ok: bool | None = None
        self._output = ""
        self._error: str | None = None
        self._finished_at = ""
        # v1.8.0：媒体根「版本号」。每次 set_root 自增。
        # 扫描开始时记下当时的版本号，扫描结束时若版本变了 → 说明扫描期间
        # 媒体根被改过 → 必须补扫一次新根（见 run_sync 的 while 循环）。
        # 修掉的是「切根时后台重扫被静默丢弃」：原先切根调 run_async，
        # 但那次的 run_sync 抢不到 _scan_lock（5 秒超时）就返回
        # "scan already running"，异常又被 except Exception: pass 吞掉，
        # 接口却照样回 200 {"rescanning": true} —— 新根从未被扫过，
        # config.json 与 library.json 的根目录永久对不上。
        self._root_generation = 0
        self._scan_generation = -1

    # ---- 基本属性 ----

    @property
    def scanner_exists(self) -> bool:
        return self._scanner_path.is_file()

    @property
    def root(self) -> Path:
        with self._state_lock:
            return self._root

    def set_root(self, root: Path) -> None:
        """运行时切换媒体根（设置面板改目录时用）。

        只取短锁，**不会**被正在跑的扫描阻塞。
        """
        with self._state_lock:
            self._root = root
            self._root_generation += 1

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    def snapshot(self) -> dict[str, Any]:
        """返回可 JSON 序列化的扫描状态。"""
        with self._state_lock:
            return {
                "running": self._running,
                "ok": self._ok,
                "scanner_exists": self.scanner_exists,
                "error": self._error,
                "output_tail": self._output[-2000:],
                "finished_at": self._finished_at,
            }

    # ---- 执行 ----

    def run_sync(self) -> tuple[bool, str]:
        """同步执行一次增量扫描，返回 ``(是否成功, 输出文本)``。

        已有扫描在进行时**不会**并发拉起第二个进程：等前一个结束（最多
        ``SCAN_WAIT_TIMEOUT_SEC`` 秒）后复用其结果；等不到就报「扫描正在进行中」。

        v1.8.0：扫描期间若媒体根被改过（``set_root`` 自增了 ``_root_generation``），
        本次扫描结束后**自动补扫一次**新根——保证「切根后一定要扫到新根」。
        """
        if not self.scanner_exists:
            return False, f"scanner.py not found: {self._scanner_path}"

        if not self._scan_lock.acquire(timeout=SCAN_WAIT_TIMEOUT_SEC):
            with self._state_lock:
                self._error = "scan already running"
            return False, self._error

        try:
            # 补扫次数上限：防止有人反复切目录导致无限循环
            for _ in range(3):
                ok, out = self._scan_once()
                with self._state_lock:
                    changed = self._root_generation != self._scan_generation
                if not changed:
                    return ok, out
                # 扫描期间媒体根变了 → 用新根再扫一次
                print("[avwall] 媒体根在扫描期间变更，立即补扫一次。", flush=True)
            with self._state_lock:
                self._error = "media root changed repeatedly during scan"
            return False, self._error
        finally:
            self._scan_lock.release()

    def _scan_once(self) -> tuple[bool, str]:
        """真正跑一次 scanner 进程（调用方需已持有 ``_scan_lock``）。"""
        with self._state_lock:
            # 等锁期间可能已有人跑完并改过状态，这里重新取一次
            if self._running:
                return self._ok is True, self._output
            self._running = True
            self._ok = None
            self._error = None
            self._output = ""
            root = self._root
            self._scan_generation = self._root_generation
        try:
            # v1.6.0：显式传 --root。程序与媒体目录已分离，不能再让 scanner 自推根目录
            # （否则会扫到程序所在盘符，扫出 0 个番号）。
            proc = subprocess.run(
                [sys.executable, str(self._scanner_path), "--quiet", "--root", str(root)],
                capture_output=True,
                text=True,
                timeout=SCAN_TIMEOUT_SEC,
                errors="replace",
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            with self._state_lock:
                self._output = out
                self._ok = proc.returncode == 0
                if not self._ok:
                    self._error = f"scanner.py exit code {proc.returncode}"
                return self._ok, out
        except subprocess.TimeoutExpired as exc:
            with self._state_lock:
                self._ok = False
                self._error = f"scan timeout after {SCAN_TIMEOUT_SEC}s"
            partial = exc.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return False, f"{self._error}\n{partial}"
        except Exception as exc:  # noqa: BLE001 - 不能弄崩服务
            with self._state_lock:
                self._ok = False
                self._error = f"{type(exc).__name__}: {exc}"
                return False, self._error
        finally:
            with self._state_lock:
                self._running = False
                self._finished_at = now_text()

    def run_async(self) -> None:
        """后台线程执行一次增量扫描（首次必须跑，否则无 library.json）。

        每次调用都开一个新线程；真正干活的是 ``run_sync``，它用 ``_scan_lock``
        串行化，重复触发不会并发拉起多个 scanner 进程。
        """
        thread = threading.Thread(
            target=self._run_quietly,
            name="avwall-scan",
            daemon=True,
        )
        thread.start()

    def _run_quietly(self) -> None:
        ok, _out = self.run_sync()
        if not ok and not self.scanner_exists:
            print("[avwall] 未找到 scanner.py，跳过扫描；/api/library 暂不可用。", flush=True)
        elif not ok:
            print(f"[avwall] 扫描失败：{self._error}", flush=True)
        else:
            print("[avwall] 扫描完成，/api/library 已可用。", flush=True)


def patch_library_root(old_root: str, new_root: str) -> bool:
    """把数据文件里记录的媒体根改成新的写法（仅当两者指向同一目录）。

    场景：用户把 ``\\\\ds1821\\HD\\...`` 改成 ``\\\\Ds1821\\hd\\...``（同一目录）。
    若不重扫，``library.json`` 里的 ``root`` 仍是旧写法，页面会显示旧路径；
    直接重扫又白白花几分钟。这里只把三个数据文件里的 ``root`` 字段替换成新写法。

    返回 True 表示已改写（或本来就是新写法），False 表示没改。
    """
    from .common import PROGRAM_DIR, atomic_write_bytes, paths_equivalent

    if not paths_equivalent(old_root, new_root):
        return False
    data_dir = PROGRAM_DIR / "data"
    old_json = json.dumps(old_root, ensure_ascii=False)
    new_json = json.dumps(new_root, ensure_ascii=False)
    old_field = '"root": ' + old_json
    new_field = '"root": ' + new_json
    changed = False
    for name in ("library.json", "library.js", "library.meta.json"):
        path = data_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if old_field not in text:
            continue
        # v1.8.0：改用原子写（临时文件 + os.replace）。
        # 原先直接 write_text，页面正好在刷新时会读到半截 JSON。
        if atomic_write_bytes(path, text.replace(old_field, new_field).encode("utf-8")):
            changed = True
    return changed


def wait_for_scan(manager: ScanManager, timeout: float = 600.0,
                  poll: float = 1.0) -> bool:
    """等待当前（或刚触发的）扫描结束；返回是否已结束。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not manager.running:
            return True
        time.sleep(poll)
    return not manager.running
