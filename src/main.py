"""
Zalo Relocator - clean-room implementation.
Move heavy Zalo Desktop folders away from drive C and create NTFS junctions back to their original paths.

Windows only. Run as Administrator for best compatibility.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import csv
import datetime
import os
import queue
import re
import shutil
import stat
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Union
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Zalo Relocator"
APP_VERSION = "1.3.2"
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
COPY_WORKERS = min(8, max(4, os.cpu_count() or 4))


def env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        return Path.home()
    return Path(value)


ZALO_INSTALL_ROOT = env_path("LOCALAPPDATA") / "Programs" / "Zalo"


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative_path


@dataclass(frozen=True)
class ZaloFolder:
    key: str
    label: str
    source: Path
    target_name: str
    recommended: bool = True
    description: str = ""

    def target(self, base: Path) -> Path:
        return base / self.target_name

    def backup_path(self) -> Path:
        return self.source.with_name(self.source.name + "-Old")


ZALO_FOLDERS = [
    ZaloFolder(
        key="program",
        label="Zalo app",
        source=env_path("LOCALAPPDATA") / "Programs" / "Zalo",
        target_name="Zalo",
        recommended=True,
        description="Thư mục cài Zalo Desktop",
    ),
    ZaloFolder(
        key="zalopc",
        label="ZaloPC",
        source=env_path("LOCALAPPDATA") / "ZaloPC",
        target_name="ZaloPC",
        recommended=True,
        description="Dữ liệu/cache nặng của Zalo",
    ),
    ZaloFolder(
        key="zalodata",
        label="ZaloData",
        source=env_path("APPDATA") / "ZaloData",
        target_name="ZaloData",
        recommended=True,
        description="Dữ liệu người dùng Zalo",
    ),
    ZaloFolder(
        key="received",
        label="Zalo Received Files",
        source=Path.home() / "Documents" / "Zalo Received Files",
        target_name="Zalo Received Files",
        recommended=True,
        description="File nhận qua Zalo",
    ),
    ZaloFolder(
        key="updater",
        label="zalo-updater",
        source=env_path("LOCALAPPDATA") / "zalo-updater",
        target_name="zalo-updater",
        recommended=True,
        description="Bộ cập nhật Zalo",
    ),
]


def installed_zalo_versions(root: Path = ZALO_INSTALL_ROOT) -> list[tuple[tuple[int, ...], Path]]:
    versions: list[tuple[tuple[int, ...], Path]] = []
    if not root.exists():
        return versions
    for path in root.iterdir():
        if not path.is_dir():
            continue
        match = re.fullmatch(r"Zalo-(\d+(?:\.\d+)*)", path.name, flags=re.IGNORECASE)
        if match:
            versions.append((tuple(int(part) for part in match.group(1).split(".")), path))
    return sorted(versions, key=lambda item: item[0])


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_hidden(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run a child process without flashing a console window on Windows."""
    if is_windows():
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs.setdefault("startupinfo", startupinfo)
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(args, **kwargs)


def is_reparse_point(path: Path) -> bool:
    try:
        stat_result = path.lstat()
        attrs = getattr(stat_result, "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or path.is_symlink()
    except OSError:
        return False


def get_junction_target(path: Path) -> Optional[Path]:
    """Get the target path of an NTFS junction or symlink."""
    if not is_reparse_point(path):
        return None
    try:
        raw = os.readlink(str(path))
        clean = raw.removeprefix("\\\\?\\")
        return Path(clean)
    except Exception:
        try:
            return path.resolve()
        except Exception:
            return None


def get_effective_data_path(item: ZaloFolder) -> Optional[Path]:
    """Return the folder path containing actual data (source on C:, or target if already linked)."""
    src = item.source
    if is_reparse_point(src):
        target = get_junction_target(src)
        if target and target.exists():
            return target
        return None
    if src.exists():
        return src
    return None


def remove_dir_force(path: Path) -> None:
    """Robust directory removal that clears read-only attributes on Windows."""
    def _onerror(func: Callable, p: str, exc_info: tuple) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=_onerror)


def find_all_zalo_backups(
    folders: Iterable[ZaloFolder],
    target_base: Optional[Path] = None,
    extra_dirs: Optional[Iterable[Path]] = None,
) -> list[Path]:
    """Find all Zalo backup folders (-Old) across C: drive, target bases, previous junction targets, and drive roots."""
    folders_list = list(folders)
    candidates: set[Path] = set()

    # 1. Standard C: drive backup locations
    for item in folders_list:
        candidates.add(item.backup_path())

    bases_to_check: set[Path] = set()

    # 2. Existing junction targets (e.g. if C points to D:\Zalo\... or F:\Zalo\...)
    for item in folders_list:
        if is_reparse_point(item.source):
            target = get_junction_target(item.source)
            if target:
                bases_to_check.add(target.parent)
                candidates.add(target.with_name(target.name + "-Old"))

    # 3. Destination folder configured in app
    if target_base:
        bases_to_check.add(target_base)
        bases_to_check.add(target_base.parent)

    # 4. Extra paths (e.g. last source bases)
    if extra_dirs:
        for d in extra_dirs:
            if d:
                bases_to_check.add(d)
                bases_to_check.add(d.parent)

    # 5. Scan available drive roots and [Drive]:\Zalo (e.g. D:\Zalo, E:\Zalo...)
    if is_windows():
        for letter in string.ascii_uppercase:
            zalo_dir = Path(f"{letter}:/Zalo")
            drive_root = Path(f"{letter}:/")
            try:
                if zalo_dir.is_dir():
                    bases_to_check.add(zalo_dir)
                if drive_root.is_dir():
                    bases_to_check.add(drive_root)
            except Exception:
                pass

    old_names = {f"{item.target_name.lower()}-old" for item in folders_list}
    old_names.update({f"{item.source.name.lower()}-old" for item in folders_list})

    for base in bases_to_check:
        if base and base.exists() and base.is_dir():
            try:
                for sub in base.iterdir():
                    if not sub.is_dir():
                        continue
                    sub_lower = sub.name.lower()
                    if sub_lower in old_names or (
                        sub_lower.endswith("-old")
                        and any(item.target_name.lower() in sub_lower for item in folders_list)
                    ):
                        candidates.add(sub)
            except Exception:
                pass

    found: list[Path] = []
    seen: set[Path] = set()
    for c in sorted(candidates):
        try:
            resolved = c.resolve()
            if resolved in seen:
                continue
            if c.exists() and c.is_dir():
                seen.add(resolved)
                found.append(c)
        except Exception:
            pass
    return found


def remove_link(path: Path) -> None:
    try:
        path.lstat()
    except OSError:
        return
    if is_windows():
        # rmdir on Windows removes a directory junction/symlink itself, not the target.
        result = run_hidden(["cmd", "/c", "rmdir", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            try:
                path.unlink()
            except Exception:
                raise RuntimeError((result.stderr or result.stdout or "rmdir failed").strip())
    else:
        path.unlink()


def create_junction(link_path: Path, target_path: Path) -> None:
    if not is_windows():
        raise RuntimeError("This tool supports Windows NTFS junctions only.")
    link_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.mkdir(parents=True, exist_ok=True)
    result = run_hidden(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "mklink failed").strip())


def copytree_merge(
    src: Path,
    dst: Path,
    log: Optional[Callable[[str], None]] = None,
    bytes_copied: Optional[Callable[[int], None]] = None,
) -> None:
    """Merge-copy a tree with bounded worker memory and byte progress."""
    if not src.exists():
        return
    resolved_src = src.resolve()
    resolved_dst = dst.resolve()
    if resolved_dst == resolved_src:
        return

    dst.mkdir(parents=True, exist_ok=True)
    jobs: queue.Queue[Optional[tuple[Path, Path, int]]] = queue.Queue(
        maxsize=COPY_WORKERS * 4
    )
    errors: list[Exception] = []
    error_lock = threading.Lock()
    stop_event = threading.Event()

    def copy_worker() -> None:
        while True:
            job = jobs.get()
            try:
                if job is None:
                    return
                if stop_event.is_set():
                    continue
                src_file, dst_file, size = job
                try:
                    shutil.copy2(src_file, dst_file)
                    if bytes_copied:
                        bytes_copied(size)
                except Exception as exc:
                    if isinstance(exc, PermissionError):
                        exc = RuntimeError(
                            f"Không copy được do thiếu quyền/file đang mở: {src_file}"
                        )
                    with error_lock:
                        if not errors:
                            errors.append(exc)
                    stop_event.set()
            finally:
                jobs.task_done()

    workers = [
        threading.Thread(target=copy_worker, name=f"zalo-copy-{index}", daemon=True)
        for index in range(COPY_WORKERS)
    ]
    for worker in workers:
        worker.start()

    file_count = 0
    if log:
        log(f"Bắt đầu copy với {COPY_WORKERS} luồng I/O.")
    for root, dirs, files in os.walk(src):
        # Exclude dst and subdirectories of dst to prevent infinite recursion
        dirs[:] = [
            d for d in dirs
            if (Path(root) / d).resolve() != resolved_dst
            and not (Path(root) / d).resolve().is_relative_to(resolved_dst)
        ]
        root_path = Path(root)
        dst_root = dst / root_path.relative_to(src)
        dst_root.mkdir(parents=True, exist_ok=True)
        for directory in dirs:
            (dst_root / directory).mkdir(exist_ok=True)
        for filename in files:
            src_file = root_path / filename
            try:
                size = src_file.stat().st_size
            except OSError:
                size = 0
            jobs.put((src_file, dst_root / filename, size))
            file_count += 1

    for _ in workers:
        jobs.put(None)
    jobs.join()
    for worker in workers:
        worker.join()
    if errors:
        raise errors[0]
    if log:
        log(f"Đã copy xong {file_count} file.")


def folder_size(path: Path) -> int:
    if not path.exists() or is_reparse_point(path):
        return 0
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not is_reparse_point(Path(root) / d)]
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


KNOWN_ZALO_EXE_NAMES = {
    "zalo.exe",
    "zaloexecutable.exe",
    "zalocall.exe",
    "zalocap.exe",
    "zavimeet.exe",
    "zalocrashhandler.exe",
    "zalocrashhandler64.exe",
    "zaloupdater.exe",
}

TOOL_EXCLUSION_KEYWORDS = (
    "relocator",
    "zalomove",
    "zalo-move",
    "zalo_move",
    "zalo_relocator",
)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    path: str = ""

    def __str__(self) -> str:
        return f"{self.name} (PID: {self.pid})"


def is_self_or_tool_process(pid: int, name: str, path: str = "") -> bool:
    if pid == os.getpid():
        return True

    name_lower = name.lower()
    if any(kw in name_lower for kw in TOOL_EXCLUSION_KEYWORDS):
        return True

    path_lower = path.lower() if path else ""
    if any(kw in path_lower for kw in TOOL_EXCLUSION_KEYWORDS):
        return True

    try:
        current_exe = Path(sys.executable).resolve()
        if name_lower == current_exe.name.lower():
            if not path or Path(path).resolve() == current_exe:
                return True
    except Exception:
        pass

    try:
        script_path = Path(sys.argv[0]).resolve()
        if script_path.is_file() and name_lower == script_path.name.lower():
            if not path or Path(path).resolve() == script_path:
                return True
    except Exception:
        pass

    return False


def enumerate_windows_processes() -> list[tuple[int, str, str]]:
    if not is_windows():
        return []

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    h_snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snapshot == -1 or not h_snapshot:
        return []

    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)

    results: list[tuple[int, str, str]] = []
    try:
        if kernel32.Process32FirstW(h_snapshot, ctypes.byref(pe)):
            while True:
                pid = int(pe.th32ProcessID)
                name = str(pe.szExeFile)
                path = ""
                if pid > 4:
                    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if h_proc:
                        try:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = wintypes.DWORD(len(buf))
                            if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                                path = buf.value
                        finally:
                            kernel32.CloseHandle(h_proc)
                results.append((pid, name, path))
                if not kernel32.Process32NextW(h_snapshot, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(h_snapshot)
    return results


def enumerate_tasklist_processes() -> list[tuple[int, str, str]]:
    if not is_windows():
        return []
    try:
        result = run_hidden(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5
        )
        results: list[tuple[int, str, str]] = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) >= 2:
                name = row[0].strip()
                try:
                    pid = int(row[1].strip())
                except ValueError:
                    pid = 0
                results.append((pid, name, ""))
        return results
    except Exception:
        return []


def get_running_zalo_processes(
    extra_target_base: Optional[Path] = None,
) -> list[ProcessInfo]:
    """Detect actual running Zalo processes, strictly excluding the relocator itself."""
    if not is_windows():
        return []

    try:
        raw_procs = enumerate_windows_processes()
    except Exception:
        raw_procs = []

    if not raw_procs:
        raw_procs = enumerate_tasklist_processes()

    install_root = str(ZALO_INSTALL_ROOT).lower().rstrip("\\") + "\\"
    updater_root = str(env_path("LOCALAPPDATA") / "zalo-updater").lower().rstrip("\\") + "\\"
    zalo_dirs = [install_root, updater_root]
    if extra_target_base:
        zalo_dirs.append(str(extra_target_base).lower().rstrip("\\") + "\\")

    matched_by_pid: dict[int, ProcessInfo] = {}

    for pid, name, path in raw_procs:
        if is_self_or_tool_process(pid, name, path):
            continue

        name_lower = name.lower()
        path_lower = path.lower().replace("/", "\\") if path else ""

        # 1. Match known specific Zalo executables
        if name_lower in KNOWN_ZALO_EXE_NAMES:
            matched_by_pid[pid] = ProcessInfo(pid=pid, name=name, path=path)
            continue

        # 2. Match processes running out of Zalo installation/updater directories
        if path_lower:
            for zdir in zalo_dirs:
                if path_lower.startswith(zdir):
                    matched_by_pid[pid] = ProcessInfo(pid=pid, name=name, path=path)
                    break

    return sorted(matched_by_pid.values(), key=lambda p: (p.name.lower(), p.pid))


def kill_zalo_processes(
    processes: Optional[list[ProcessInfo]] = None,
) -> tuple[int, list[str]]:
    """Terminates running Zalo processes. Returns (killed_count, error_messages)."""
    if processes is None:
        processes = get_running_zalo_processes()
    if not processes:
        return 0, []

    killed = 0
    errors: list[str] = []
    pids_to_kill = {
        p.pid for p in processes if not is_self_or_tool_process(p.pid, p.name, p.path)
    }

    for pid in pids_to_kill:
        try:
            res = run_hidden(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                killed += 1
            else:
                PROCESS_TERMINATE = 0x0001
                kernel32 = ctypes.windll.kernel32
                h_proc = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if h_proc:
                    try:
                        if kernel32.TerminateProcess(h_proc, 1):
                            killed += 1
                        else:
                            errors.append(f"PID {pid}: TerminateProcess failed")
                    finally:
                        kernel32.CloseHandle(h_proc)
                else:
                    err = (res.stderr or res.stdout or "").strip()
                    if err and "not found" not in err.lower():
                        errors.append(f"PID {pid}: {err}")
        except Exception as exc:
            errors.append(f"PID {pid}: {exc}")

    return killed, errors


def zalo_is_running() -> bool:
    return bool(get_running_zalo_processes())


class Worker:
    def __init__(
        self,
        log: Callable[[str], None],
        progress: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.log = log
        self.progress = progress
        self.completed_bytes = 0
        self.total_bytes = 0
        self._progress_lock = threading.Lock()
        self.created_backups: list[Path] = []
        self.source_bases: list[Path] = []

    def move_selected(
        self,
        folders: Iterable[ZaloFolder],
        base: Path,
        mode: str,
        create_link: bool = True,
    ) -> None:
        folders = list(folders)
        base.mkdir(parents=True, exist_ok=True)
        if mode == "copy_safe":
            self.log("Đang tính tổng dung lượng để ước lượng thời gian...")
            total = 0
            for item in folders:
                eff = get_effective_data_path(item)
                if eff and eff.exists():
                    total += folder_size(eff)
            self.total_bytes = total
            if self.progress:
                self.progress(0, self.total_bytes, "Chuẩn bị copy")
        for item in folders:
            self._move_one(item, base, mode, create_link)
        self.log("Hoàn tất di chuyển. Nên mở Zalo kiểm tra trước khi xoá thư mục -Old.")

    def _add_copied_bytes(self, amount: int) -> None:
        with self._progress_lock:
            self.completed_bytes += amount
            completed = self.completed_bytes
        if self.progress:
            self.progress(completed, self.total_bytes, "Đang copy")

    def _move_one(
        self,
        item: ZaloFolder,
        base: Path,
        mode: str,
        create_link: bool = True,
    ) -> None:
        src = item.source
        dst = item.target(base)
        bak = item.backup_path()
        self.log(f"--- {item.label} ---")

        # Xác định vị trí dữ liệu thực tế hiện tại
        is_linked = is_reparse_point(src)
        old_target = get_junction_target(src) if is_linked else None

        if is_linked:
            if old_target and old_target.exists():
                actual_src = old_target
                self.log(f"Vị trí dữ liệu hiện tại (qua liên kết cũ): {actual_src}")
            else:
                self.log(f"CẢNH BÁO: Liên kết hiện tại bị lỗi hoặc ổ cũ không kết nối: {old_target}")
                actual_src = None
        elif src.exists():
            actual_src = src
            self.log(f"Vị trí dữ liệu hiện tại (thư mục gốc ở C): {actual_src}")
        else:
            actual_src = None

        self.log(f"Vị trí đích mới: {dst}")

        if not actual_src:
            if dst.exists():
                if create_link:
                    if is_linked:
                        remove_link(src)
                    create_junction(src, dst)
                    self.log(f"Nguồn không có nhưng đích có dữ liệu, đã tạo liên kết: {src} -> {dst}")
                else:
                    self.log("Không có dữ liệu nguồn. Bỏ qua.")
            else:
                self.log("Không tìm thấy dữ liệu nguồn để di chuyển. Bỏ qua.")
            return

        try:
            if actual_src.resolve() == dst.resolve():
                self.log("Thư mục đích trùng với vị trí hiện tại. Bỏ qua sao chép.")
                if create_link and not is_linked:
                    if bak.exists():
                        raise RuntimeError(f"Backup đã tồn tại: {bak}")
                    src.rename(bak)
                    self.created_backups.append(bak)
                    create_junction(src, dst)
                    self.log(f"Đã tạo liên kết: {src} -> {dst}")
                return

            if dst.resolve().is_relative_to(actual_src.resolve()):
                old_bak = actual_src.with_name(actual_src.name + "-Old")
                if old_bak.exists():
                    old_bak = actual_src.with_name(actual_src.name + f"-Old-{int(time.time())}")
                self.log(f"Đích nằm trong thư mục nguồn, tự động đổi nguồn thành backup trước: {old_bak}")
                actual_src.rename(old_bak)
                self.created_backups.append(old_bak)
                self.source_bases.append(old_bak.parent)
                actual_src = old_bak
        except Exception as exc:
            self.log(f"Lưu ý khi kiểm tra đường dẫn nguồn/đích: {exc}")

        if mode == "copy_safe":
            self.log(f"Đang sao chép an toàn từ {actual_src} sang {dst}...")
            copytree_merge(actual_src, dst, self.log, self._add_copied_bytes)

            if not is_linked:
                if bak.exists():
                    raise RuntimeError(f"Không thể đổi tên vì backup đã tồn tại: {bak}")
                actual_src.rename(bak)
                self.created_backups.append(bak)
                self.source_bases.append(actual_src.parent)
                self.log(f"Đã đổi thư mục gốc thành backup: {bak}")
            else:
                if not str(actual_src.name).endswith("-Old") and "-Old-" not in actual_src.name:
                    old_bak = actual_src.with_name(actual_src.name + "-Old")
                    if not old_bak.exists():
                        try:
                            actual_src.rename(old_bak)
                            self.created_backups.append(old_bak)
                            self.source_bases.append(actual_src.parent)
                            self.log(f"Đã đổi thư mục ổ cũ thành backup: {old_bak}")
                        except Exception as exc:
                            self.log(f"Không đổi tên được thư mục ổ cũ (giữ nguyên): {exc}")

        elif mode == "move":
            if dst.exists() and any(dst.iterdir()):
                raise RuntimeError(f"Đích đã có dữ liệu, không move đè: {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            self.log(f"Đang di chuyển từ {actual_src} sang {dst}...")
            shutil.move(str(actual_src), str(dst))
            self.source_bases.append(actual_src.parent)
            self.log("Đã di chuyển xong dữ liệu sang đích.")
        else:
            raise RuntimeError(f"Chế độ di chuyển không hợp lệ: {mode}")

        if create_link:
            if is_reparse_point(src):
                remove_link(src)
                self.log("Đã gỡ liên kết cũ ở ổ C.")
            elif src.exists():
                if bak.exists():
                    raise RuntimeError(f"Backup đã tồn tại: {bak}")
                src.rename(bak)
                self.created_backups.append(bak)
                self.log(f"Đổi thư mục gốc thành backup: {bak}")

            create_junction(src, dst)
            self.log(f"Đã tạo liên kết mới thành công: {src} -> {dst}")
        else:
            self.log("Tùy chọn tạo liên kết đang TẮT: Dữ liệu đã chuyển sang đích nhưng chưa tạo junction ở C.")

    def relink_selected(self, folders: Iterable[ZaloFolder], base: Path) -> None:
        folders = list(folders)
        for item in folders:
            src = item.source
            dst = item.target(base)
            bak = item.backup_path()
            self.log(f"--- Tạo lại liên kết cho {item.label} ---")
            self.log(f"Đường dẫn gốc (C:): {src}")
            self.log(f"Đích liên kết: {dst}")

            if not dst.exists():
                self.log(f"BỎ QUA: Thư mục đích không tồn tại: {dst}")
                continue

            if is_reparse_point(src):
                old_target = get_junction_target(src)
                remove_link(src)
                self.log(f"Đã gỡ liên kết cũ (từng trỏ tới {old_target}).")

            if src.exists() and not is_reparse_point(src):
                if bak.exists():
                    raise RuntimeError(f"Backup đã tồn tại ở gốc: {bak}")
                src.rename(bak)
                self.created_backups.append(bak)
                self.log(f"Đổi thư mục gốc thành backup: {bak}")

            create_junction(src, dst)
            self.log(f"Đã tạo liên kết thành công: {src} -> {dst}")
        self.log("Hoàn tất tạo lại liên kết.")

    def restore_selected(self, folders: Iterable[ZaloFolder]) -> None:
        for item in folders:
            src = item.source
            bak = item.backup_path()
            self.log(f"--- Khôi phục {item.label} ---")
            target_on_other_drive = None
            if is_reparse_point(src):
                target_on_other_drive = get_junction_target(src)
                remove_link(src)
                self.log(f"Đã xoá liên kết ở ổ C (từng trỏ tới {target_on_other_drive}).")

            if bak.exists() and not src.exists():
                bak.rename(src)
                self.log(f"Đã khôi phục từ backup -Old: {src}")
            elif not src.exists() and target_on_other_drive and target_on_other_drive.exists():
                self.log(f"Đang chép dữ liệu từ ổ phụ ({target_on_other_drive}) về lại ổ C ({src})...")
                copytree_merge(target_on_other_drive, src, self.log)
                self.log(f"Đã khôi phục dữ liệu về lại ổ C: {src}")
            elif src.exists():
                self.log("Thư mục gốc ở ổ C đang tồn tại, không ghi đè.")
            else:
                self.log("Không có backup hoặc dữ liệu để khôi phục.")
        self.log("Khôi phục hoàn tất.")

    def delete_backup_paths(self, paths: Iterable[Path]) -> tuple[int, int]:
        paths_list = list(paths)
        deleted = 0
        total_freed = 0
        for p in paths_list:
            self.log(f"--- Xoá backup: {p} ---")
            if not p.exists():
                self.log(f"Không tìm thấy: {p}")
                continue
            try:
                sz = folder_size(p)
                remove_dir_force(p)
                deleted += 1
                total_freed += sz
                self.log(f"Đã xoá: {p} (giải phóng {human_size(sz)})")
            except Exception as exc:
                self.log(f"Lỗi khi xoá {p}: {exc}")
        self.log(f"Xoá backup hoàn tất ({deleted}/{len(paths_list)} thư mục đã xoá, giải phóng {human_size(total_freed)}).")
        return deleted, total_freed

    def delete_backups(self, folders: Iterable[Union[ZaloFolder, Path]]) -> tuple[int, int]:
        resolved: list[Path] = []
        for item in folders:
            if isinstance(item, Path):
                resolved.append(item)
            elif isinstance(item, ZaloFolder):
                resolved.append(item.backup_path())
        return self.delete_backup_paths(resolved)


    def clean_old_versions(self, root: Path = ZALO_INSTALL_ROOT) -> None:
        versions = installed_zalo_versions(root)
        if len(versions) < 2:
            self.log("Không có phiên bản Zalo cũ để dọn.")
            return
        newest = versions[-1][1]
        self.log(f"Giữ lại phiên bản mới nhất: {newest.name}")
        for _, path in versions[:-1]:
            self.log(f"Đang xoá phiên bản cũ: {path}")
            shutil.rmtree(path)
            self.log(f"Đã xoá: {path}")
        self.log("Dọn phiên bản Zalo cũ hoàn tất.")


class BackupManagerDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        backups: list[Path],
        on_delete_confirmed: Callable[[list[Path]], None],
    ) -> None:
        super().__init__(parent)
        self.title("Quản lý & Xoá bản sao lưu Zalo (-Old)")
        self.geometry("820x540")
        self.minsize(700, 420)
        self.configure(bg="#0f172a")
        self.transient(parent)
        self.grab_set()

        self.on_delete_confirmed = on_delete_confirmed
        self.backup_data: dict[str, dict] = {}

        self._build_ui()
        for p in backups:
            self._add_path(p)
        self._update_summary()

        self.update_idletasks()
        try:
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_x()
            py = parent.winfo_y()
            w = self.winfo_width()
            h = self.winfo_height()
            x = max(0, px + (pw - w) // 2)
            y = max(0, py + (ph - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="DANH SÁCH BẢN SAO LƯU ZALO (-OLD)",
            font=("Segoe UI", 12, "bold"),
            foreground="#f8fafc",
            background="#0f172a",
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="Tích chọn các bản sao lưu bạn muốn xoá. Các bản bỏ tích sẽ được giữ nguyên an toàn trên máy.",
            font=("Segoe UI", 9),
            foreground="#94a3b8",
            background="#0f172a",
        ).pack(anchor="w", pady=(3, 0))

        toolbar = ttk.Frame(self, padding=(18, 0, 18, 8))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Chọn tất cả", command=self._select_all).pack(side="left")
        ttk.Button(toolbar, text="Bỏ chọn tất cả", command=self._deselect_all).pack(side="left", padx=8)
        ttk.Button(toolbar, text="📁 Quét thêm thư mục khác...", command=self._scan_custom_dir).pack(side="left")

        tree_frame = ttk.Frame(self, padding=(18, 0))
        tree_frame.pack(fill="both", expand=True)

        cols = ("check", "name", "size", "mtime", "path")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=10)
        self.tree.heading("check", text="Chọn")
        self.tree.heading("name", text="Thư mục sao lưu")
        self.tree.heading("size", text="Dung lượng")
        self.tree.heading("mtime", text="Ngày sửa đổi")
        self.tree.heading("path", text="Đường dẫn")

        self.tree.column("check", width=55, anchor="center")
        self.tree.column("name", width=140)
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("mtime", width=125, anchor="center")
        self.tree.column("path", width=360)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_space)

        footer = ttk.Frame(self, padding=(18, 14))
        footer.pack(fill="x")

        self.summary_var = tk.StringVar()
        ttk.Label(
            footer,
            textvariable=self.summary_var,
            font=("Segoe UI", 10, "bold"),
            foreground="#38bdf8",
            background="#0f172a",
        ).pack(anchor="w", pady=(0, 10))

        btn_row = ttk.Frame(footer)
        btn_row.pack(fill="x")

        ttk.Button(
            btn_row,
            text="XOÁ CÁC BẢN ĐÃ CHỌN",
            style="Accent.TButton",
            command=self._confirm_and_delete,
        ).pack(side="right")
        ttk.Button(btn_row, text="Đóng / Giữ lại", command=self.destroy).pack(side="right", padx=8)

    def _add_path(self, p: Path) -> None:
        try:
            resolved = p.resolve()
        except Exception:
            resolved = p
        iid = str(resolved)
        if iid in self.backup_data or not p.exists() or not p.is_dir():
            return

        size = folder_size(p)
        try:
            mtime_ts = p.stat().st_mtime
            mtime_str = datetime.datetime.fromtimestamp(mtime_ts).strftime("%d/%m/%Y %H:%M")
        except Exception:
            mtime_str = "-"

        self.backup_data[iid] = {
            "path": p,
            "size": size,
            "mtime": mtime_str,
            "checked": True,
        }
        self.tree.insert("", "end", iid=iid, values=(
            "[ ✔ ]",
            p.name,
            human_size(size),
            mtime_str,
            str(p),
        ))

    def _on_tree_click(self, event: tk.Event) -> None:
        item_id = self.tree.identify_row(event.y)
        if item_id:
            self._toggle_item(item_id)

    def _on_space(self, event: tk.Event) -> None:
        sel = self.tree.selection()
        if sel:
            self._toggle_item(sel[0])

    def _toggle_item(self, iid: str) -> None:
        if iid in self.backup_data:
            data = self.backup_data[iid]
            data["checked"] = not data["checked"]
            chk_symbol = "[ ✔ ]" if data["checked"] else "[   ]"
            self.tree.set(iid, "check", chk_symbol)
            self._update_summary()

    def _select_all(self) -> None:
        for iid, data in self.backup_data.items():
            data["checked"] = True
            self.tree.set(iid, "check", "[ ✔ ]")
        self._update_summary()

    def _deselect_all(self) -> None:
        for iid, data in self.backup_data.items():
            data["checked"] = False
            self.tree.set(iid, "check", "[   ]")
        self._update_summary()

    def _scan_custom_dir(self) -> None:
        folder = filedialog.askdirectory(title="Chọn thư mục để quét backup Zalo")
        if folder:
            path = Path(folder)
            found = 0
            if path.name.lower().endswith("-old") or "zalo" in path.name.lower():
                self._add_path(path)
                found += 1
            try:
                for sub in path.iterdir():
                    if sub.is_dir() and (sub.name.lower().endswith("-old") or "zalo" in sub.name.lower()):
                        self._add_path(sub)
                        found += 1
            except Exception:
                pass
            self._update_summary()
            if found:
                messagebox.showinfo(APP_NAME, f"Đã quét và thêm {found} thư mục từ {folder}.")
            else:
                messagebox.showinfo(APP_NAME, f"Không tìm thấy thư mục backup nào trong {folder}.")

    def _update_summary(self) -> None:
        selected = [d for d in self.backup_data.values() if d["checked"]]
        total_size = sum(d["size"] for d in selected)
        total_items = len(self.backup_data)
        self.summary_var.set(
            f"Đã chọn: {len(selected)} / {total_items} bản sao lưu  |  Tổng dung lượng giải phóng: {human_size(total_size)}"
        )

    def _confirm_and_delete(self) -> None:
        selected_paths = [d["path"] for d in self.backup_data.values() if d["checked"]]
        if not selected_paths:
            messagebox.showwarning(APP_NAME, "Vui lòng tích chọn ít nhất 1 bản sao lưu để xoá.")
            return

        total_size = sum(
            self.backup_data[str(p.resolve())]["size"]
            for p in selected_paths
            if str(p.resolve()) in self.backup_data
        )
        labels = "\n".join(
            f"• {p.name} ({human_size(self.backup_data[str(p.resolve())]['size'])}): {p}"
            for p in selected_paths
        )

        if not messagebox.askyesno(
            APP_NAME,
            f"Bạn có chắc chắn muốn XOÁ VĨNH VIỄN {len(selected_paths)} bản sao lưu đã chọn?\n\n"
            f"{labels}\n\n"
            f"Tổng dung lượng giải phóng: {human_size(total_size)}\n\n"
            "Chỉ xoá khi bạn chắc chắn Zalo đã hoạt động tốt!",
            icon="warning",
            default=messagebox.NO,
        ):
            return

        self.destroy()
        self.on_delete_confirmed(selected_paths)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.withdraw()
        self.title(f"{APP_NAME} {APP_VERSION}")
        icon_path = resource_path("assets/ZaloRelocator.ico")
        if is_windows() and icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.configure(bg="#0f172a")
        self.queue: queue.Queue[object] = queue.Queue()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.target_var = tk.StringVar(value=str(Path("D:/Zalo") if is_windows() else Path.home() / "Zalo"))
        self.auto_subfolder_var = tk.BooleanVar(value=True)
        self.target_preview_var = tk.StringVar()
        self.last_migration_backups: list[Path] = []
        self.last_source_bases: list[Path] = []
        self.main_mode_var = tk.StringVar(value="migrate")
        self.move_mode_var = tk.StringVar(value="copy_safe")
        self.create_link_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Sẵn sàng")
        self.progress_started_at: Optional[float] = None
        self.running = False
        self.target_var.trace_add("write", lambda *_: self._update_target_preview())
        self.auto_subfolder_var.trace_add("write", lambda *_: self._update_target_preview())
        self._setup_style()
        self._build_ui()
        self._update_target_preview()
        self._set_initial_window_size()
        self.deiconify()
        self.after(200, self._poll_queue)
        self.refresh_status()

    def _set_initial_window_size(self) -> None:
        """Fit all controls while keeping the window inside the current screen."""
        self.update_idletasks()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        available_width = max(900, screen_width - 48)
        available_height = max(640, screen_height - 96)
        requested_width = self.winfo_reqwidth() + 36
        width = min(max(1180, requested_width), available_width)
        # Let wrapped labels calculate their final height at the chosen width.
        self.geometry(f"{width}x{available_height}")
        self.update_idletasks()
        requested_height = self.winfo_reqheight() + 72
        height = min(max(820, requested_height), available_height)
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(1000, width), min(700, height))

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0f172a")
        style.configure("Card.TFrame", background="#111827", relief="flat")
        style.configure("TLabel", background="#0f172a", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#0f172a", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("Title.TLabel", background="#0f172a", foreground="#f8fafc", font=("Segoe UI", 20, "bold"))
        style.configure("Card.TLabel", background="#111827", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.configure("Small.Card.TLabel", background="#111827", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("TCheckbutton", background="#111827", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", "#111827")], foreground=[("active", "#ffffff")])
        style.configure("TRadiobutton", background="#111827", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.map("TRadiobutton", background=[("active", "#111827")], foreground=[("active", "#ffffff")])
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=10)
        style.configure("TButton", font=("Segoe UI", 10), padding=8)
        style.configure("Treeview", background="#0b1220", foreground="#e5e7eb", fieldbackground="#0b1220", rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="Zalo Relocator", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Di chuyển dữ liệu Zalo khỏi ổ C, giữ đường dẫn cũ bằng NTFS junction.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        top = ttk.Frame(root, style="Card.TFrame", padding=14)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="Thư mục đích", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(top, textvariable=self.target_var, font=("Segoe UI", 10))
        entry.grid(row=1, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        ttk.Button(top, text="Chọn...", command=self.choose_target).grid(row=1, column=1, padx=(0, 8))
        ttk.Button(top, text="Mở đích", command=lambda: self.open_path(self.get_effective_target_base())).grid(row=1, column=2)
        top.columnconfigure(0, weight=1)

        subfolder_check = ttk.Checkbutton(
            top,
            text="Tự động gom trọn các thư mục vào thư mục con 'Zalo' tại đích (Khuyên dùng)",
            variable=self.auto_subfolder_var,
            command=self._update_target_preview,
        )
        subfolder_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))

        self.target_preview_label = ttk.Label(
            top,
            textvariable=self.target_preview_var,
            style="Small.Card.TLabel",
            foreground="#38bdf8",
        )
        self.target_preview_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self.progress_bar = ttk.Progressbar(
            top, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        ttk.Label(top, textvariable=self.progress_text_var, style="Small.Card.TLabel").grid(
            row=5, column=0, columnspan=3, sticky="w"
        )

        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True)
        left = ttk.Frame(mid, style="Card.TFrame", padding=14)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(mid, style="Card.TFrame", padding=14)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))

        ttk.Label(left, text="Chọn thư mục Zalo", style="Card.TLabel").pack(anchor="w")
        ttk.Label(left, text="Mặc định chọn cả 5 thư mục như tool gốc mô tả.", style="Small.Card.TLabel").pack(anchor="w", pady=(2, 10))

        self.tree = ttk.Treeview(left, columns=("src", "status", "size"), show="headings", height=9)
        self.tree.heading("src", text="Thư mục Zalo")
        self.tree.heading("status", text="Trạng thái / Vị trí hiện tại")
        self.tree.heading("size", text="Dung lượng")
        self.tree.column("src", width=190)
        self.tree.column("status", width=290)
        self.tree.column("size", width=80, anchor="e")
        self.tree.pack(fill="both", expand=True)

        checks = ttk.Frame(left, style="Card.TFrame")
        checks.pack(fill="x", pady=(12, 8))
        for i, item in enumerate(ZALO_FOLDERS):
            var = tk.BooleanVar(value=item.recommended)
            self.vars[item.key] = var
            cb = ttk.Checkbutton(checks, text=f"{item.label} — {item.description}", variable=var)
            cb.grid(row=i, column=0, sticky="w", pady=2)

        btn_row = ttk.Frame(left, style="Card.TFrame")
        btn_row.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_row, text="Chọn tất cả", command=self.select_all).pack(side="left")
        ttk.Button(btn_row, text="Bỏ chọn", command=self.clear_all).pack(side="left", padx=8)
        ttk.Button(btn_row, text="Refresh", command=self.refresh_status).pack(side="left")
        ttk.Button(btn_row, text="Đóng Zalo", command=self.close_zalo_manually).pack(side="left", padx=8)

        ttk.Label(right, text="Chế độ thao tác", style="Card.TLabel").pack(anchor="w")
        modes = ttk.Frame(right, style="Card.TFrame")
        modes.pack(fill="x", pady=(6, 8))
        ttk.Radiobutton(
            modes,
            text="Di chuyển sang ổ mới (Move / Đổi ổ cứng)",
            value="migrate",
            variable=self.main_mode_var,
            command=self._update_mode_ui,
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            modes,
            text="Chỉ tạo lại liên kết (Relink)",
            value="relink",
            variable=self.main_mode_var,
            command=self._update_mode_ui,
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            modes,
            text="Khôi phục & Dọn dẹp",
            value="cleanup",
            variable=self.main_mode_var,
            command=self._update_mode_ui,
        ).pack(anchor="w", pady=2)

        # Dynamic options container
        self.mode_details_frame = ttk.Frame(right, style="Card.TFrame", padding=(10, 8))
        self.mode_details_frame.pack(fill="x", pady=(0, 8))

        # Dynamic action buttons container
        self.action_frame = ttk.Frame(right, style="Card.TFrame")
        self.action_frame.pack(fill="x", pady=(0, 8))

        warn = ttk.Label(
            right,
            textvariable=self.status_var,
            style="Small.Card.TLabel",
            wraplength=390,
            justify="left",
        )
        warn.pack(fill="x", pady=(0, 8))

        ttk.Label(right, text="Nhật ký", style="Card.TLabel").pack(anchor="w", pady=(2, 4))
        self.log_text = tk.Text(
            right,
            height=14,
            bg="#020617",
            fg="#d1d5db",
            insertbackground="#f8fafc",
            relief="flat",
            font=("Consolas", 9),
            wrap="word",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log(f"{APP_NAME} {APP_VERSION} ready.")

        # Render dynamic options and buttons for the initial mode
        self._update_mode_ui()

    def get_effective_target_base(self) -> Path:
        raw_text = self.target_var.get().strip()
        if not raw_text:
            raw_text = "D:/Zalo" if is_windows() else str(Path.home() / "Zalo")
        raw = Path(raw_text).expanduser()
        if not self.auto_subfolder_var.get():
            return raw

        clean_name = raw.name.rstrip("\\/").lower()
        if clean_name != "zalo":
            return raw / "Zalo"
        return raw

    def _update_target_preview(self) -> None:
        eff = self.get_effective_target_base()
        if self.auto_subfolder_var.get():
            self.target_preview_var.set(
                f"Đích thực tế sẽ lưu tại: {eff}  (Tất cả thư mục Zalo, ZaloData... sẽ nằm gọn trong đây)"
            )
        else:
            self.target_preview_var.set(
                f"Đích thực tế sẽ lưu tại: {eff}  (Lưu trực tiếp vào thư mục này, không tạo thư mục Zalo bao ngoài)"
            )

    def choose_target(self) -> None:
        folder = filedialog.askdirectory(title="Chọn nơi lưu Zalo")
        if folder:
            self.target_var.set(folder)
            self._update_target_preview()
            self.refresh_status()

    def selected_items(self) -> list[ZaloFolder]:
        return [item for item in ZALO_FOLDERS if self.vars[item.key].get()]

    def select_all(self) -> None:
        for var in self.vars.values():
            var.set(True)

    def clear_all(self) -> None:
        for var in self.vars.values():
            var.set(False)

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.queue.put(f"[{timestamp}] {message}")

    def report_progress(self, completed: int, total: int, stage: str) -> None:
        self.queue.put(("progress", completed, total, stage, time.monotonic()))

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self.queue.get_nowait()
                if isinstance(event, tuple) and event and event[0] == "progress":
                    _, completed, total, stage, now = event
                    self._update_progress(int(completed), int(total), str(stage), float(now))
                else:
                    self.log_text.insert("end", str(event) + "\n")
                    self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _update_progress(self, completed: int, total: int, stage: str, now: float) -> None:
        if completed <= 0 or self.progress_started_at is None:
            self.progress_started_at = now
        percent = (completed / total * 100) if total > 0 else 0.0
        self.progress_var.set(min(100.0, percent))
        elapsed = max(0.001, now - self.progress_started_at)
        speed = completed / elapsed if completed > 0 else 0.0
        remaining = max(0, total - completed)
        eta = remaining / speed if speed > 0 else 0.0
        eta_text = self._format_duration(eta) if speed > 0 else "đang tính"
        self.progress_text_var.set(
            f"{stage}: {percent:.1f}% — {human_size(completed)} / {human_size(total)} — "
            f"{human_size(int(speed))}/s — còn khoảng {eta_text}"
        )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds_int = max(0, int(seconds))
        hours, remainder = divmod(seconds_int, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def _update_mode_ui(self) -> None:
        mode = self.main_mode_var.get()

        for child in self.mode_details_frame.winfo_children():
            child.destroy()
        for child in self.action_frame.winfo_children():
            child.destroy()

        if mode == "migrate":
            ttk.Label(
                self.mode_details_frame,
                text="Tùy chọn di chuyển:",
                style="Card.TLabel",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(0, 4))
            ttk.Radiobutton(
                self.mode_details_frame,
                text="Sao chép an toàn (giữ bản sao lưu ổ cũ)",
                value="copy_safe",
                variable=self.move_mode_var,
            ).pack(anchor="w", pady=1)
            ttk.Radiobutton(
                self.mode_details_frame,
                text="Chuyển hẳn (Move nhanh, giải phóng ổ cũ ngay)",
                value="move",
                variable=self.move_mode_var,
            ).pack(anchor="w", pady=1)
            ttk.Checkbutton(
                self.mode_details_frame,
                text="Tạo / Cập nhật liên kết (junction) ở ổ C trỏ sang đích mới",
                variable=self.create_link_var,
            ).pack(anchor="w", pady=(6, 2))

            ttk.Button(
                self.action_frame,
                text="BẮT ĐẦU DI CHUYỂN",
                style="Accent.TButton",
                command=self.start_move,
            ).pack(fill="x", pady=2)

            sub_row = ttk.Frame(self.action_frame, style="Card.TFrame")
            sub_row.pack(fill="x", pady=(4, 0))
            ttk.Button(sub_row, text="Khôi phục từ -Old", command=self.start_restore).pack(
                side="left", expand=True, fill="x", padx=(0, 4)
            )
            ttk.Button(sub_row, text="Xoá backup -Old", command=self.start_delete_backups).pack(
                side="right", expand=True, fill="x", padx=(4, 0)
            )

        elif mode == "relink":
            ttk.Label(
                self.mode_details_frame,
                text="Tạo lại liên kết (Không copy/move dữ liệu):",
                style="Card.TLabel",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(0, 2))
            ttk.Label(
                self.mode_details_frame,
                text="Dùng khi dữ liệu đã có sẵn tại ổ đích (do tự copy hoặc vừa cài lại Windows). Tool sẽ tạo junction từ ổ C trỏ tới đích.",
                style="Small.Card.TLabel",
                wraplength=380,
                justify="left",
            ).pack(anchor="w", pady=(0, 2))

            # Only relink button!
            ttk.Button(
                self.action_frame,
                text="TẠO LẠI LIÊN KẾT (RELINK)",
                style="Accent.TButton",
                command=self.start_relink,
            ).pack(fill="x", pady=2)

        elif mode == "cleanup":
            ttk.Label(
                self.mode_details_frame,
                text="Khôi phục & Dọn dẹp ổ đĩa:",
                style="Card.TLabel",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(0, 2))
            ttk.Label(
                self.mode_details_frame,
                text="Khôi phục dữ liệu Zalo về lại ổ C mặc định, hoặc xoá các bản sao lưu -Old và các phiên bản Zalo cũ để giải phóng dung lượng.",
                style="Small.Card.TLabel",
                wraplength=380,
                justify="left",
            ).pack(anchor="w", pady=(0, 2))

            ttk.Button(
                self.action_frame,
                text="Khôi phục dữ liệu về ổ C",
                style="Accent.TButton",
                command=self.start_restore,
            ).pack(fill="x", pady=2)
            ttk.Button(
                self.action_frame,
                text="Xoá các thư mục backup -Old",
                command=self.start_delete_backups,
            ).pack(fill="x", pady=2)
            ttk.Button(
                self.action_frame,
                text="Dọn phiên bản Zalo cũ",
                command=self.start_clean_old_versions,
            ).pack(fill="x", pady=2)

    def refresh_status(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for item in ZALO_FOLDERS:
            src = item.source
            if is_reparse_point(src):
                target = get_junction_target(src)
                if target and target.exists():
                    status = f"Linked -> {target}"
                    size = human_size(folder_size(target))
                elif target:
                    status = f"Link gãy (Mất đích: {target})"
                    size = "-"
                else:
                    status = "Linked"
                    size = "-"
            elif src.exists():
                status = "Gốc ở C:"
                size = human_size(folder_size(src))
            elif item.backup_path().exists():
                status = "Chỉ có -Old"
                size = human_size(folder_size(item.backup_path()))
            else:
                status = "Chưa có dữ liệu"
                size = "-"
            self.tree.insert("", "end", values=(f"{item.label} ({src.name})", status, size))
        admin = "Admin: OK" if is_admin() else "Admin: NO — nên chạy Run as administrator để tạo junction ổn định."
        running_procs = get_running_zalo_processes()
        if running_procs:
            names = ", ".join(sorted({p.name for p in running_procs}))
            running = f"Zalo đang chạy ({names}) — hãy tắt Zalo hoặc nhấn 'Đóng Zalo' bên dưới."
        else:
            running = "Zalo process: không thấy đang chạy."
        backups = find_all_zalo_backups(
            ZALO_FOLDERS,
            target_base=self.get_effective_target_base(),
            extra_dirs=self.last_source_bases,
        )
        backup_hint = (
            f"\nPhát hiện {len(backups)} bản backup -Old trên đĩa (bấm 'Xoá backup -Old' để dọn dẹp)."
            if backups
            else ""
        )
        self.status_var.set(
            f"{admin}\n{running}{backup_hint}\nLưu ý: nên dùng đường dẫn ngắn, không dấu, không ký tự lạ, ví dụ D:\\Zalo hoặc F:\\Zalo."
        )

    def close_zalo_manually(self) -> None:
        running_processes = get_running_zalo_processes()
        if not running_processes:
            messagebox.showinfo(APP_NAME, "Hiện không có tiến trình Zalo nào đang chạy ngầm.")
            return
        names = ", ".join(sorted({p.name for p in running_processes}))
        pids = ", ".join(str(p.pid) for p in running_processes)
        if not messagebox.askyesno(
            APP_NAME,
            f"Phát hiện các tiến trình Zalo đang chạy:\n\n{names} (PID: {pids})\n\n"
            "Bạn có chắc muốn đóng tất cả tiến trình Zalo này không?",
            icon="question",
        ):
            return
        self.log(f"Đang đóng các tiến trình Zalo ({names})...")
        killed, _ = kill_zalo_processes(running_processes)
        time.sleep(1)
        self.refresh_status()
        still_running = get_running_zalo_processes()
        if still_running:
            names_left = ", ".join(sorted({p.name for p in still_running}))
            messagebox.showwarning(
                APP_NAME,
                f"Chưa thể tắt hết: {names_left}.\nHãy kiểm tra lại quyền Administrator hoặc Task Manager.",
            )
        else:
            self.log(f"Đã đóng xong {killed} tiến trình Zalo.")
            messagebox.showinfo(APP_NAME, "Đã đóng toàn bộ tiến trình Zalo thành công.")

    def confirm_preflight(self, destructive: bool = False) -> bool:
        running_processes = get_running_zalo_processes()
        if running_processes:
            names = ", ".join(sorted({p.name for p in running_processes}))
            pids = ", ".join(str(p.pid) for p in running_processes)
            confirm_kill = messagebox.askyesno(
                APP_NAME,
                "Zalo hoặc thành phần liên quan vẫn đang chạy ngầm:\n\n"
                f"{names} (PID: {pids})\n\n"
                "Cần đóng Zalo hoàn toàn để tránh khoá file hoặc mất dữ liệu.\n\n"
                "Bạn có muốn công cụ TỰ ĐỘNG ĐÓNG các tiến trình Zalo này ngay không?\n"
                "(Chọn 'No' nếu bạn muốn tự thoát thủ công)",
                icon="warning",
            )
            if confirm_kill:
                self.log(f"Đang đóng các tiến trình Zalo ({names})...")
                killed, _ = kill_zalo_processes(running_processes)
                time.sleep(1)
                self.refresh_status()
                still_running = get_running_zalo_processes()
                if still_running:
                    names_left = ", ".join(sorted({p.name for p in still_running}))
                    messagebox.showwarning(
                        APP_NAME,
                        f"Chưa thể tắt hết tiến trình Zalo: {names_left}.\n"
                        "Vui lòng tắt Zalo trong Task Manager hoặc khay hệ thống rồi thử lại.",
                    )
                    return False
                self.log(f"Đã đóng thành công {killed} tiến trình Zalo.")
            else:
                return False
        if not is_windows():
            messagebox.showerror(APP_NAME, "Tool này chỉ dành cho Windows.")
            return False
        if not self.selected_items():
            messagebox.showwarning(APP_NAME, "Chưa chọn thư mục nào.")
            return False
        if destructive:
            return messagebox.askyesno(APP_NAME, "Thao tác này có thể xoá/khôi phục dữ liệu. Bạn chắc chắn chứ?")
        return True

    def run_worker(
        self, fn: Callable[[], None], on_success: Optional[Callable[[], None]] = None
    ) -> None:
        if self.running:
            messagebox.showinfo(APP_NAME, "Đang chạy tác vụ khác.")
            return
        self.running = True

        def task() -> None:
            try:
                fn()
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                error = str(exc)
                self.after(0, lambda: messagebox.showerror(APP_NAME, error))
            else:
                if on_success:
                    self.after(0, on_success)
            finally:
                self.running = False
                self.after(0, self.refresh_status)

        threading.Thread(target=task, daemon=True).start()

    def start_move(self) -> None:
        if not self.confirm_preflight(False):
            return
        self.progress_var.set(0.0)
        self.progress_text_var.set("Đang chuẩn bị...")
        self.progress_started_at = None
        target = self.get_effective_target_base()
        mode = self.move_mode_var.get()
        create_link = self.create_link_var.get()
        items = self.selected_items()
        worker = Worker(self.log, self.report_progress)

        def on_move_success() -> None:
            self.last_migration_backups = list(worker.created_backups)
            self.last_source_bases = list(worker.source_bases)
            self.verify_zalo_after_move(items)

        self.run_worker(
            lambda: worker.move_selected(items, target, mode, create_link),
            on_move_success,
        )

    def start_relink(self) -> None:
        if not self.confirm_preflight(False):
            return
        target = self.get_effective_target_base()
        items = self.selected_items()
        existing_targets = [item for item in items if item.target(target).exists()]
        if not existing_targets:
            messagebox.showerror(
                APP_NAME,
                f"Không tìm thấy dữ liệu tại thư mục đích:\n{target}\n\n"
                "Chế độ 'Tạo lại liên kết' yêu cầu dữ liệu đã tồn tại sẵn ở thư mục đích.\n"
                "Hãy chọn đúng thư mục chứa dữ liệu Zalo hoặc chọn chế độ 'Di chuyển sang ổ mới'."
            )
            return

        worker = Worker(self.log, self.report_progress)
        self.run_worker(
            lambda: worker.relink_selected(items, target),
            lambda: self.verify_zalo_after_relink(items),
        )

    def verify_zalo_after_relink(self, items: list[ZaloFolder]) -> None:
        self.progress_var.set(100.0)
        self.progress_text_var.set("Đã tạo liên kết — mở Zalo để kiểm tra")
        zalo_exe = ZALO_INSTALL_ROOT / "Zalo.exe"
        try:
            if zalo_exe.exists():
                os.startfile(str(zalo_exe))
                self.log(f"Đã mở Zalo để kiểm tra: {zalo_exe}")
            else:
                self.log(f"Không tìm thấy Zalo.exe tại {zalo_exe}")
        except Exception as exc:
            self.log(f"Không thể tự mở Zalo: {exc}")
        messagebox.showinfo(APP_NAME, "Đã tạo lại liên kết thành công! Hãy kiểm tra hoạt động của Zalo.")

    def verify_zalo_after_move(self, items: list[ZaloFolder]) -> None:
        self.progress_var.set(100.0)
        self.progress_text_var.set("Hoàn tất — đang mở Zalo để kiểm tra")
        zalo_exe = ZALO_INSTALL_ROOT / "Zalo.exe"
        try:
            if not zalo_exe.exists():
                raise FileNotFoundError(f"Không tìm thấy Zalo.exe tại {zalo_exe}")
            os.startfile(str(zalo_exe))  # type: ignore[attr-defined]
            self.log(f"Đã mở Zalo để kiểm tra: {zalo_exe}")
        except Exception as exc:
            self.log(f"Không thể tự mở Zalo: {exc}")
            messagebox.showwarning(
                APP_NAME,
                f"Không thể tự mở Zalo. Backup -Old vẫn được giữ lại an toàn.\n\n{exc}",
            )
            return

        target = self.get_effective_target_base()
        extra_dirs = list(self.last_source_bases)
        backups = find_all_zalo_backups(items, target_base=target, extra_dirs=extra_dirs)
        if not backups:
            messagebox.showinfo(APP_NAME, "Đã mở Zalo. Không có backup -Old nào để xử lý.")
            return

        def do_delete(selected_paths: list[Path]) -> None:
            worker = Worker(self.log)
            self.run_worker(
                lambda: worker.delete_backup_paths(selected_paths),
                lambda: messagebox.showinfo(
                    APP_NAME,
                    f"Đã xoá hoàn tất {len(selected_paths)} thư mục backup đã chọn!",
                ),
            )

        delete_now = messagebox.askyesno(
            APP_NAME,
            f"Zalo đã được mở. Hãy kiểm tra đăng nhập, tin nhắn và gửi/nhận file.\n\n"
            f"Phát hiện {len(backups)} bản sao lưu -Old trên đĩa.\n"
            "Bạn có muốn xem danh sách và chọn các bản sao lưu cần xoá ngay không?\n\n"
            "(Chọn 'No' nếu muốn giữ lại toàn bộ và dọn dẹp sau)",
            default=messagebox.NO,
        )
        if delete_now:
            BackupManagerDialog(self, backups, do_delete)
        else:
            self.log("Người dùng chọn giữ lại toàn bộ bản sao lưu -Old.")

    def start_restore(self) -> None:
        if not self.confirm_preflight(True):
            return
        items = self.selected_items()
        worker = Worker(self.log)
        self.run_worker(lambda: worker.restore_selected(items))

    def start_delete_backups(self) -> None:
        if not is_windows():
            messagebox.showerror(APP_NAME, "Tool này chỉ dành cho Windows.")
            return

        items = self.selected_items() or ZALO_FOLDERS
        target = self.get_effective_target_base()
        extra_dirs = list(self.last_source_bases)

        backups = find_all_zalo_backups(items, target_base=target, extra_dirs=extra_dirs)

        def do_delete(selected_paths: list[Path]) -> None:
            worker = Worker(self.log)
            self.run_worker(
                lambda: worker.delete_backup_paths(selected_paths),
                lambda: messagebox.showinfo(
                    APP_NAME,
                    f"Đã xoá hoàn tất {len(selected_paths)} thư mục backup đã chọn!",
                ),
            )

        if not backups:
            choose_manual = messagebox.askyesno(
                APP_NAME,
                "Không tự động tìm thấy thư mục backup -Old ở các vị trí mặc định (C:, ổ đích, hoặc D..Z:\\Zalo).\n\n"
                "Bạn có muốn tự duyệt chọn thư mục để quét xoá không?",
            )
            if choose_manual:
                chosen = filedialog.askdirectory(title="Chọn thư mục để quét backup Zalo")
                if chosen:
                    backups = find_all_zalo_backups(items, target_base=target, extra_dirs=[Path(chosen)])
            if not backups:
                messagebox.showinfo(APP_NAME, "Không tìm thấy thư mục backup -Old nào.")
                return

        BackupManagerDialog(self, backups, do_delete)

    def start_clean_old_versions(self) -> None:
        if not is_windows():
            messagebox.showerror(APP_NAME, "Tính năng này chỉ dành cho Windows.")
            return
        running_processes = get_running_zalo_processes()
        if running_processes:
            names = ", ".join(sorted({p.name for p in running_processes}))
            confirm_kill = messagebox.askyesno(
                APP_NAME,
                f"Phát hiện Zalo đang chạy ({names}).\n\n"
                "Cần đóng Zalo trước khi dọn phiên bản cũ.\n"
                "Bạn có muốn tự động đóng Zalo ngay không?",
                icon="warning",
            )
            if confirm_kill:
                self.log(f"Đang đóng các tiến trình Zalo ({names})...")
                killed, _ = kill_zalo_processes(running_processes)
                time.sleep(1)
                self.refresh_status()
                if zalo_is_running():
                    messagebox.showwarning(APP_NAME, "Chưa tắt được Zalo. Vui lòng tắt thủ công rồi thử lại.")
                    return
                self.log(f"Đã đóng {killed} tiến trình Zalo.")
            else:
                return
        versions = installed_zalo_versions()
        if len(versions) < 2:
            messagebox.showinfo(APP_NAME, "Không tìm thấy phiên bản Zalo cũ để dọn.")
            return
        newest = versions[-1][1]
        old_versions = [path for _, path in versions[:-1]]
        reclaim = sum(folder_size(path) for path in old_versions)
        names = "\n".join(f"• {path.name}" for path in old_versions)
        if not messagebox.askyesno(
            APP_NAME,
            f"Sẽ giữ lại: {newest.name}\n\n"
            f"Sẽ xoá:\n{names}\n\n"
            f"Dung lượng có thể giải phóng: {human_size(reclaim)}\n\n"
            "Bạn chắc chắn muốn tiếp tục?",
            default=messagebox.NO,
        ):
            return
        worker = Worker(self.log)
        self.run_worker(worker.clean_old_versions)

    def open_path(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            if is_windows():
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
