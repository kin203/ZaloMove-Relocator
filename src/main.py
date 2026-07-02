"""
Zalo Relocator - clean-room implementation.
Move heavy Zalo Desktop folders away from drive C and create NTFS junctions back to their original paths.

Windows only. Run as Administrator for best compatibility.
"""
from __future__ import annotations

import ctypes
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
APP_VERSION = "1.2.1"
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


def get_processes() -> list[str]:
    if not is_windows():
        return []
    try:
        result = run_hidden(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5
        )
        names = []
        for row in csv.reader(result.stdout.splitlines()):
            if row:
                names.append(row[0].lower())
        return names
    except Exception:
        return []


def get_running_zalo_processes() -> list[str]:
    """Detect Zalo by process name and by executable location."""
    matched = {
        name
        for name in get_processes()
        if name.startswith("zalo") or name in {"zavimeet.exe", "zalocall.exe", "zalocap.exe"}
    }
    if not is_windows():
        return sorted(matched)

    install_root = str(ZALO_INSTALL_ROOT).lower().rstrip("\\") + "\\"
    updater_root = str(env_path("LOCALAPPDATA") / "zalo-updater").lower().rstrip("\\") + "\\"
    script = (
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "ForEach-Object { if ($_.ExecutablePath) { "
        "[Console]::WriteLine(($_.Name + '|' + $_.ExecutablePath)) } }"
    )
    try:
        result = run_hidden(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
        for line in result.stdout.splitlines():
            name, separator, executable = line.partition("|")
            executable_lower = executable.lower()
            if separator and (
                executable_lower.startswith(install_root)
                or executable_lower.startswith(updater_root)
            ):
                matched.add(name.lower())
    except Exception:
        pass
    return sorted(matched)


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
        running = "Zalo đang chạy — hãy tắt hẳn Zalo trước khi thao tác." if zalo_is_running() else "Zalo process: không thấy đang chạy."
        self.status_var.set(f"{admin}\n{running}\nLưu ý: nên dùng đường dẫn ngắn, không dấu, không ký tự lạ, ví dụ D:\\Zalo.")

    def confirm_preflight(self, destructive: bool = False) -> bool:
        running_processes = get_running_zalo_processes()
        if running_processes:
            names = ", ".join(running_processes)
            messagebox.showwarning(
                APP_NAME,
                "Zalo hoặc thành phần liên quan vẫn đang chạy:\n\n"
                f"{names}\n\n"
                "Hãy thoát Zalo hoàn toàn (kể cả biểu tượng ở khay hệ thống), rồi thử lại.",
            )
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
        if zalo_is_running():
            messagebox.showwarning(APP_NAME, "Hãy tắt hoàn toàn Zalo trước khi dọn phiên bản cũ.")
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
