"""Load only the current photo's thumbnail, with a bounded in-memory cache."""
from __future__ import annotations

import tkinter as tk
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from src.preview import PILLOW_AVAILABLE, is_supported, load_color_preview
from src.ui_theme import TEXT_SUB, WIN_BG


def _load_thumbnail(info, size=(310, 108)):
    image = load_color_preview(info, draft_size=(max(560, size[0]), max(560, size[1])))
    image.thumbnail(size)
    return image


class ThumbnailMixin:
    def _init_thumbnail(self, size=(298, 202)) -> None:
        self._thumbnail_size = size
        self._thumbnail_pool = ThreadPoolExecutor(max_workers=1)
        self._thumbnail_cache = OrderedDict()
        self._thumbnail_key = None
        self._thumbnail_pending = None
        self._thumbnail_work = None
        self._thumbnail_poll = None
        self._thumbnail_image = None
        self._thumbnail_source = None
        self._thumbnail_fit = None
        self.preview_canvas.bind("<Configure>", self._center_thumbnail)
        self.preview_canvas.bind("<Destroy>", self._stop_thumbnail)

    def _center_thumbnail(self, event=None) -> None:
        canvas = self.preview_canvas
        width = event.width if event else canvas.winfo_width()
        height = event.height if event else canvas.winfo_height()
        size = (
            max(1, width - 2) if width > 1 else self._thumbnail_size[0],
            max(1, height - 2) if height > 1 else self._thumbnail_size[1],
        )
        if self._thumbnail_source is not None and self._thumbnail_fit != size:
            from PIL import ImageTk

            scaled = self._thumbnail_source.copy()
            scaled.thumbnail(size)
            self._thumbnail_image = ImageTk.PhotoImage(scaled, master=canvas)
            canvas.itemconfigure("thumbnail", image=self._thumbnail_image)
            self._thumbnail_fit = size
        canvas.coords("thumbnail", width // 2, height // 2)

    def _show_thumbnail(self, image=None, *, message="") -> None:
        canvas = self.preview_canvas
        canvas.delete("all")
        self._thumbnail_image = None
        self._thumbnail_source = image
        self._thumbnail_fit = None
        if image is None:
            canvas.create_text(0, 0, text=message, fill=TEXT_SUB, tags=("thumbnail",))
        else:
            canvas.create_image(0, 0, tags=("thumbnail",))
        self._center_thumbnail()

    def _draw_preview(self, info) -> None:
        self._thumbnail_pending = None
        if info is None or not PILLOW_AVAILABLE or not is_supported(info.category):
            self._thumbnail_key = None
            self._show_thumbnail(message="未选择照片" if info is None else "暂无预览")
            return
        key = (info.path, info.size_bytes, info.file_mtime_ns, info.modified_at)
        if key == self._thumbnail_key:
            return
        self._thumbnail_key = key
        if key in self._thumbnail_cache:
            self._thumbnail_cache.move_to_end(key)
            self._show_thumbnail(self._thumbnail_cache[key])
        else:
            self._show_thumbnail(message="正在加载预览…")
            self._thumbnail_pending = key, info
            self._submit_thumbnail()

    def _submit_thumbnail(self) -> None:
        if self._thumbnail_work is not None or self._thumbnail_pending is None:
            return
        key, info = self._thumbnail_pending
        self._thumbnail_pending = None
        self._thumbnail_work = key, self._thumbnail_pool.submit(_load_thumbnail, info, self._thumbnail_size)
        self._thumbnail_poll = self.preview_canvas.after(25, self._poll_thumbnail)

    def _poll_thumbnail(self) -> None:
        self._thumbnail_poll = None
        key, future = self._thumbnail_work
        if not future.done():
            self._thumbnail_poll = self.preview_canvas.after(25, self._poll_thumbnail)
            return
        self._thumbnail_work = None
        try:
            image = future.result()
        except Exception:
            if key == self._thumbnail_key:
                self._show_thumbnail(message="无法读取预览")
        else:
            self._thumbnail_cache[key] = image
            if len(self._thumbnail_cache) > (12 if self._thumbnail_size[0] > 600 else 48):
                self._thumbnail_cache.popitem(last=False)
            if key == self._thumbnail_key:
                self._show_thumbnail(image)
        self._submit_thumbnail()

    def _stop_thumbnail(self, _event=None) -> None:
        if self._thumbnail_poll is not None:
            self.preview_canvas.after_cancel(self._thumbnail_poll)
            self._thumbnail_poll = None
        self._thumbnail_pending = None
        self._thumbnail_pool.shutdown(wait=False, cancel_futures=True)


class PreviewCanvas(tk.Canvas, ThumbnailMixin):
    """An independent preview for the large-photo window."""

    def __init__(self, parent, size):
        super().__init__(parent, bg=WIN_BG, width=size[0], height=size[1], highlightthickness=0)
        self.preview_canvas = self
        self._init_thumbnail(size)

    def show_photo(self, info) -> None:
        self._draw_preview(info)
