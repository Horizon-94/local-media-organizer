from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional, Sequence

from .repository import ReadonlyMediaRepository
from .search_jobs import SearchJobManager
from .processing_profile import build_processing_profile, detect_hardware, save_processing_profile


APP_NAME = "本地数据库"
APP_VERSION = "1.1.4-search-progress-warm-cache"

COLORS = {
    "bg": "#F4F6FA",
    "sidebar": "#F8FAFD",
    "card": "#FFFFFF",
    "line": "#DDE3EC",
    "text": "#111827",
    "muted": "#667085",
    "blue": "#1677FF",
    "blue_soft": "#E8F1FF",
    "green": "#18A957",
    "green_soft": "#EAF8F0",
    "orange": "#F79009",
    "orange_soft": "#FFF5E6",
    "red": "#E5484D",
}


def format_bytes(value: Any) -> str:
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return "0 B"


def format_timecode(milliseconds: Any) -> str:
    value = max(0, int(milliseconds or 0))
    hours, rem = divmod(value, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def shorten(text: Any, length: int = 220) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= length else value[: length - 1] + "…"


class ScrollableFrame(tk.Frame):
    def __init__(self, master: tk.Misc, background: str = COLORS["bg"]):
        super().__init__(master, bg=background)
        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=background)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_body(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ThumbnailCache:
    def __init__(self, repository: ReadonlyMediaRepository, cache_root: Path):
        self.repository = repository
        self.cache_root = Path(cache_root).resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.images: list[tk.PhotoImage] = []

    def load(self, derived_id: str, max_pixels: int = 280) -> Optional[tk.PhotoImage]:
        source = self.repository.derived_path(derived_id)
        if not source:
            return None
        digest = hashlib.sha256((str(source) + str(source.stat().st_mtime_ns)).encode("utf-8")).hexdigest()[:20]
        target = self.cache_root / f"{digest}_{max_pixels}.png"
        if not target.is_file():
            result = subprocess.run(
                ["/usr/bin/sips", "-s", "format", "png", "--resampleHeightWidthMax", str(max_pixels), str(source), "--out", str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode != 0 or not target.is_file():
                return None
        try:
            image = tk.PhotoImage(file=str(target))
        except tk.TclError:
            return None
        self.images.append(image)
        return image


class NativeMediaArchiveApp:
    def __init__(
        self,
        root: tk.Tk,
        repository: ReadonlyMediaRepository,
        search_jobs: SearchJobManager,
        output_root: Path,
    ):
        self.root = root
        self.repository = repository
        self.search_jobs = search_jobs
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.thumbnail_cache = ThumbnailCache(repository, self.output_root / "thumbnail_cache")
        self.current_page = "search"
        self.current_search_job: Optional[str] = None
        self.latest_search: Optional[dict[str, Any]] = None
        self.status_var = tk.StringVar(value="中心数据库已连接 · 图片与视频模式")
        self.nav_buttons: dict[str, tk.Button] = {}

        self.root.title(APP_NAME)
        self.root.geometry("1480x920")
        self.root.minsize(1120, 720)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_styles()
        self._build_shell()
        self.switch_page("search")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass
        style.configure("Archive.Horizontal.TProgressbar", troughcolor="#E5EAF1", background=COLORS["blue"], thickness=9)
        style.configure("Archive.Treeview", font=("PingFang SC", 12), rowheight=34, background=COLORS["card"], fieldbackground=COLORS["card"])
        style.configure("Archive.Treeview.Heading", font=("PingFang SC", 11, "bold"))
        style.configure("Archive.TCombobox", padding=6)

    def _build_shell(self) -> None:
        self.sidebar = tk.Frame(self.root, bg=COLORS["sidebar"], width=246, highlightbackground=COLORS["line"], highlightthickness=1)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.content = tk.Frame(self.root, bg=COLORS["bg"])
        self.content.pack(side="left", fill="both", expand=True)

        brand = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        brand.pack(fill="x", padx=24, pady=(34, 28))
        icon = tk.Label(brand, text="⌕", font=("Helvetica Neue", 31, "bold"), fg="white", bg=COLORS["blue"], width=2, height=1)
        icon.pack(side="left")
        brand_text = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_text.pack(side="left", padx=(12, 0))
        tk.Label(brand_text, text=APP_NAME, font=("PingFang SC", 16, "bold"), fg=COLORS["text"], bg=COLORS["sidebar"]).pack(anchor="w")
        tk.Label(brand_text, text="图片与视频素材管理", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["sidebar"]).pack(anchor="w", pady=(3, 0))

        nav = [
            ("new", "＋", "新建任务"),
            ("running", "▷", "运行状态"),
            ("history", "◷", "任务历史"),
            ("search", "⌕", "搜索素材"),
            ("duplicates", "▣", "重复素材"),
            ("special", "◫", "特殊素材"),
            ("settings", "⚙", "设置"),
        ]
        for key, icon_text, title in nav:
            button = tk.Button(
                self.sidebar, text=f"  {icon_text}   {title}", anchor="w",
                font=("PingFang SC", 13), bd=0, relief="flat", cursor="hand2",
                bg=COLORS["sidebar"], fg=COLORS["text"], activebackground=COLORS["blue_soft"],
                command=lambda value=key: self.switch_page(value), padx=17, pady=12,
            )
            button.pack(fill="x", padx=17, pady=2)
            self.nav_buttons[key] = button

        footer = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        footer.pack(side="bottom", fill="x", padx=24, pady=24)
        tk.Frame(footer, bg=COLORS["line"], height=1).pack(fill="x", pady=(0, 14))
        tk.Label(footer, text="●  中心数据库只读连接", font=("PingFang SC", 10), fg=COLORS["green"], bg=COLORS["sidebar"]).pack(anchor="w")
        tk.Label(footer, text=f"版本 {APP_VERSION}", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["sidebar"]).pack(anchor="w", pady=(7, 0))

        status = tk.Label(self.content, textvariable=self.status_var, font=("PingFang SC", 10), fg=COLORS["muted"], bg="#EEF2F7", anchor="w", padx=16, pady=7)
        status.pack(side="bottom", fill="x")
        self.page_host = tk.Frame(self.content, bg=COLORS["bg"])
        self.page_host.pack(fill="both", expand=True)

    def close(self) -> None:
        self.search_jobs.stop_all()
        self.root.destroy()

    def switch_page(self, page: str) -> None:
        self.current_page = page
        for key, button in self.nav_buttons.items():
            selected = key == page
            button.configure(
                bg=COLORS["blue_soft"] if selected else COLORS["sidebar"],
                fg=COLORS["blue"] if selected else COLORS["text"],
                font=("PingFang SC", 13, "bold" if selected else "normal"),
            )
        for child in self.page_host.winfo_children():
            child.destroy()
        renderer = getattr(self, f"page_{page}")
        try:
            renderer()
        except Exception as exc:
            self._error_page(exc)

    def _page(self, title: str, subtitle: str) -> tuple[ScrollableFrame, tk.Frame]:
        scroll = ScrollableFrame(self.page_host)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        header = tk.Frame(body, bg=COLORS["bg"])
        header.pack(fill="x", padx=42, pady=(34, 22))
        tk.Label(header, text=title, font=("PingFang SC", 28, "bold"), fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w")
        tk.Label(header, text=subtitle, font=("PingFang SC", 12), fg=COLORS["muted"], bg=COLORS["bg"]).pack(anchor="w", pady=(7, 0))
        content = tk.Frame(body, bg=COLORS["bg"])
        content.pack(fill="both", expand=True, padx=42, pady=(0, 40))
        return scroll, content

    def _card(self, master: tk.Misc, padding: int = 20) -> tk.Frame:
        frame = tk.Frame(master, bg=COLORS["card"], highlightbackground=COLORS["line"], highlightthickness=1, bd=0)
        frame._archive_padding = padding  # type: ignore[attr-defined]
        return frame

    def _button(self, master: tk.Misc, text: str, command: Callable[[], None], primary: bool = False, danger: bool = False) -> tk.Button:
        bg = COLORS["blue"] if primary else COLORS["card"]
        fg = "white" if primary else (COLORS["red"] if danger else COLORS["text"])
        border = COLORS["blue"] if primary else (COLORS["red"] if danger else COLORS["line"])
        return tk.Button(
            master, text=text, command=command, cursor="hand2", font=("PingFang SC", 11, "bold" if primary else "normal"),
            bg=bg, fg=fg, activebackground=bg, activeforeground=fg, relief="flat", bd=0,
            highlightbackground=border, highlightthickness=1, padx=17, pady=8,
        )

    def _metric(self, master: tk.Misc, label: str, value: str, color: str = COLORS["text"]) -> tk.Frame:
        box = self._card(master)
        tk.Label(box, text=label, font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=16, pady=(14, 5))
        tk.Label(box, text=value, font=("Helvetica Neue", 21, "bold"), fg=color, bg=COLORS["card"]).pack(anchor="w", padx=16, pady=(0, 14))
        return box

    def _error_page(self, exc: Exception) -> None:
        for child in self.page_host.winfo_children():
            child.destroy()
        frame = tk.Frame(self.page_host, bg=COLORS["bg"])
        frame.pack(fill="both", expand=True, padx=60, pady=60)
        tk.Label(frame, text="页面暂时无法读取", font=("PingFang SC", 24, "bold"), fg=COLORS["red"], bg=COLORS["bg"]).pack(anchor="w")
        tk.Label(frame, text=str(exc), wraplength=900, justify="left", font=("Menlo", 11), fg=COLORS["muted"], bg=COLORS["bg"]).pack(anchor="w", pady=16)
        self._button(frame, "重新加载", lambda: self.switch_page(self.current_page), primary=True).pack(anchor="w")

    # ---- Search ---------------------------------------------------------
    def page_search(self) -> None:
        _scroll, content = self._page("搜索素材", "同时搜索全部派生画面、AI 描述、OCR 文字和物体标签。")
        search_card = self._card(content)
        search_card.pack(fill="x", pady=(0, 18))
        search_row = tk.Frame(search_card, bg=COLORS["card"])
        search_row.pack(fill="x", padx=20, pady=(20, 12))
        self.search_query = tk.StringVar()
        entry = ttk.Entry(search_row, textvariable=self.search_query, font=("PingFang SC", 15))
        entry.pack(side="left", fill="x", expand=True, ipady=9)
        entry.bind("<Return>", lambda _event: self.start_search())
        self.search_button = self._button(search_row, "搜索", self.start_search, primary=True)
        self.search_button.pack(side="left", padx=(14, 0))

        filters = tk.Frame(search_card, bg=COLORS["card"])
        filters.pack(fill="x", padx=20, pady=(0, 20))
        tk.Label(filters, text="素材类型", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(side="left")
        self.search_media_type = tk.StringVar(value="全部")
        ttk.Combobox(filters, state="readonly", width=9, textvariable=self.search_media_type, values=("全部", "视频", "图片"), style="Archive.TCombobox").pack(side="left", padx=(8, 22))
        tk.Label(filters, text="预览区间", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(side="left")
        self.search_window = tk.StringVar(value="10 秒")
        ttk.Combobox(filters, state="readonly", width=8, textvariable=self.search_window, values=("5 秒", "10 秒"), style="Archive.TCombobox").pack(side="left", padx=(8, 22))
        tk.Label(filters, text="视频结果自动合并 5 秒内的相邻帧", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(side="left")

        info = tk.Frame(content, bg=COLORS["blue_soft"], highlightbackground="#BBD5FF", highlightthickness=1)
        info.pack(fill="x", pady=(0, 18))
        tk.Label(info, text="ⓘ  当前界面只显示图片和视频；音频、文本接口保留在后台，但不参与本页展示。", font=("PingFang SC", 10), fg="#245AA5", bg=COLORS["blue_soft"], pady=10, padx=14).pack(anchor="w")

        self.search_results_host = tk.Frame(content, bg=COLORS["bg"])
        self.search_results_host.pack(fill="both", expand=True)
        if self.latest_search:
            self.render_search_results(self.latest_search)
        else:
            overview = self.repository.overview()
            title = tk.Label(self.search_results_host, text="数据库已经可以搜索", font=("PingFang SC", 18, "bold"), fg=COLORS["text"], bg=COLORS["bg"])
            title.pack(anchor="w", pady=(8, 12))
            row = tk.Frame(self.search_results_host, bg=COLORS["bg"])
            row.pack(fill="x")
            metrics = [
                ("可搜索画面", f"{overview['visual_unit_total_count']:,}"),
                ("图片素材", f"{overview['source']['image']['count']:,}"),
                ("视频素材", f"{overview['source']['video']['count']:,}"),
                ("文本向量", f"{overview['recognition']['text_vectors']:,}"),
            ]
            for index, (label, value) in enumerate(metrics):
                metric = self._metric(row, label, value, COLORS["blue"] if index == 0 else COLORS["text"])
                metric.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 8, 0))

    def start_search(self) -> None:
        query = self.search_query.get().strip()
        media_map = {"全部": "all", "视频": "video", "图片": "image"}
        request = {
            "query": query,
            "media_type": media_map.get(self.search_media_type.get(), "all"),
            "preview_window_ms": 5000 if self.search_window.get() == "5 秒" else 10000,
            "temporal_dedup_ms": 5000,
            "limit": 30,
            "device": "auto",
        }
        try:
            job = self.search_jobs.start(request)
        except Exception as exc:
            messagebox.showerror("无法开始搜索", str(exc), parent=self.root)
            return
        self.current_search_job = str(job["job_id"])
        self.search_button.configure(state="disabled", text="正在搜索…")
        self.status_var.set("正在全量扫描视觉向量；搜索文字不会写入中心数据库")
        self._show_search_waiting()
        self.root.after(800, self.poll_search)

    def _show_search_waiting(self) -> None:
        for child in self.search_results_host.winfo_children():
            child.destroy()
        card = self._card(self.search_results_host)
        card.pack(fill="x")
        tk.Label(card, text="正在本地搜索", font=("PingFang SC", 18, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w", padx=24, pady=(24, 7))
        tk.Label(card, text="正在比较全部视觉向量、文本描述与 YOLOE 物体标签，一般约需二十秒。", font=("PingFang SC", 11), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=24)
        progress = ttk.Progressbar(card, mode="indeterminate", style="Archive.Horizontal.TProgressbar")
        progress.pack(fill="x", padx=24, pady=24)
        progress.start(12)

    def poll_search(self) -> None:
        if not self.current_search_job:
            return
        state = self.search_jobs.get(self.current_search_job)
        if not state:
            return
        if state["status"] in {"queued", "running"}:
            self.status_var.set(f"正在全量搜索 · 已用 {int(state.get('elapsed_seconds', 0))} 秒 · 完成后自动显示结果")
            self.root.after(800, self.poll_search)
            return
        self.search_button.configure(state="normal", text="搜索")
        if state["status"] == "success":
            result = self.search_jobs.results(self.current_search_job)
            if result:
                self.latest_search = result
                self.status_var.set("搜索完成 · 查询原文与查询向量均未写入数据库")
                self.render_search_results(result)
                return
        self.status_var.set("搜索失败；中心数据库未被修改")
        messagebox.showerror("搜索失败", state.get("error") or "没有生成有效结果", parent=self.root)

    def render_search_results(self, payload: dict[str, Any]) -> None:
        if not hasattr(self, "search_results_host"):
            return
        for child in self.search_results_host.winfo_children():
            child.destroy()
        results = payload.get("results", [])
        coverage = payload.get("coverage", {})
        heading = tk.Frame(self.search_results_host, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(4, 10))
        tk.Label(heading, text=f"搜索结果（{len(results)} 条）", font=("PingFang SC", 16, "bold"), fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(
            heading,
            text=f"扫描视觉向量 {int(coverage.get('scanned_visual_vector_count', 0)):,} · 文本向量 {int(coverage.get('scanned_text_vector_count', 0)):,}",
            font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["bg"],
        ).pack(side="right")
        for index, row in enumerate(results, 1):
            self._search_result_card(self.search_results_host, index, row)

    def _search_result_card(self, master: tk.Misc, index: int, row: dict[str, Any]) -> None:
        card = self._card(master)
        card.pack(fill="x", pady=6)
        left = tk.Frame(card, bg="#E9EEF5", width=255, height=170)
        left.pack(side="left", padx=14, pady=14)
        left.pack_propagate(False)
        image = self.thumbnail_cache.load(str(row.get("derived_id", "")), 320)
        if image:
            tk.Label(left, image=image, bg="#E9EEF5").pack(fill="both", expand=True)
        else:
            tk.Label(left, text="派生预览暂不可用", font=("PingFang SC", 10), fg=COLORS["muted"], bg="#E9EEF5").pack(expand=True)
        right = tk.Frame(card, bg=COLORS["card"])
        right.pack(side="left", fill="both", expand=True, padx=(4, 18), pady=14)
        title_row = tk.Frame(right, bg=COLORS["card"])
        title_row.pack(fill="x")
        media_icon = "▸" if row.get("media_type") == "video" else "▧"
        tk.Label(title_row, text=f"{media_icon}  {Path(str(row.get('source_relative_path', ''))).name}", font=("PingFang SC", 14, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(side="left", anchor="w")
        tk.Label(title_row, text=f"#{index}  相关度 {float(row.get('hybrid_score') or 0):.4f}", font=("Helvetica Neue", 10), fg=COLORS["blue"], bg=COLORS["blue_soft"], padx=8, pady=4).pack(side="right")
        if row.get("media_type") == "video":
            interval = f"命中区间：{row.get('preview_segment_start_timecode')} – {row.get('preview_segment_end_timecode')}   命中点：{row.get('timecode')}"
        else:
            interval = "图片素材"
        tk.Label(right, text=interval, font=("PingFang SC", 11), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", pady=(8, 4))
        environment = str(row.get("environment_label") or "环境未标注")
        if row.get("environment_user_confirmation_required"):
            environment += " · 用户可确认"
        tk.Label(right, text=f"场景：{environment}", font=("PingFang SC", 10), fg="#865B00", bg=COLORS["orange_soft"], padx=7, pady=3).pack(anchor="w", pady=(0, 7))
        tk.Label(right, text=shorten(row.get("text_preview") or "该画面通过全视觉通道召回，暂无详细文本描述。", 260), wraplength=760, justify="left", font=("PingFang SC", 10), fg="#344054", bg=COLORS["card"]).pack(anchor="w")
        labels = ", ".join(str(item.get("label_zh") or item.get("label")) for item in row.get("yoloe_labels", [])[:8])
        if labels:
            tk.Label(right, text=f"画面物体：{labels}", font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", pady=(6, 0))
        buttons = tk.Frame(right, bg=COLORS["card"])
        buttons.pack(anchor="w", pady=(11, 0))
        if row.get("media_type") == "video":
            self._button(buttons, "▶ 打开命中片段", lambda item=row: self.open_media(item), primary=True).pack(side="left")
        self._button(buttons, "在 Finder 中显示", lambda item=row: self.reveal_media(item)).pack(side="left", padx=(8, 0))
        relative_path = str(row.get("source_relative_path", ""))
        tk.Label(right, text=f"所属文件夹：{Path(relative_path).parent}", font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", pady=(8, 0))
        tk.Label(right, text=f"完整相对路径：{relative_path}", font=("PingFang SC", 9), fg="#98A2B3", bg=COLORS["card"]).pack(anchor="w", pady=(2, 0))

    def open_media(self, row: dict[str, Any]) -> None:
        item = self.repository.source_media(str(row.get("source_content_id", "")))
        if not item or not item.get("available"):
            messagebox.showwarning("原视频不可用", "数据库中有记录，但当前磁盘未挂载或文件不存在。", parent=self.root)
            return
        start = format_timecode(row.get("preview_segment_start_ms"))
        end = format_timecode(row.get("preview_segment_end_ms"))
        try:
            subprocess.Popen(["/usr/bin/open", str(item["resolved_path"])])
        except OSError as error:
            messagebox.showerror("无法打开视频", str(error), parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(start)
        self.status_var.set(f"已打开视频 · 目标片段 {start} – {end} · 起点已复制")

    def reveal_media(self, row: dict[str, Any]) -> None:
        item = self.repository.source_media(str(row.get("source_content_id", "")))
        if not item or not item.get("available"):
            messagebox.showwarning("素材不可用", "数据库中有记录，但当前磁盘未挂载或文件不存在。", parent=self.root)
            return
        subprocess.Popen(["/usr/bin/open", "-R", str(item["resolved_path"])])

    # ---- Pipeline and reports ------------------------------------------
    def page_running(self) -> None:
        _scroll, content = self._page("运行状态", "所有数字直接来自中心数据库；没有运行中的任务时显示最近一次完整状态。")
        pipeline = self.repository.pipeline()
        overview = self.repository.overview()
        active_runs = self.repository.active_runs()
        if active_runs:
            for run in active_runs:
                live = self._card(content)
                live.pack(fill="x", pady=(0, 12))
                tk.Label(live, text=f"正在进行：{run['stage']}", font=("PingFang SC", 16, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w", padx=22, pady=(18, 8))
                ttk.Progressbar(live, maximum=100, value=run["percent"], style="Archive.Horizontal.TProgressbar").pack(fill="x", padx=22)
                eta = "正在计算" if run["eta_seconds"] is None else self._duration_label(run["eta_seconds"])
                tk.Label(live, text=f"已完成 {run['completed']:,} / {run['total']:,} · 剩余 {run['remaining']:,} · 预计还需 {eta}", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=22, pady=(8, 18))
        summary = self._card(content)
        summary.pack(fill="x", pady=(0, 16))
        top = tk.Frame(summary, bg=COLORS["card"])
        top.pack(fill="x", padx=22, pady=(20, 9))
        tk.Label(top, text="图片与视频主线", font=("PingFang SC", 17, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(side="left")
        ready_text = "搜索已开放" if pipeline["search_ready"] else "仍需检查"
        tk.Label(top, text=ready_text, font=("PingFang SC", 10, "bold"), fg=COLORS["green"] if pipeline["search_ready"] else COLORS["orange"], bg=COLORS["green_soft"] if pipeline["search_ready"] else COLORS["orange_soft"], padx=10, pady=4).pack(side="right")
        ttk.Progressbar(summary, maximum=100, value=pipeline["overall_percent"], style="Archive.Horizontal.TProgressbar").pack(fill="x", padx=22, pady=(0, 8))
        tk.Label(summary, text=f"总体完成度 {pipeline['overall_percent']:.1f}% · 图片 {overview['source']['image']['count']:,} · 视频 {overview['source']['video']['count']:,} · 处理错误记录 {pipeline['failed_record_count']:,}", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=22, pady=(0, 20))

        stage_title = tk.Label(content, text="阶段进度", font=("PingFang SC", 16, "bold"), fg=COLORS["text"], bg=COLORS["bg"])
        stage_title.pack(anchor="w", pady=(8, 8))
        for index, stage in enumerate(pipeline["stages"], 1):
            card = self._card(content)
            card.pack(fill="x", pady=5)
            status_color = COLORS["green"] if stage["status"] == "success" else COLORS["orange"]
            tk.Label(card, text="✓" if stage["status"] == "success" else str(index), font=("Helvetica Neue", 12, "bold"), fg="white", bg=status_color, width=2).pack(side="left", padx=(16, 12), pady=15)
            detail = tk.Frame(card, bg=COLORS["card"])
            detail.pack(side="left", fill="x", expand=True, pady=10)
            tk.Label(detail, text=stage["name"], font=("PingFang SC", 12, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w")
            tk.Label(detail, text=stage["description"], font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", pady=(2, 0))
            tk.Label(card, text=f"{stage['done']:,} / {stage['total']:,}" if stage["total"] else f"{stage['done']:,}", font=("Helvetica Neue", 11, "bold"), fg=status_color, bg=COLORS["card"]).pack(side="right", padx=18)

        warning = tk.Frame(content, bg=COLORS["orange_soft"], highlightbackground="#F7C97A", highlightthickness=1)
        warning.pack(fill="x", pady=(16, 0))
        tk.Label(warning, text="总编排入口尚未冻结：本页已经接入所有真实阶段状态，但不会从界面擅自拼接全量模型命令。", font=("PingFang SC", 10), fg="#7A4B00", bg=COLORS["orange_soft"], padx=14, pady=11).pack(anchor="w")

    @staticmethod
    def _duration_label(seconds: Any) -> str:
        value = max(0, int(float(seconds or 0)))
        hours, rem = divmod(value, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

    def page_history(self) -> None:
        _scroll, content = self._page("任务历史", "查看扫描、派生、视觉分析、OCR、Qwen-VL 和文本向量的真实运行记录。")
        rows = self.repository.recent_runs(80)
        card = self._card(content)
        card.pack(fill="both", expand=True)
        columns = ("stage", "status", "input", "output", "started", "finished")
        tree = ttk.Treeview(card, columns=columns, show="headings", style="Archive.Treeview", height=18)
        labels = {"stage": "阶段", "status": "状态", "input": "输入", "output": "完成", "started": "开始", "finished": "结束"}
        widths = {"stage": 250, "status": 110, "input": 80, "output": 80, "started": 180, "finished": 180}
        for column in columns:
            tree.heading(column, text=labels[column])
            tree.column(column, width=widths[column], anchor="w")
        for row in rows:
            tree.insert("", "end", values=(row.get("stage"), row.get("status"), row.get("input_count", 0), row.get("output_count", 0), row.get("started_at") or "—", row.get("finished_at") or "—"))
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=14)
        scrollbar.pack(side="right", fill="y", padx=(0, 14), pady=14)

    def page_duplicates(self) -> None:
        _scroll, content = self._page("重复素材", "这里只做标记和建议，不会自动删除、移动或修改任何原始文件。")
        payload = self.repository.duplicate_groups(limit=60)
        tk.Label(content, text=f"找到 {payload['total']:,} 个完全重复组", font=("PingFang SC", 15, "bold"), fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w", pady=(0, 10))
        for row in payload["items"]:
            card = self._card(content)
            card.pack(fill="x", pady=5)
            badge = tk.Label(card, text="完全相同", font=("PingFang SC", 9, "bold"), fg=COLORS["blue"], bg=COLORS["blue_soft"], padx=8, pady=4)
            badge.pack(side="left", padx=16, pady=16)
            detail = tk.Frame(card, bg=COLORS["card"])
            detail.pack(side="left", fill="x", expand=True, pady=11)
            tk.Label(detail, text=str(row.get("file_name") or row.get("duplicate_group_id")), font=("PingFang SC", 12, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w")
            tk.Label(detail, text=str(row.get("relative_path") or "路径待确认"), font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", pady=(3, 0))
            tk.Label(card, text=f"{int(row.get('member_count') or 0)} 个文件\n总计 {format_bytes(row.get('total_bytes'))}", justify="right", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(side="right", padx=18)

    def page_special(self) -> None:
        _scroll, content = self._page("特殊素材", "延时摄影按组展示代表帧；连拍与其他类型接口保留，当前没有结果时不显示。")
        payload = self.repository.timelapse_groups(limit=50)
        tk.Label(content, text=f"延时摄影组 {payload['total']:,}", font=("PingFang SC", 15, "bold"), fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w", pady=(0, 10))
        for group in payload["items"]:
            card = self._card(content)
            card.pack(fill="x", pady=7)
            header = tk.Frame(card, bg=COLORS["card"])
            header.pack(fill="x", padx=18, pady=(15, 10))
            tk.Label(header, text=f"延时摄影组 {group['sequence_id']}", font=("PingFang SC", 14, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(side="left")
            tk.Label(header, text=f"{group['keyframe_count']} 张代表帧", font=("PingFang SC", 9), fg=COLORS["blue"], bg=COLORS["blue_soft"], padx=8, pady=3).pack(side="right")
            frames = tk.Frame(card, bg=COLORS["card"])
            frames.pack(fill="x", padx=18, pady=(0, 16))
            for frame in group["frames"]:
                box = tk.Frame(frames, bg="#EEF2F7", width=230, height=155)
                box.pack(side="left", padx=(0, 10))
                box.pack_propagate(False)
                image = self.thumbnail_cache.load(str(frame.get("derived_id") or ""), 260)
                if image:
                    tk.Label(box, image=image, bg="#EEF2F7").pack(fill="both", expand=True)
                else:
                    tk.Label(box, text="代表帧", bg="#EEF2F7", fg=COLORS["muted"]).pack(expand=True)
                tk.Label(box, text={"first": "起始帧", "middle": "中间帧", "last": "结束帧"}.get(str(frame.get("representative_position")), "代表帧"), font=("PingFang SC", 9, "bold"), fg="white", bg="#111827", padx=7, pady=3).place(x=7, y=7)
            tk.Label(card, text=str(group.get("first_path") or ""), font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=18, pady=(0, 14))

    # ---- New task and settings -----------------------------------------
    def page_new(self) -> None:
        _scroll, content = self._page("新建整理任务", "选择图片/视频素材文件夹；原始素材始终只读，所有派生产物写入独立工作区。")
        card = self._card(content)
        card.pack(fill="x")
        self.task_source = tk.StringVar()
        self.task_name = tk.StringVar(value=time.strftime("素材整理_%Y%m%d"))
        self.task_mode = tk.StringVar(value="第一次完整整理")
        fields = tk.Frame(card, bg=COLORS["card"])
        fields.pack(fill="x", padx=26, pady=24)
        self._form_directory(fields, "1", "素材文件夹", self.task_source)
        self._form_entry(fields, "2", "任务名称", self.task_name)
        row = tk.Frame(fields, bg=COLORS["card"])
        row.pack(fill="x", pady=10)
        tk.Label(row, text="3", font=("Helvetica Neue", 11, "bold"), fg="white", bg=COLORS["blue"], width=2).pack(side="left", padx=(0, 12))
        tk.Label(row, text="整理模式", font=("PingFang SC", 12, "bold"), fg=COLORS["text"], bg=COLORS["card"], width=12, anchor="w").pack(side="left")
        ttk.Combobox(row, state="readonly", textvariable=self.task_mode, values=("第一次完整整理", "增量整理", "修复缺失内容", "重建搜索入口"), width=24).pack(side="left", ipady=5)
        note = tk.Frame(content, bg=COLORS["orange_soft"], highlightbackground="#F7C97A", highlightthickness=1)
        note.pack(fill="x", pady=16)
        tk.Label(note, text="当前版本会完成路径检查并保存通用任务配置，但不会启动尚未冻结的全链路总编排器。已经冻结的搜索功能可以直接使用。", wraplength=900, justify="left", font=("PingFang SC", 10), fg="#7A4B00", bg=COLORS["orange_soft"], padx=16, pady=13).pack(anchor="w")
        self._button(content, "检查并保存任务配置", self.save_task_draft, primary=True).pack(anchor="center", pady=10)

    def _form_directory(self, master: tk.Misc, number: str, title: str, variable: tk.StringVar) -> None:
        row = tk.Frame(master, bg=COLORS["card"])
        row.pack(fill="x", pady=10)
        tk.Label(row, text=number, font=("Helvetica Neue", 11, "bold"), fg="white", bg=COLORS["blue"], width=2).pack(side="left", padx=(0, 12))
        tk.Label(row, text=title, font=("PingFang SC", 12, "bold"), fg=COLORS["text"], bg=COLORS["card"], width=12, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=variable, font=("PingFang SC", 11)).pack(side="left", fill="x", expand=True, ipady=7)
        self._button(row, "浏览…", lambda: self.choose_directory(variable)).pack(side="left", padx=(10, 0))

    def _form_entry(self, master: tk.Misc, number: str, title: str, variable: tk.StringVar) -> None:
        row = tk.Frame(master, bg=COLORS["card"])
        row.pack(fill="x", pady=10)
        tk.Label(row, text=number, font=("Helvetica Neue", 11, "bold"), fg="white", bg=COLORS["blue"], width=2).pack(side="left", padx=(0, 12))
        tk.Label(row, text=title, font=("PingFang SC", 12, "bold"), fg=COLORS["text"], bg=COLORS["card"], width=12, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=variable, font=("PingFang SC", 11)).pack(side="left", fill="x", expand=True, ipady=7)

    def choose_directory(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(parent=self.root, mustexist=True)
        if selected:
            variable.set(selected)

    def save_task_draft(self) -> None:
        source = Path(self.task_source.get()).expanduser()
        name = " ".join(self.task_name.get().split())
        if not source.is_dir():
            messagebox.showerror("素材文件夹不可用", "请选择一个当前可以读取的文件夹。", parent=self.root)
            return
        if not name:
            messagebox.showerror("任务名称为空", "请填写任务名称。", parent=self.root)
            return
        task_id = "task_" + hashlib.sha256((str(source.resolve()) + name + str(time.time_ns())).encode("utf-8")).hexdigest()[:20]
        task_dir = self.output_root / "task_drafts" / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        payload = {
            "task_contract": "media_archive_image_video_task_draft_v1",
            "task_id": task_id,
            "name": name,
            "source_root": str(source.resolve()),
            "source_access": "read_only",
            "visible_media_types": ["image", "video"],
            "hidden_media_interfaces": ["audio", "text"],
            "mode": self.task_mode.get(),
            "workspace": str(task_dir / "workspace"),
            "status": "AWAITING_FROZEN_PIPELINE_ORCHESTRATOR",
            "central_database_write": False,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (task_dir / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.status_var.set(f"任务配置已保存：{task_id} · 尚未启动模型")
        messagebox.showinfo("任务配置已保存", "路径和只读边界检查通过。总编排入口冻结后，可直接接入这份任务配置。", parent=self.root)

    def page_settings(self) -> None:
        _scroll, content = self._page("处理设置", "先看电脑能力，再决定并发、抽帧密度和高价值分析范围。")
        overview = self.repository.overview()
        integrity = self.repository.integrity()
        readiness = self.search_jobs.readiness()
        hardware = detect_hardware()
        recommendation = hardware["recommendation"]
        hardware_row = tk.Frame(content, bg=COLORS["bg"])
        hardware_row.pack(fill="x", pady=(0, 16))
        hardware_metrics = [
            ("芯片", hardware["chip"]),
            ("CPU", f"{hardware['cpu_cores_total']} 核"),
            ("GPU", f"{hardware['gpu_cores']} 核" if hardware.get("gpu_cores") else "系统未公开"),
            ("统一内存", f"{hardware['unified_memory_gb']:g} GB" if hardware.get("unified_memory_gb") else "系统未公开"),
        ]
        for index, (label, value) in enumerate(hardware_metrics):
            self._metric(hardware_row, label, str(value), COLORS["blue"] if index == 0 else COLORS["text"]).pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 8, 0))

        profile_card = self._card(content)
        profile_card.pack(fill="x", pady=(0, 16))
        tk.Label(profile_card, text="新任务处理方案", font=("PingFang SC", 16, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w", padx=22, pady=(19, 5))
        tk.Label(profile_card, text="默认值按本机能力保守推荐。用户可以降低或提高，但运行时遇到内存压力只会自动降低并发。", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=22, pady=(0, 13))
        form = tk.Frame(profile_card, bg=COLORS["card"])
        form.pack(fill="x", padx=22, pady=(0, 20))
        self.profile_scheduler = tk.StringVar(value="自动选择（推荐）")
        self.profile_model_workers = tk.IntVar(value=recommendation["model_workers"])
        self.profile_frame_workers = tk.IntVar(value=recommendation["frame_extract_workers"])
        self.profile_interval = tk.StringVar(value="3 秒")
        self.profile_high_value = tk.StringVar(value="目标 15%")
        self.profile_image_scope = tk.StringVar(value="按当前规则筛选图片")
        self._settings_combo(form, "运行方式", self.profile_scheduler, ("自动选择（推荐）", "数据库流水线异步（尚未开放）", "按阶段串行"))
        self._settings_spin(form, "模型并发路数", self.profile_model_workers, 1, 8, f"推荐 {recommendation['model_workers']} 路")
        self._settings_spin(form, "抽帧并发路数", self.profile_frame_workers, 1, 16, f"推荐 {recommendation['frame_extract_workers']} 路")
        self._settings_combo(form, "视频抽帧间隔", self.profile_interval, ("1 秒", "2 秒", "3 秒", "4 秒", "5 秒"))
        self._settings_combo(form, "高价值分析密度", self.profile_high_value, ("兼容当前规则", "目标 15%", "目标 20%", "目标 30%"))
        self._settings_combo(form, "图片分析范围", self.profile_image_scope, ("按当前规则筛选图片", "所有普通图片都进入画面描述"))
        self._button(profile_card, "保存为今后任务的默认方案", self.save_profile_from_settings, primary=True).pack(anchor="e", padx=22, pady=(0, 20))

        boundary = tk.Frame(content, bg=COLORS["orange_soft"], highlightbackground="#F7C97A", highlightthickness=1)
        boundary.pack(fill="x", pady=(0, 16))
        tk.Label(boundary, text="设置不会改写当前 V25：改变 3 秒抽帧或选择 15%/20%/30% 时，方案会标记为“需要新规则版本”，等待通用异步总编排器接入。", wraplength=1000, justify="left", font=("PingFang SC", 10), fg="#7A4B00", bg=COLORS["orange_soft"], padx=15, pady=12).pack(anchor="w")

        tk.Label(content, text="系统与安全", font=("PingFang SC", 16, "bold"), fg=COLORS["text"], bg=COLORS["bg"]).pack(anchor="w", pady=(4, 7))
        cards = [
            ("中心数据库", str(self.repository.db_path), integrity["integrity_check"] == "ok" and integrity["foreign_key_error_count"] == 0),
            ("全视觉搜索", "冻结入口 Stop03-5E V2", bool(readiness["ready"])),
            ("原始素材保护", "只读；仅在用户点击播放或 Finder 时访问", True),
            ("当前显示范围", "图片、视频（音频和文本接口隐藏保留）", True),
            ("应用输出目录", str(self.output_root), True),
        ]
        for title, value, passed in cards:
            card = self._card(content)
            card.pack(fill="x", pady=5)
            tk.Label(card, text="✓" if passed else "!", font=("Helvetica Neue", 13, "bold"), fg="white", bg=COLORS["green"] if passed else COLORS["orange"], width=2).pack(side="left", padx=16, pady=14)
            detail = tk.Frame(card, bg=COLORS["card"])
            detail.pack(side="left", fill="x", expand=True, pady=10)
            tk.Label(detail, text=title, font=("PingFang SC", 12, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w")
            tk.Label(detail, text=value, wraplength=900, justify="left", font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", pady=(2, 0))
        storage = overview["storage"]
        tk.Label(content, text=f"数据库所在磁盘可用空间：{format_bytes(storage['free'])} / {format_bytes(storage['total'])}", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["bg"]).pack(anchor="w", pady=(15, 0))

        model_card = self._card(content)
        model_card.pack(fill="x", pady=(18, 0))
        tk.Label(model_card, text="模型更新必须经过 5 道检查", font=("PingFang SC", 14, "bold"), fg=COLORS["text"], bg=COLORS["card"]).pack(anchor="w", padx=20, pady=(17, 7))
        tk.Label(model_card, text="登记模型指纹  →  离线小样本测试  →  与当前模型对比  →  人工确认  →  明确启用", font=("PingFang SC", 10), fg=COLORS["muted"], bg=COLORS["card"]).pack(anchor="w", padx=20, pady=(0, 17))

    def _settings_combo(self, master: tk.Misc, label: str, variable: tk.StringVar, values: tuple[str, ...]) -> None:
        row = tk.Frame(master, bg=COLORS["card"])
        row.pack(fill="x", pady=5)
        tk.Label(row, text=label, width=18, anchor="w", font=("PingFang SC", 11), fg=COLORS["text"], bg=COLORS["card"]).pack(side="left")
        ttk.Combobox(row, state="readonly", textvariable=variable, values=values, width=28).pack(side="left", ipady=4)

    def _settings_spin(self, master: tk.Misc, label: str, variable: tk.IntVar, minimum: int, maximum: int, hint: str) -> None:
        row = tk.Frame(master, bg=COLORS["card"])
        row.pack(fill="x", pady=5)
        tk.Label(row, text=label, width=18, anchor="w", font=("PingFang SC", 11), fg=COLORS["text"], bg=COLORS["card"]).pack(side="left")
        tk.Spinbox(row, from_=minimum, to=maximum, textvariable=variable, width=8, font=("Helvetica Neue", 11)).pack(side="left")
        tk.Label(row, text=hint, font=("PingFang SC", 9), fg=COLORS["muted"], bg=COLORS["card"]).pack(side="left", padx=10)

    def save_profile_from_settings(self) -> None:
        scheduler_map = {"自动选择（推荐）": "auto", "数据库流水线异步（尚未开放）": "pipeline_async", "按阶段串行": "stage_serial"}
        high_value_map = {"兼容当前规则": "frozen_v25_compatible", "目标 15%": "target_15", "目标 20%": "target_20", "目标 30%": "target_30"}
        image_map = {"按当前规则筛选图片": "frozen_current_policy", "所有普通图片都进入画面描述": "all_images"}
        interval_match = re.search(r"\d+", self.profile_interval.get())
        try:
            profile = build_processing_profile(
                detect_hardware(),
                scheduler_mode=scheduler_map[self.profile_scheduler.get()],
                model_workers=self.profile_model_workers.get(),
                frame_extract_workers=self.profile_frame_workers.get(),
                video_frame_interval_seconds=float(interval_match.group()) if interval_match else 3.0,
                high_value_mode=high_value_map[self.profile_high_value.get()],
                image_scope=image_map[self.profile_image_scope.get()],
            )
            path = save_processing_profile(self.output_root, profile)
        except (KeyError, ValueError, OSError) as exc:
            messagebox.showerror("无法保存处理方案", str(exc), parent=self.root)
            return
        self.status_var.set(f"处理方案已保存并会用于下一次新任务：{profile['profile_id']}")
        messagebox.showinfo("处理方案已保存", f"已保存到：\n{path}\n\n下一次新建任务会把这份通用配置写入任务快照。", parent=self.root)


def default_paths(project_root: Path) -> dict[str, Path]:
    ai_local = project_root.parent
    environment_root = Path(
        os.environ.get("MEDIA_ARCHIVE_ENV_ROOT", str(ai_local / "envs"))
    ).expanduser().absolute()
    return {
        "db": project_root / "media_archive.sqlite",
        "out": ai_local / "test-output" / "media_archive_image_video_app_v1",
        "embedding_python": environment_root / "media-archive-embedding" / "bin" / "python",
        "openclip_python": environment_root / "media-archive-v06-visual" / "bin" / "python",
        "search_script": project_root / "scripts" / "04_media_archive_app" / "stop03_5e_hybrid_search_app_adapter_v1.py",
        "search_config": project_root / "configs" / "stop03_5e_hybrid_visual_text_search_v2.json",
    }


def build_parser(project_root: Path) -> argparse.ArgumentParser:
    defaults = default_paths(project_root)
    parser = argparse.ArgumentParser(description="AI local media archive native image/video app")
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument("--db", type=Path, default=defaults["db"])
    parser.add_argument("--out", type=Path, default=defaults["out"])
    parser.add_argument("--embedding-python", type=Path, default=defaults["embedding_python"])
    parser.add_argument("--openclip-python", type=Path, default=defaults["openclip_python"])
    parser.add_argument("--search-script", type=Path, default=defaults["search_script"])
    parser.add_argument("--search-config", type=Path, default=defaults["search_config"])
    parser.add_argument("--check", action="store_true", help="read-only backend check without opening a window")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    inferred_root = Path(__file__).resolve().parents[2]
    args = build_parser(inferred_root).parse_args(argv)
    repository = ReadonlyMediaRepository(args.db)
    manager = SearchJobManager(
        db_path=args.db,
        output_root=args.out,
        search_script=args.search_script,
        search_config=args.search_config,
        embedding_python=args.embedding_python,
        openclip_python=args.openclip_python,
    )
    if args.check:
        report = {
            "status": "PASS" if manager.readiness()["ready"] else "BLOCKED",
            "app": APP_NAME,
            "version": APP_VERSION,
            "ui_kind": "native_tk_python",
            "web_server_used": False,
            "overview": repository.overview(),
            "pipeline": repository.pipeline(),
            "database": repository.integrity(),
            "search_runtime": manager.readiness(),
            "central_database_write": False,
            "model_run": False,
            "original_media_read": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 2
    root = tk.Tk()
    NativeMediaArchiveApp(root, repository, manager, args.out)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
