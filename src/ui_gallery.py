"""Scrollable photo grid. Decode only tiles near the viewport."""

from __future__ import annotations

import tkinter as tk
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from src.photo_info import PhotoInfo
from src.preview import PILLOW_AVAILABLE, is_supported, load_color_preview
from src.ui_theme import ACCENT, BORDER_SOFT, CARD_BG, ORANGE, TEXT, TEXT_SUB, WIN_BG


@dataclass(frozen=True)
class GalleryPhoto:
    info: PhotoInfo
    picked: bool = False
    status: str = ""


def _load_tile(info: PhotoInfo, size: tuple[int, int]):
    image = load_color_preview(info)
    image.thumbnail(size)
    return image


class PhotoGallery(tk.Canvas):
    GAP = 12
    CAPTION = 28
    CHECKBOX = 22
    CACHE_LIMIT = 96

    def __init__(self, parent, font):
        super().__init__(
            parent, bg=WIN_BG, highlightthickness=0, takefocus=True, yscrollincrement=24,
        )
        self._font = font
        self._items: list[GalleryPhoto] = []
        self._indices: dict[str, int] = {}
        self._focused = ""
        self._selected: set[str] = set()
        self._columns = 1
        self._width = 1
        self._height = 1
        self._step = 1
        self._empty_message = ""
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._cache: OrderedDict[tuple, object] = OrderedDict()
        self._work: dict[tuple, Future] = {}
        self._images: dict[tuple, object] = {}
        self._draw_job = None
        self._poll_job = None
        self.bind("<Configure>", self._schedule_draw)
        self.bind("<Map>", self._schedule_draw)
        self.bind("<MouseWheel>", self._scroll_wheel)
        self.bind("<Destroy>", self._close)

    @property
    def columns(self) -> int:
        return self._columns

    def show_photos(self, photos: list[GalleryPhoto], empty_message: str) -> None:
        indices = {str(photo.info.path): index for index, photo in enumerate(photos)}
        if indices != self._indices:
            self.yview_moveto(0)
        self._items = photos
        self._indices = indices
        self._empty_message = empty_message
        self._schedule_draw()

    def show_selection(self, focused: str, selected: set[str]) -> None:
        self._focused = focused
        self._selected = selected.copy()
        self._schedule_draw()

    def hit(self, x: int, y: int) -> tuple[str, bool]:
        """Return the tile and whether its checkbox was clicked."""
        x = int(self.canvasx(x)) - self.GAP
        y = int(self.canvasy(y)) - self.GAP
        if x < 0 or y < 0:
            return "", False
        column, dx = divmod(x, self._width + self.GAP)
        row, dy = divmod(y, self._step)
        index = row * self._columns + column
        if (column >= self._columns or index >= len(self._items)
                or dx >= self._width or dy >= self._height + self.CAPTION):
            return "", False
        in_box = 8 <= dx <= 8 + self.CHECKBOX and 8 <= dy <= 8 + self.CHECKBOX
        return str(self._items[index].info.path), in_box

    def see_photo(self, iid: str) -> None:
        if iid not in self._indices:
            return
        top = self._indices[iid] // self._columns * self._step + self.GAP
        bottom = top + self._height + self.CAPTION
        view_top = self.canvasy(0)
        view_height = self.winfo_height()
        total = self._content_height()
        if top < view_top:
            self.yview_moveto(max(0, top - self.GAP) / total)
        elif bottom > view_top + view_height:
            self.yview_moveto((bottom + self.GAP - view_height) / total)
        self._schedule_draw()

    def scroll(self, *args) -> None:
        self.yview(*args)
        self._schedule_draw()

    def _scroll_wheel(self, event) -> str:
        steps = max(1, abs(event.delta) // 120) * 3
        self.scroll("scroll", -steps if event.delta > 0 else steps, "units")
        return "break"

    def _schedule_draw(self, _event=None) -> None:
        if self._draw_job is None:
            self._draw_job = self.after_idle(self._draw)

    def _content_height(self) -> int:
        rows = (len(self._items) + self._columns - 1) // self._columns
        return max(self.winfo_height(), self.GAP + rows * self._step)

    def _draw(self) -> None:
        self._draw_job = None
        if not self.winfo_ismapped():
            return
        width = self.winfo_width()
        height = self.winfo_height()
        self._columns = max(1, (width - self.GAP) // 225)
        self._width = max(1, (width - (self._columns + 1) * self.GAP) // self._columns)
        self._height = max(1, self._width * 5 // 8)
        self._step = self._height + self.CAPTION + self.GAP
        self.configure(scrollregion=(0, 0, width, self._content_height()))
        self.delete("all")
        images = {}
        if not self._items:
            self.create_text(
                width // 2, height // 3, text=self._empty_message, fill=TEXT_SUB,
                font=self._font, width=max(1, width - 60), justify="center",
            )
        else:
            top = max(0, int(self.canvasy(0)))
            first = top // self._step * self._columns
            last = min(len(self._items), ((top + height) // self._step + 1) * self._columns)
            for index in range(first, last):
                self._draw_tile(index, images)
        self._images = images
        if self._work and self._poll_job is None:
            self._poll_job = self.after(25, self._poll)

    def _draw_tile(self, index: int, images: dict) -> None:
        photo = self._items[index]
        info = photo.info
        iid = str(info.path)
        row, column = divmod(index, self._columns)
        x = self.GAP + column * (self._width + self.GAP)
        y = self.GAP + row * self._step
        right, bottom = x + self._width, y + self._height
        checked = iid in self._selected
        focused = iid == self._focused
        self.create_rectangle(x, y, right, bottom, fill=CARD_BG, outline="")
        size = (self._width - 2, self._height - 2)
        image = None
        message = "暂无预览"
        if PILLOW_AVAILABLE and is_supported(info.category):
            # Size buckets keep small window resizes from decoding every image again.
            target = tuple(max(32, (length + 31) // 32 * 32) for length in size)
            key = (iid, info.size_bytes, info.file_mtime_ns, info.modified_at, target)
            if key in self._cache:
                decoded = self._cache[key]
                self._cache.move_to_end(key)
                if decoded is not None:
                    from PIL import ImageTk

                    render_key = key, size
                    image = self._images.get(render_key)
                    if image is None:
                        scaled = decoded.copy()
                        scaled.thumbnail(size)
                        image = ImageTk.PhotoImage(scaled, master=self)
                    images[render_key] = image
                else:
                    message = "无法读取预览"
            else:
                message = "正在加载…"
                if key not in self._work and len(self._work) < 2:
                    self._work[key] = self._pool.submit(_load_tile, info, target)
        if image is not None:
            self.create_image((x + right) // 2, (y + bottom) // 2, image=image)
        else:
            self.create_text((x + right) // 2, (y + bottom) // 2, text=message, fill=TEXT_SUB, font=self._font)
        self.create_rectangle(
            x, y, right, bottom, outline=ACCENT if focused or checked else BORDER_SOFT,
            width=2 if focused else 1,
        )
        box_x, box_y = x + 8, y + 8
        self.create_rectangle(
            box_x, box_y, box_x + self.CHECKBOX, box_y + self.CHECKBOX,
            outline=TEXT, fill=ACCENT if checked else WIN_BG,
        )
        if checked:
            self.create_line(
                box_x + 5, box_y + 11, box_x + 9, box_y + 15, box_x + 17, box_y + 7,
                fill=TEXT, width=2,
            )
        status = "  ".join(part for part in ("★" if photo.picked else "", photo.status) if part)
        status_width = self._font.measure(status) + 16 if status else 8
        name = info.file_name
        if self._font.measure(name) > self._width - status_width - 7:
            while name and self._font.measure(name + "…") > self._width - status_width - 7:
                name = name[:-1]
            name += "…"
        self.create_text(x + 3, bottom + 14, text=name, anchor="w", fill=TEXT, font=self._font)
        if status:
            self.create_text(
                right - 4, bottom + 14, text=status, anchor="e",
                fill=ORANGE if photo.picked else TEXT_SUB, font=self._font,
            )

    def _poll(self) -> None:
        self._poll_job = None
        completed = False
        for key, future in list(self._work.items()):
            if not future.done():
                continue
            completed = True
            del self._work[key]
            try:
                self._cache[key] = future.result()
            except Exception:
                self._cache[key] = None
            if len(self._cache) > self.CACHE_LIMIT:
                self._cache.popitem(last=False)
        if completed:
            self._schedule_draw()
        elif self._work and self.winfo_ismapped():
            self._poll_job = self.after(25, self._poll)

    def _close(self, _event=None) -> None:
        for job in (self._poll_job, self._draw_job):
            if job is not None:
                self.after_cancel(job)
        self._pool.shutdown(wait=False, cancel_futures=True)
