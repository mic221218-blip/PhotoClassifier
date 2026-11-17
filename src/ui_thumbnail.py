"""Load only the current photo's thumbnail, with a bounded in-memory cache."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from src.preview import PILLOW_AVAILABLE, is_supported, load_color_preview
from src.ui_theme import TEXT_SUB


def _load_thumbnail(info):
    image = load_color_preview(info)
    image.thumbnail((310, 108))
    return image


class ThumbnailMixin:
    def _init_thumbnail(self) -> None:
        self._thumbnail_pool = ThreadPoolExecutor(max_workers=1)
        self._thumbnail_cache = OrderedDict()
        self._thumbnail_key = None
        self._thumbnail_pending = None
        self._thumbnail_work = None
        self._thumbnail_poll = None
        self._thumbnail_image = None
        self.preview_canvas.bind("<Configure>", self._center_thumbnail)
        self.preview_canvas.bind("<Destroy>", self._stop_thumbnail)

    def _center_thumbnail(self, event=None) -> None:
        canvas = self.preview_canvas
        x = (event.width if event else canvas.winfo_width()) // 2
        y = int(canvas["height"]) // 2
        canvas.coords("thumbnail", x, y)

    def _show_thumbnail(self, image=None, *, message="") -> None:
        canvas = self.preview_canvas
        canvas.delete("all")
        self._thumbnail_image = None
        if image is None:
            canvas.create_text(0, 0, text=message, fill=TEXT_SUB, tags=("thumbnail",))
        else:
            from PIL import ImageTk

            self._thumbnail_image = ImageTk.PhotoImage(image, master=canvas)
            canvas.create_image(0, 0, image=self._thumbnail_image, tags=("thumbnail",))
        self._center_thumbnail()

    def _draw_preview(self, info) -> None:
        self._thumbnail_pending = None
        if info is None or not PILLOW_AVAILABLE or not is_supported(info.category):
            self._thumbnail_key = None
            self._show_thumbnail(message="未选择照片" if info is None else "暂无预览")
            return
        key = (info.path, info.size_bytes, info.file_mtime_ns, info.modified_at)
        if key == self._thumbnail_key and self._thumbnail_work is not None and self._thumbnail_work[0] == key:
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
        self._thumbnail_work = key, self._thumbnail_pool.submit(_load_thumbnail, info)
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
            if len(self._thumbnail_cache) > 64:
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
