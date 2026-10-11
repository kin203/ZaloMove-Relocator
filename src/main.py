"""
Zalo Relocator - clean-room implementation.
Move heavy Zalo Desktop folders away from drive C and create NTFS junctions back to their original paths.

Windows only. Run as Administrator for best compatibility.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import csv
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Zalo Relocator"
APP_VERSION = "1.2.2"
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
    if not path.exists():
        return False
    try:
        stat_result = path.lstat()
        attrs = getattr(stat_result, "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or path.is_symlink()
    except OSError:
        return False


def remove_link(path: Path) -> None:
    if not path.exists() and not is_reparse_point(path):
        return
    if is_windows():
        # rmdir on Windows removes a directory junction/symlink itself, not the target.
        result = run_hidden(["cmd", "/c", "rmdir", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
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
    for root, _, files in os.walk(path):
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

    def move_selected(self, folders: Iterable[ZaloFolder], base: Path, mode: str) -> None:
        folders = list(folders)
        base.mkdir(parents=True, exist_ok=True)
        if mode == "copy_safe":
            self.log("Đang tính tổng dung lượng để ước lượng thời gian...")
            self.total_bytes = sum(folder_size(item.source) for item in folders)
            if self.progress:
                self.progress(0, self.total_bytes, "Chuẩn bị copy")
        for item in folders:
            self._move_one(item, base, mode)
        self.log("Hoàn tất. Nên mở Zalo kiểm tra trước khi xoá thư mục -Old.")

    def _add_copied_bytes(self, amount: int) -> None:
        with self._progress_lock:
            self.completed_bytes += amount
            completed = self.completed_bytes
        if self.progress:
            self.progress(completed, self.total_bytes, "Đang copy")

    def _move_one(self, item: ZaloFolder, base: Path, mode: str) -> None:
        src = item.source
        dst = item.target(base)
        bak = item.backup_path()
        self.log(f"--- {item.label} ---")
        self.log(f"Gốc: {src}")
        self.log(f"Đích: {dst}")

        if is_reparse_point(src):
            self.log("Đã là liên kết. Bỏ qua.")
            return

        if mode == "relink":
            if not dst.exists():
                self.log("Không có dữ liệu ở đích để tạo lại liên kết. Bỏ qua.")
                return
            if src.exists():
                if bak.exists():
                    raise RuntimeError(f"Backup đã tồn tại: {bak}")
                src.rename(bak)
                self.log(f"Đổi thư mục gốc thành backup: {bak}")
            create_junction(src, dst)
            self.log("Đã tạo liên kết lại.")
            return

        if not src.exists():
            if dst.exists():
                create_junction(src, dst)
                self.log("Nguồn không tồn tại nhưng đích có dữ liệu, đã tạo liên kết.")
            else:
                self.log("Không thấy thư mục nguồn. Bỏ qua.")
            return

        if mode == "copy_safe":
            copytree_merge(src, dst, self.log, self._add_copied_bytes)
            if bak.exists():
                raise RuntimeError(f"Không thể đổi tên vì backup đã tồn tại: {bak}")
            src.rename(bak)
            self.log(f"Đã giữ backup: {bak}")
            create_junction(src, dst)
            self.log("Đã tạo liên kết.")
            return

        if mode == "move":
            if dst.exists() and any(dst.iterdir()):
                raise RuntimeError(f"Đích đã có dữ liệu, không move đè: {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            self.log("Đã move dữ liệu sang đích.")
            create_junction(src, dst)
            self.log("Đã tạo liên kết.")
            return

        raise RuntimeError(f"Unknown mode: {mode}")

    def restore_selected(self, folders: Iterable[ZaloFolder]) -> None:
        for item in folders:
            src = item.source
            bak = item.backup_path()
            self.log(f"--- Restore {item.label} ---")
            if is_reparse_point(src):
                remove_link(src)
                self.log("Đã xoá liên kết.")
            if bak.exists() and not src.exists():
                bak.rename(src)
                self.log(f"Đã khôi phục: {src}")
            elif bak.exists() and src.exists():
                self.log("Có backup nhưng thư mục gốc đang tồn tại, không ghi đè.")
            else:
                self.log("Không có backup -Old để khôi phục.")
        self.log("Restore hoàn tất.")

    def delete_backups(self, folders: Iterable[ZaloFolder]) -> None:
        for item in folders:
            bak = item.backup_path()
            self.log(f"--- Delete backup {item.label} ---")
            if bak.exists():
                shutil.rmtree(bak)
                self.log(f"Đã xoá: {bak}")
            else:
                self.log("Không có backup.")
        self.log("Xoá backup hoàn tất.")


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
        self.mode_var = tk.StringVar(value="copy_safe")
        self.status_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Sẵn sàng")
        self.progress_started_at: Optional[float] = None
        self.running = False
        self._setup_style()
        self._build_ui()
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
        ttk.Button(top, text="Mở đích", command=lambda: self.open_path(Path(self.target_var.get()))).grid(row=1, column=2)
        top.columnconfigure(0, weight=1)
        self.progress_bar = ttk.Progressbar(
            top, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        ttk.Label(top, textvariable=self.progress_text_var, style="Small.Card.TLabel").grid(
            row=3, column=0, columnspan=3, sticky="w"
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
        self.tree.heading("src", text="Đường dẫn gốc")
        self.tree.heading("status", text="Trạng thái")
        self.tree.heading("size", text="Dung lượng")
        self.tree.column("src", width=360)
        self.tree.column("status", width=120)
        self.tree.column("size", width=90, anchor="e")
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
        modes.pack(fill="x", pady=(8, 12))
        ttk.Radiobutton(
            modes,
            text="An toàn: copy sang đích, đổi gốc thành -Old, tạo liên kết",
            value="copy_safe",
            variable=self.mode_var,
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            modes,
            text="Move: chuyển hẳn sang đích, không giữ -Old",
            value="move",
            variable=self.mode_var,
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            modes,
            text="Tạo lại liên kết: dùng khi đã có dữ liệu ở ổ đích",
            value="relink",
            variable=self.mode_var,
        ).pack(anchor="w", pady=2)

        warn = ttk.Label(
            right,
            textvariable=self.status_var,
            style="Small.Card.TLabel",
            wraplength=390,
            justify="left",
        )
        warn.pack(fill="x", pady=(0, 12))

        action_row = ttk.Frame(right, style="Card.TFrame")
        action_row.pack(fill="x", pady=(0, 12))
        ttk.Button(action_row, text="Dọn phiên bản Zalo cũ", command=self.start_clean_old_versions).pack(fill="x", pady=4)
        ttk.Button(action_row, text="TỰ ĐỘNG DI CHUYỂN", style="Accent.TButton", command=self.start_move).pack(fill="x", pady=4)
        ttk.Button(action_row, text="Khôi phục từ -Old", command=self.start_restore).pack(fill="x", pady=4)
        ttk.Button(action_row, text="Xoá backup -Old", command=self.start_delete_backups).pack(fill="x", pady=4)

        ttk.Label(right, text="Nhật ký", style="Card.TLabel").pack(anchor="w", pady=(4, 6))
        self.log_text = tk.Text(
            right,
            height=18,
            bg="#020617",
            fg="#d1d5db",
            insertbackground="#f8fafc",
            relief="flat",
            font=("Consolas", 9),
            wrap="word",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log(f"{APP_NAME} {APP_VERSION} ready.")

    def choose_target(self) -> None:
        folder = filedialog.askdirectory(title="Chọn nơi lưu Zalo")
        if folder:
            self.target_var.set(folder)
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

    def refresh_status(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for item in ZALO_FOLDERS:
            src = item.source
            if is_reparse_point(src):
                status = "Linked"
                size = "-"
            elif src.exists():
                status = "Source OK"
                size = human_size(folder_size(src))
            elif item.backup_path().exists():
                status = "Backup -Old"
                size = human_size(folder_size(item.backup_path()))
            else:
                status = "Missing"
                size = "-"
            self.tree.insert("", "end", values=(str(src), status, size))
        admin = "Admin: OK" if is_admin() else "Admin: NO — nên chạy Run as administrator để tạo junction ổn định."
        running_procs = get_running_zalo_processes()
        if running_procs:
            names = ", ".join(sorted({p.name for p in running_procs}))
            running = f"Zalo đang chạy ({names}) — hãy tắt Zalo hoặc nhấn 'Đóng Zalo' bên dưới."
        else:
            running = "Zalo process: không thấy đang chạy."
        self.status_var.set(f"{admin}\n{running}\nLưu ý: nên dùng đường dẫn ngắn, không dấu, không ký tự lạ, ví dụ D:\\Zalo.")

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
        target = Path(self.target_var.get()).expanduser()
        mode = self.mode_var.get()
        items = self.selected_items()
        worker = Worker(self.log, self.report_progress)
        self.run_worker(
            lambda: worker.move_selected(items, target, mode),
            lambda: self.verify_zalo_after_move(items),
        )

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
                f"Không thể tự mở Zalo. Backup -Old vẫn được giữ lại.\n\n{exc}",
            )
            return

        backups = [item for item in items if item.backup_path().exists()]
        if not backups:
            messagebox.showinfo(APP_NAME, "Đã mở Zalo. Không có backup -Old mới để xử lý.")
            return

        labels = "\n".join(f"• {item.label}: {item.backup_path()}" for item in backups)
        delete_now = messagebox.askyesno(
            APP_NAME,
            "Zalo đã được mở. Hãy kiểm tra đăng nhập, tin nhắn, ảnh và gửi/nhận file.\n\n"
            "Zalo hoạt động bình thường và bạn muốn XOÁ backup -Old ngay?\n\n"
            f"{labels}\n\n"
            "Chọn No để giữ backup và xoá thủ công hoặc bằng tool sau.",
            default=messagebox.NO,
        )
        if delete_now:
            worker = Worker(self.log)
            self.run_worker(lambda: worker.delete_backups(backups))
        else:
            self.log("Người dùng chọn giữ lại backup -Old.")

    def start_restore(self) -> None:
        if not self.confirm_preflight(True):
            return
        items = self.selected_items()
        worker = Worker(self.log)
        self.run_worker(lambda: worker.restore_selected(items))

    def start_delete_backups(self) -> None:
        if not self.confirm_preflight(True):
            return
        items = self.selected_items()
        worker = Worker(self.log)
        self.run_worker(lambda: worker.delete_backups(items))

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
