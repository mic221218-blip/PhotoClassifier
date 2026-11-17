"""`src.gui` 的测试（GUI V1.5 外壳）。

在真实 Tk 环境中构建窗口（Windows 桌面会话可用），不弹窗、不进入主循环：
- 文件夹库与结果列表正确显示数据库内容；
- 选中结果行时详情面板联动；
- 直接调用扫描 worker 时，真实完成"扫描 → EXIF → 入库 → 刷新"流程。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

import src.gui as gui
from src.gui import PhotoClassifierApp
from src.quality import ANALYZER_VERSION, FLAG_IDS, QualityMeasurement
from src.tags import PICK_TAG_NAME

from exif_maker import nikon_z6iii_nef
from test_database import make_info


@pytest.fixture(scope="module")
def tk_root():
    """整个模块共享一次 Tk 初始化（Windows 下重复创建 Tk 偶发 tcl 资源竞态）。"""
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture()
def root(tk_root):
    """每个测试用独立的 Toplevel 作为窗口容器（不弹窗）。"""
    win = tk.Toplevel(tk_root)
    win.withdraw()
    yield win
    win.destroy()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "gui.db"


def seeded_app(root, db_path: Path):
    """预置一个文件夹和两条照片记录的应用。"""
    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder("D:/Photos/2026")
    app.db.upsert_many(
        [
            make_info(Path("D:/Photos/2026/a.NEF")),
            make_info(Path("D:/Photos/2026/b.JPG"), category="jpeg", iso=800),
        ]
    )
    app.checked_folders.add(str(Path("D:/Photos/2026").resolve()))
    app.refresh_library()
    app.refresh_results()
    return app


def test_combined_ai_analysis_uses_the_same_checked_photos(root, db_path, tmp_path, monkeypatch) -> None:
    import src.ui_ai as ai_ui
    from src.database import PhotoDatabase
    from src.exif_reader import read_photo_info
    from src.person_ai import MODEL_VERSION, PersonResult

    library = tmp_path / "library"
    library.mkdir()
    files = [library / name for name in ("person.jpg", "landscape.jpg", "unselected.jpg")]
    for path in files:
        path.write_bytes(b"fixture")
    paths = [str(path.resolve()) for path in files]
    infos = [read_photo_info(path, "jpeg") for path in files]
    real_batch = ai_ui.analyze_person_selection
    seen = []
    sky_seen = []

    class Detector:
        def analyze(self, info):
            seen.append(info.file_name)
            return PersonResult(info.file_name == "person.jpg", 0.1)

    monkeypatch.setattr(ai_ui, "analyze_person_selection",
                        lambda *args, **kwargs: real_batch(*args, **kwargs, make_analyzer=Detector))

    def sky_analysis(infos, db, *, progress, cancelled):
        sky_seen.extend(str(info.path) for info in infos)
        return len(infos), 0, 0, None

    monkeypatch.setattr(ai_ui, "analyze_sky_selection", sky_analysis)
    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(library)
        app.db.upsert_many(infos)
        app.refresh_results()
        app.start_result_selection()
        app._select_result_row(paths[0])
        app._select_result_row(paths[1])
        app._select_result_row(paths[2])
        app._select_result_row(paths[2])
        app.ai_menu.invoke(2)
        deadline = time.monotonic() + 5
        while app.ai_thread is not None and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert app.ai_thread is None
        assert set(seen) == {"person.jpg", "landscape.jpg"}
        assert set(sky_seen) == set(paths[:2])
        assert set(app.result_tree.selection()) == set(paths[:2])
        assert str(app.current.path) == paths[2]
        assert app.selection_count["text"] == "已选 2 张"
        assert app.person_results == {paths[0]: True, paths[1]: False}
        assert "AI · 人物" in app.result_tree.item(paths[0], "values")[6]
        app.person_filter.set(True)
        app.refresh_results()
        assert app.result_tree.get_children() == (paths[0],)
        app.clear_filters()
        assert set(app.result_tree.get_children()) == set(paths)
        with PhotoDatabase.open(db_path) as reopened:
            assert reopened.person_results(MODEL_VERSION) == app.person_results
            reopened.remove_photos([files[0]])
            reopened.upsert_photo(infos[0])
            assert paths[0] not in reopened.person_results(MODEL_VERSION)
        assert all(path.read_bytes() == b"fixture" for path in files)
    finally:
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_sky_analysis_combines_with_person_filter_and_reuses_results(root, db_path, tmp_path, monkeypatch):
    from src import ui_ai
    from src.database import PhotoDatabase
    from src.exif_reader import read_photo_info
    from src.person_ai import MODEL_VERSION as PERSON_VERSION
    from src.sky_ai import MODEL_VERSION, SkyResult, analyze_selection

    files = [tmp_path / name for name in ("both.jpg", "sky.jpg", "person.jpg", "unselected.jpg")]
    for path in files:
        path.write_bytes(b"fixture")
    infos = [read_photo_info(path, "jpeg") for path in files]
    both, sky, person, unselected = [str(info.path) for info in infos]
    seen = []

    class Detector:
        def analyze(self, info):
            seen.append(info.file_name)
            detected = info.file_name in {"both.jpg", "sky.jpg"}
            return SkyResult(detected, 0.6 if detected else 0.1)

    monkeypatch.setattr(ui_ai, "analyze_sky_selection",
                        lambda *args, **kwargs: analyze_selection(*args, **kwargs, make_analyzer=Detector))
    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(tmp_path)
        app.db.upsert_many(infos)
        for info in (infos[0], infos[2]):
            assert app.db.save_person_result(info, PERSON_VERSION, info.file_mtime_ns, True, 0.1)
        app.refresh_results()
        app.start_result_selection()
        app.btn_select_page.invoke()
        app._select_result_row(unselected)
        for _ in range(2):
            app.ai_menu.invoke(1)
            assert app.btn_scan["state"] == "disabled"
            deadline = time.monotonic() + 5
            while app.ai_thread is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            assert app.ai_thread is None
        assert sorted(seen) == ["both.jpg", "person.jpg", "sky.jpg"]
        assert app.sky_results == {both: True, sky: True, person: False}
        assert "AI · 人物 · 天空" in app.result_tree.item(both, "values")[6]
        app.set_detail(infos[0])
        assert app.ai_details["天空"]["text"] == "AI 建议 · 天空"
        app.sky_filter.set(True)
        app.refresh_results()
        assert set(app.result_tree.get_children()) == {both, sky}
        app.person_filter.set(True)
        app.refresh_results()
        assert app.result_tree.get_children() == (both,)
        app.and_mode.set(False)
        app.refresh_results()
        assert set(app.result_tree.get_children()) == {both, sky, person}
        app.clear_filters()
        assert set(app.result_tree.get_children()) == {both, sky, person, unselected}
        with PhotoDatabase.open(db_path) as reopened:
            assert reopened.sky_results(MODEL_VERSION) == app.sky_results
            assert reopened.remove_photos([files[0]]) == 1
            reopened.upsert_photo(infos[0])
            assert both not in reopened.sky_results(MODEL_VERSION)
        assert all(path.read_bytes() == b"fixture" for path in files)
    finally:
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_selection_mode_toggles_rows_and_cancels_across_pages(root, db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "RESULT_PAGE_SIZE", 2)
    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(tmp_path)
        app.db.upsert_many([
            make_info(tmp_path / "a.jpg", category="jpeg"),
            make_info(tmp_path / "b.jpg", category="jpeg", iso=6400),
            make_info(tmp_path / "c.jpg", category="jpeg"),
            make_info(tmp_path / "d.mp4", category="video"),
        ])
        app.refresh_results()
        app.set_result_view("list")
        root.deiconify()
        root.update()
        tree = app.result_tree
        first, second = tree.get_children()
        assert app.status_left.winfo_viewable()
        click_time = 0

        def click(iid, column, *, delay=1000):
            nonlocal click_time
            click_time += delay
            x, y, width, height = tree.bbox(iid, column)
            event = dict(x=x + width // 2, y=y + height // 2, time=click_time)
            tree.event_generate("<ButtonPress-1>", **event)
            tree.event_generate("<ButtonRelease-1>", **event)
            root.update()

        click(second, "name")
        assert str(app.current.path) == second
        assert not app._selecting_results
        assert app._selected_iids == set()
        app.btn_select_results.invoke()
        root.update()
        assert tree.selection() == ()
        assert app.btn_ai["state"] == "disabled"
        click(first, "name")
        assert tree.item(first, "text") == "☑"
        assert tree.selection() == (first,)
        click(first, "name", delay=50)
        assert tree.item(first, "text") == "☐"
        click(first, "name")
        click(first, "#0")
        assert tree.item(first, "text") == "☐"
        assert tree.selection() == ()
        click(first, "#0")
        app.change_result_page(1)
        third, video = tree.get_children()
        app.btn_select_page.invoke()
        assert app._selected_iids == {first, third, video}
        assert app.selection_count["text"] == "已选 3 张"
        assert "2 张" in app.btn_ai["text"]
        app.btn_select_page.invoke()
        assert app._selected_iids == {first}
        click(third, "name")
        app.btn_pick.invoke()
        picked = app.tag_controller.get_by_name(PICK_TAG_NAME)
        assert app.tag_controller.tag_counts[picked.id] == 2
        assert app._selected_iids == {first, third}
        app.btn_cancel_selection.invoke()
        assert not app._selecting_results
        assert tree.selection() == ()
        assert tree.item(third, "text") == "☐"
        assert str(app.current.path) == third
        app.change_result_page(-1)
        app.btn_select_results.invoke()
        assert tree.item(first, "text") == "☐"
        assert app._selected_iids == set()
        app.btn_select_page.invoke()
        app.rule_vars["high_iso"].set(True)
        app._on_filter_changed()
        assert app._selected_iids == {second}
    finally:
        root.withdraw()
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_ctrl_and_shift_click_update_ai_checks_and_row_selection(root, db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "RESULT_PAGE_SIZE", 4)
    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(tmp_path)
        app.db.upsert_many([
            make_info(tmp_path / "a.jpg", category="jpeg"),
            make_info(tmp_path / "b.mp4", category="video"),
            make_info(tmp_path / "c.jpg", category="jpeg"),
            make_info(tmp_path / "d.jpg", category="jpeg"),
            make_info(tmp_path / "e.jpg", category="jpeg"),
        ])
        app.refresh_results()
        app.set_result_view("list")
        root.deiconify()
        root.update()
        tree = app.result_tree
        first, video, third, last = tree.get_children()
        click_time = 0

        def click(iid, modifiers=0, column="name"):
            nonlocal click_time
            click_time += 1000
            x, y, width, height = tree.bbox(iid, column)
            event = dict(x=x + width // 2, y=y + height // 2, state=modifiers, time=click_time)
            tree.event_generate("<ButtonPress-1>", **event)
            tree.event_generate("<ButtonRelease-1>", **event)
            root.update()

        click(first)
        assert app._selected_iids == set()
        click(third, modifiers=0x4)  # Ctrl
        assert app._selected_iids == {first, third}
        assert tree.selection() == (first, third)
        click(third, modifiers=0x4, column="#0")
        assert app._selected_iids == {first}
        assert tree.item(third, "text") == "☐"

        app.change_result_page(1)
        other_page = tree.get_children()[0]
        app.btn_select_page.invoke()
        app.change_result_page(-1)
        root.update()
        click(first)
        click(last, modifiers=0x1)  # Shift
        assert app._selected_iids == {first, video, third, last, other_page}
        assert tree.selection() == (first, video, third, last)
        assert tree.item(video, "text") == "☑"
        assert app.selection_count["text"] == "已选 5 张"
        click(third, modifiers=0x1)
        assert app._selected_iids == {first, video, third, other_page}
        assert tree.selection() == (first, video, third)
        assert app.btn_ai["state"] == "normal"
        tree.focus_force()
        root.update()
        tree.event_generate("<Escape>")
        root.update()
        assert not app._selecting_results
        assert app._selected_iids == set()
        assert tree.selection() == ()
        assert tree.focus() == ""
        click(last, modifiers=0x4)
        assert app._selected_iids == {last}
        assert tree.selection() == (last,)
    finally:
        root.withdraw()
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_large_library_filters_before_paging_and_keeps_cross_page_selection(root, db_path, tmp_path):
    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(tmp_path)
        with app.db._conn:
            app.db._conn.executemany(
                "INSERT INTO photos (path,file_name,category,size_bytes,exif_status,iso,first_seen_at,last_scanned_at) "
                "VALUES (?,?, 'jpeg',1,'ok',?,'now','now')",
                [(str(tmp_path / f"{i:05}.jpg"), f"{i:05}.jpg", 6400 if i == 20009 else 100)
                 for i in range(20010)],
            )
        app.refresh_results()
        assert len(app._matched_iids) == 20010
        first = app.result_tree.get_children()[0]
        app.start_result_selection()
        app._select_result_row(first)
        app.change_result_page(1)
        second = app.result_tree.get_children()[0]
        app._select_result_row(second)
        assert {str(info.path) for info in app._selected_infos()} == {first, second}
        app.rule_vars["high_iso"].set(True)
        app._on_filter_changed()
        assert app.result_tree.get_children() == (str(tmp_path / "20009.jpg"),)
        assert app._selected_infos() == []
    finally:
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_quality_cancel_button_targets_quality_worker(root, db_path):
    app = PhotoClassifierApp(root, db_path)
    try:
        app._set_analyzing_ui(True)
        app.btn_cancel.invoke()
        assert app.analyze_cancel.is_set()
        assert not app.cancel_event.is_set()
        app._set_analyzing_ui(False)
        app._set_scanning_ui(True)
        app.btn_cancel.invoke()
        assert app.cancel_event.is_set()
    finally:
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_thumbnail_displays_latest_selection(root, db_path, tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    import src.ui_thumbnail as thumbnails

    started = threading.Event()
    release = threading.Event()
    app = PhotoClassifierApp(root, db_path)

    def load(info, _size):
        if info.file_name == "old.JPG":
            started.set()
            assert release.wait(3)
            return Image.new("RGB", (4, 3), "red")
        return Image.new("RGB", (2, 3), "blue")

    monkeypatch.setattr(thumbnails, "_load_thumbnail", load)
    try:
        app.set_detail(make_info(tmp_path / "old.JPG", category="jpeg"))
        assert started.wait(3)
        app.set_detail(make_info(tmp_path / "new.JPG", category="jpeg"))
        release.set()
        deadline = time.monotonic() + 5
        while app._thumbnail_image is None and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert app._thumbnail_image.width() == 2
        assert len(app._thumbnail_cache) == 2
    finally:
        release.set()
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_gallery_preview_and_selection_follow_the_current_view(root, db_path, tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    from src.exif_reader import read_photo_info

    monkeypatch.setattr(gui, "RESULT_PAGE_SIZE", 2)
    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(tmp_path)
        infos = []
        for index, color in enumerate(("red", "blue", "green")):
            path = tmp_path / f"{index}.jpg"
            Image.new("RGB", (640, 400), color).save(path)
            infos.append(read_photo_info(path, "jpeg"))
        app.db.upsert_many(infos)
        app.refresh_library()
        app.refresh_results()
        root.deiconify()
        root.update()
        deadline = time.monotonic() + 5
        while len(app.gallery._images) < 2 and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert len(app.gallery._images) == 2
        assert app.gallery.winfo_ismapped()
        assert not app.result_tree.winfo_ismapped()

        app.gallery.event_generate("<Button-1>", x=70, y=60, time=1000)
        assert app.current == infos[0]
        assert not app.selection_actions.winfo_manager()
        app.gallery.event_generate("<Button-1>", x=24, y=24, time=2000)
        assert app.selection_count["text"] == "已选 1 张"
        assert app.selection_actions.winfo_manager()
        app.set_result_view("list")
        assert app.result_tree.selection() == (str(infos[0].path),)
        app.set_result_view("grid")
        app.btn_cancel_selection.invoke()
        assert not app.selection_actions.winfo_manager()

        app.btn_current_pick.invoke()
        assert app.btn_current_pick["text"] == "取消精选"
        app.quick_filter_buttons["picked"].invoke()
        assert row_names(app) == {infos[0].file_name}
        app.quick_filter_buttons["all"].invoke()
        app.open_gallery_preview()
        assert app._preview_window.title() == infos[0].file_name
        app._navigate_gallery(0, 1)
        app._navigate_gallery(0, 1)
        assert app._result_page == 1
        assert app._preview_window.title() == infos[2].file_name
        app._preview_window.destroy()
        app.toggle_detail_panel()
        assert not app.detail_panel.winfo_manager()
        app.toggle_detail_panel()
        assert app.detail_panel.winfo_manager()
    finally:
        root.withdraw()
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_scan_folds_timelapse_and_selects_all_source_frames(root, db_path, tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from exif_maker import (
        TAG_DATETIME_ORIGINAL, TAG_ISO, TAG_MAKE, TAG_MODEL, TYPE_SHORT,
        ascii_value, build_jpeg, build_tiff,
    )

    monkeypatch.setattr(gui, "RESULT_PAGE_SIZE", 20)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, message: pytest.fail(message))
    revealed = []

    def reveal(path):
        revealed.append(path)
        return True, ""

    monkeypatch.setattr(gui, "open_in_explorer", reveal)
    library = tmp_path / "photos"
    library.mkdir()
    start = datetime(2026, 9, 18, 6, 10)
    camera = [(TAG_MAKE, *ascii_value("NIKON")), (TAG_MODEL, *ascii_value("NIKON Z6III"))]

    def write_frame(path, taken_at, iso=100):
        exif = [
            (TAG_DATETIME_ORIGINAL, *ascii_value(taken_at.strftime("%Y:%m:%d %H:%M:%S"))),
            (TAG_ISO, TYPE_SHORT, (iso,)),
        ]
        path.write_bytes(build_jpeg(build_tiff(camera, exif)))

    frames = [str(library / f"DSC_{index % 10000:04}.JPG") for index in range(9500, 10500)]
    for index, path in enumerate(frames):
        write_frame(Path(path), start + timedelta(seconds=5 * index), iso=6400 if index < 2 else 100)
    burst = {str(library / f"DSC_{index:04}.JPG") for index in range(1000, 1008)}
    for path in burst:
        write_frame(Path(path), start + timedelta(hours=2))

    app = PhotoClassifierApp(root, db_path)
    try:
        app.db.add_folder(library)
        app._scan_worker([library])
        app._poll_scan_queue()

        assert app.db.count_photos() == 1008
        assert app._page_count() == 1
        stack_iids = set(app.result_tree.get_children()) - burst
        assert len(stack_iids) == 1
        assert burst.issubset(app.result_tree.get_children())
        stack_iid, = stack_iids
        card = next(photo for photo in app.gallery._items if photo.tile_id == stack_iid)
        assert card.title == "延时 · 1,000 帧"
        assert card.status == "每 5 秒一张"
        assert str(card.info.path) in frames[499:501]
        assert stack_iid not in frames

        app._focus_gallery_photo(stack_iid)
        assert app._focus_iid == stack_iid
        assert not app._selecting_results
        assert app._selected_infos() == []
        app.open_focused_timelapse_folder()
        assert [path.parent for path in revealed] == [library]

        app.toggle_focused_timelapse()
        assert app.result_tree.get_children() == tuple(frames[:20])
        app.change_result_page(1)
        assert app.result_tree.get_children() == tuple(frames[20:40])
        app.btn_leave_timelapse.invoke()
        assert set(app.result_tree.get_children()) == {stack_iid, *burst}

        app.quick_filter_buttons["timelapse"].invoke()
        assert app.result_tree.get_children() == (stack_iid,)
        app._focus_gallery_photo(stack_iid)
        app.select_focused_timelapse()
        assert {str(info.path) for info in app._selected_infos()} == set(frames)
        assert app.selection_count["text"] == "已选 1,000 张"
        app.cancel_result_selection()

        app.quick_filter_buttons["all"].invoke()
        app.rule_vars["high_iso"].set(True)
        app._on_filter_changed()
        assert set(app.result_tree.get_children()) == set(frames[:2])
        app.start_result_selection()
        app._select_result_row(frames[0])
        app.clear_filters()
        assert app.result_tree.item(stack_iid, "text") == "⊟"
        assert app.gallery._partial_selection == {stack_iid}
        assert [str(info.path) for info in app._selected_infos()] == [frames[0]]
        app._select_result_row(stack_iid)
        assert app.result_tree.item(stack_iid, "text") == "☑"
        assert not app.gallery._partial_selection
        assert {str(info.path) for info in app._selected_infos()} == set(frames)
        app.cancel_result_selection()

        app.btn_fold_timelapse.invoke()
        assert app._page_count() == 51
        assert app.result_tree.get_children() == tuple(sorted(frames)[:20])

        cull = app.quick_filter_buttons["cull"]
        assert cull["text"] == "筛选组"
        cull.invoke()
        assert set(app.result_tree.get_children()) == burst
        assert {photo.tile_id for photo in app.gallery._items} == burst

        app.start_result_selection()
        app.toggle_result_page_selection()
        assert {str(info.path) for info in app._selected_infos()} == burst
        app.cancel_result_selection()

        app.rule_vars["high_iso"].set(True)
        app._on_filter_changed()
        assert not app.result_tree.get_children()
        app.rule_vars["high_iso"].set(False)
        app._on_filter_changed()

        app.btn_fold_timelapse.invoke()
        assert set(app.result_tree.get_children()) == burst
        app.quick_filter_buttons["all"].invoke()
        assert set(app.result_tree.get_children()) == {stack_iid, *burst}
    finally:
        app.tag_store.close()
        app.quality_store.close()
        app.db.close()


def test_app_lists_library_and_results(root, db_path: Path) -> None:
    app = seeded_app(root, db_path)

    library_items = app.library_tree.get_children()
    assert len(library_items) == 1
    assert "d:/photos/2026" in library_items[0].replace("\\", "/").lower()

    result_names = {
        app.result_tree.item(iid, "values")[0] for iid in app.result_tree.get_children()
    }
    assert result_names == {"a.NEF", "b.JPG"}
    assert "2" in app.count_label.cget("text")


def test_select_row_updates_detail(root, db_path: Path) -> None:
    app = seeded_app(root, db_path)

    iid = str(Path("D:/Photos/2026/a.NEF").resolve())
    app.result_tree.selection_set(iid)
    app.result_tree.event_generate("<<TreeviewSelect>>")
    root.update()

    assert app.current is not None
    assert app.current.file_name == "a.NEF"
    assert "a.NEF" in app.detail_name.cget("text")


def test_scan_worker_processes_real_files(root, db_path: Path, tmp_path: Path) -> None:
    """直接调用扫描 worker（不经线程），验证完整流程。"""
    photo_dir = tmp_path / "photos"
    photo_dir.mkdir()
    (photo_dir / "DSC_0001.NEF").write_bytes(nikon_z6iii_nef())
    (photo_dir / "notes.txt").write_bytes(b"hello")
    (photo_dir / "broken.NEF").write_bytes(b"garbage")

    app = PhotoClassifierApp(root, db_path)
    app.checked_folders.add(str(photo_dir))
    app._scan_worker([photo_dir])  # 同步执行

    # 数据库里有 3 条记录（NEF 正常 + TXT 跳过 + 损坏 NEF 异常）
    assert app.db.count_photos() == 3
    statuses = app.db.count_by_exif_status()
    assert statuses["ok"] == 1
    assert statuses["error"] == 1
    assert statuses["skipped"] == 1
    # 文件夹库记录了扫描时间
    assert app.db.list_folders()[0].last_scanned_at is not None

    app.refresh_results()
    assert app.result_tree.get_children()  # 结果列表已刷新
    app.db.close()


def test_scan_worker_runs_in_background_thread(root, db_path: Path, tmp_path: Path) -> None:
    """回归：后台线程必须自己开 SQLite 连接，不能复用主线程连接。"""
    photo_dir = tmp_path / "photos"
    photo_dir.mkdir()
    (photo_dir / "DSC_0001.NEF").write_bytes(nikon_z6iii_nef())

    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder(photo_dir)
    errors = []
    previous = threading.excepthook
    threading.excepthook = lambda args: errors.append(repr(args.exc_value))
    try:
        worker = threading.Thread(target=app._scan_worker, args=([str(photo_dir)],))
        worker.start()
        worker.join(timeout=20)
    finally:
        threading.excepthook = previous

    assert not worker.is_alive()
    assert errors == []
    assert app.db.count_photos() == 1
    messages = []
    while not app.msg_queue.empty():
        messages.append(app.msg_queue.get())
    assert messages[-1] == ("done", 1, 0)
    app.db.close()


def test_copy_path_puts_path_on_clipboard(root, db_path: Path) -> None:
    app = seeded_app(root, db_path)

    iid = str(Path("D:/Photos/2026/a.NEF").resolve())
    app.result_tree.selection_set(iid)
    app._on_result_select()
    app.copy_selected_path()
    root.update()

    assert root.clipboard_get() == iid
    app.db.close()


# --------------------------------------------------------------------------- #
# 规则筛选（V2）
# --------------------------------------------------------------------------- #


def seed_rule_rows(app) -> None:
    """A=夜景+高感，B=只有夜景，C=只有高感。"""
    app.db.add_folder("D:/Photos/2026")
    app.db.upsert_many(
        [
            make_info(
                Path("D:/Photos/2026/a.NEF"),
                taken_at="2026-09-18 20:15:32",
                iso=6400,
            ),
            make_info(
                Path("D:/Photos/2026/b.NEF"),
                taken_at="2026-09-18 21:00:00",
                iso=200,
            ),
            make_info(
                Path("D:/Photos/2026/c.NEF"),
                taken_at="2026-09-18 12:00:00",
                iso=6400,
            ),
        ]
    )
    app.checked_folders.add(str(Path("D:/Photos/2026").resolve()))
    app.refresh_library()
    app.refresh_results()


def row_names(app) -> set[str]:
    return {app.result_tree.item(iid, "values")[0] for iid in app.result_tree.get_children()}


def row_names_in_order(app) -> list[str]:
    return [
        app.result_tree.item(iid, "values")[0] for iid in app.result_tree.get_children()
    ]


def test_rule_filter_and_mode(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    assert row_names(app) == {"a.NEF", "b.NEF", "c.NEF"}

    app.rule_vars["night"].set(True)
    app._on_filter_changed()
    assert row_names(app) == {"a.NEF", "b.NEF"}

    app.rule_vars["high_iso"].set(True)  # AND：夜景 且 高感
    app._on_filter_changed()
    assert row_names(app) == {"a.NEF"}

    app.db.close()


def test_rule_filter_or_mode(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)

    app.rule_vars["night"].set(True)
    app.rule_vars["high_iso"].set(True)
    app.and_mode.set(False)  # OR：夜景 或 高感
    app._on_filter_changed()

    assert row_names(app) == {"a.NEF", "b.NEF", "c.NEF"}
    app.db.close()


def test_clear_filters_restores_all_rows(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)

    app.rule_vars["night"].set(True)
    app._on_filter_changed()
    assert row_names(app) == {"a.NEF", "b.NEF"}

    app.clear_filters()
    assert row_names(app) == {"a.NEF", "b.NEF", "c.NEF"}
    app.db.close()


def test_chips_column_shows_rule_labels(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)

    row_values = {
        app.result_tree.item(iid, "values")[0]: app.result_tree.item(iid, "values")[6]
        for iid in app.result_tree.get_children()
    }
    assert "夜景" in row_values["a.NEF"]
    assert "高感" in row_values["a.NEF"]
    assert "夜景" in row_values["b.NEF"] and "高感" not in row_values["b.NEF"]
    assert "高感" in row_values["c.NEF"] and "夜景" not in row_values["c.NEF"]
    app.db.close()


def test_detail_panel_shows_rule_reasons(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)

    iid = str(Path("D:/Photos/2026/a.NEF").resolve())
    app.result_tree.selection_set(iid)
    app._on_result_select()
    root.update()

    texts = []
    for row_frame in app.rules_frame.winfo_children():
        for label in row_frame.winfo_children():
            texts.append(label.cget("text"))
    assert any("夜景" in text for text in texts)
    assert any("ISO 6400 ≥ 3200" in text for text in texts)
    app.db.close()


# --------------------------------------------------------------------------- #
# 质量筛选与详情（V2.5）
# --------------------------------------------------------------------------- #


def quality_row(
    path: str,
    *,
    blur: float = 500.0,
    contrast: float = 50.0,
    mean: float = 110.0,
    std: float = 45.0,
    flag_blur: bool = False,
) -> QualityMeasurement:
    return QualityMeasurement(
        path=str(Path(path).resolve()),
        analyzer_version=ANALYZER_VERSION,
        analyzed_at="2026-07-13 06:00:00",
        preview_source="full_image",
        preview_width=640,
        preview_height=480,
        blur_score=blur,
        contrast=contrast,
        anisotropy=0.10,
        mean_lum=mean,
        std_lum=std,
        clip_low_frac=0.001,
        clip_high_frac=0.001,
        dhash="0" * 16,
        quick_hash="quick",
        file_size=12345,
        file_mtime="2026-09-18 21:00:00",
    )


def seed_quality_rows(app) -> None:
    """A=模糊，B=正常，C=过曝。"""
    app.quality_store.upsert_many(
        [
            quality_row("D:/Photos/2026/a.NEF", blur=60.0, contrast=40.0),
            quality_row("D:/Photos/2026/b.NEF"),
            quality_row("D:/Photos/2026/c.NEF", mean=230.0, blur=300.0),
        ]
    )
    app._quality_revision += 1
    app.refresh_results()


def test_quality_checkboxes_exist(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    assert set(app.quality_vars) == set(FLAG_IDS)
    app.db.close()
    app.quality_store.close()


def test_quality_filter_and_or(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    seed_quality_rows(app)

    app.quality_vars["blurry"].set(True)
    app._on_filter_changed()
    assert row_names(app) == {"a.NEF"}

    app.quality_vars["overexposed"].set(True)  # AND：既模糊又过曝
    app._on_filter_changed()
    assert row_names(app) == set()

    app.and_mode.set(False)  # OR：模糊或过曝
    app._on_filter_changed()
    assert row_names(app) == {"a.NEF", "c.NEF"}

    app.clear_filters()
    assert row_names(app) == {"a.NEF", "b.NEF", "c.NEF"}
    app.db.close()
    app.quality_store.close()


def test_quality_column_and_detail(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    seed_quality_rows(app)

    row_values = {
        app.result_tree.item(iid, "values")[0]: app.result_tree.item(iid, "values")
        for iid in app.result_tree.get_children()
    }
    assert "模糊" in row_values["a.NEF"][7]
    assert row_values["b.NEF"][7] == "正常"
    assert "过曝" in row_values["c.NEF"][7]

    iid = str(Path("D:/Photos/2026/a.NEF").resolve())
    app.result_tree.selection_set(iid)
    app._on_result_select()
    root.update()

    texts = []
    for row_frame in app.quality_frame.winfo_children():
        for label in row_frame.winfo_children():
            texts.append(label.cget("text"))
    assert any("模糊" in text or "清晰度" in text for text in texts)
    app.db.close()
    app.quality_store.close()


def test_analysis_worker_writes_quality(root, db_path: Path, tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    from image_maker import save_blurred_jpeg, save_sharp_jpeg

    photo_dir = tmp_path / "analysis_photos"
    photo_dir.mkdir()
    save_sharp_jpeg(photo_dir / "sharp.JPG")
    save_blurred_jpeg(photo_dir / "blurred.JPG")

    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder(photo_dir)
    app._scan_worker([photo_dir])
    infos = app.db.find_photos_under([photo_dir])
    assert len(infos) == 2

    app._analyze_worker(infos)
    final = None
    while True:
        message = app.analyze_queue.get_nowait()
        if message[0] in ("done", "error"):
            final = message
            break
    assert final is not None
    kind, report = final
    assert kind == "done"
    assert report.analyzed_now == 2
    assert report.failed == 0

    app._quality_revision += 1
    app.refresh_results()
    assert report.flag_counts.get("blurry", 0) == 1
    row_values = {
        app.result_tree.item(iid, "values")[0]: app.result_tree.item(iid, "values")
        for iid in app.result_tree.get_children()
    }
    assert "模糊" in row_values["blurred.JPG"][7]

    app.db.close()
    app.quality_store.close()


# --------------------------------------------------------------------------- #
# 文件夹库选择 / 打开文件位置（bugfix）
# --------------------------------------------------------------------------- #


def test_browsing_folder_keeps_the_scan_scope(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder("D:/Photos/2026")
    app.db.add_folder("D:/Photos/2025")
    app.db.upsert_many([
        make_info(Path("D:/Photos/2025/last.JPG")),
        make_info(Path("D:/Photos/2026/this.JPG")),
    ])
    app.refresh_library()
    item, other = app.library_tree.get_children()
    app.checked_folders = {other}
    app.library_tree.selection_set(item)
    app._on_library_select()
    assert app.library_tree.selection() == (item,)
    assert row_names(app) == {"last.JPG"}
    assert app._scan_paths(app._folder_paths) == [Path(other)]
    app.btn_library_all.invoke()
    assert row_names(app) == {"last.JPG", "this.JPG"}
    app.db.close()
    app.quality_store.close()


def test_remove_selected_folder_works_after_click(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder("D:/Photos/2026")
    app.refresh_library()
    item = app.library_tree.get_children()[0]

    app.library_tree.selection_set(item)
    app._on_library_select()
    assert app._browse_folder == item

    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    app.remove_selected_folder()

    assert app.db.list_folders() == []
    assert app._browse_folder is None
    app.db.close()
    app.quality_store.close()


def test_reveal_uses_shell_helper(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.current = make_info(Path("D:/Photos/2026/a.NEF"))
    calls = []
    monkeypatch.setattr(
        gui, "open_in_explorer", lambda path: (calls.append(path), (True, None))[1]
    )

    app.reveal_selected()

    assert calls == [app.current.path]
    app.db.close()
    app.quality_store.close()


def test_reveal_failure_copies_path_and_warns(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.current = make_info(Path("D:/Photos/2026/a.NEF"))
    warnings = []
    monkeypatch.setattr(gui, "open_in_explorer", lambda path: (False, "系统拒绝打开文件位置"))
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda *args, **kwargs: warnings.append(args)
    )

    app.reveal_selected()

    assert app.root.clipboard_get() == str(app.current.path)
    assert warnings and "系统拒绝" in warnings[0][1]
    app.db.close()
    app.quality_store.close()


# --------------------------------------------------------------------------- #
# 打开图片（双击 / 详情按钮）
# --------------------------------------------------------------------------- #


def test_open_button_enabled_and_opens_image(
    root, db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "a.NEF"
    target.write_bytes(b"x")
    app = PhotoClassifierApp(root, db_path)
    info = make_info(target)
    app.current = info
    app.set_detail(info)
    assert app.btn_open.cget("state") == "normal"

    calls = []
    monkeypatch.setattr(
        gui, "open_with_default_app", lambda path: (calls.append(path), (True, None))[1]
    )
    app.open_selected()

    assert calls == [info.path]
    assert "已打开" in app.status_left.cget("text")
    app.db.close()
    app.quality_store.close()


def test_double_click_opens_image_not_folder(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    binding = str(app.result_tree.bind("<Double-1>"))
    assert "_on_result_double_click" in binding
    app.db.close()
    app.quality_store.close()


def test_open_failure_warns(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.current = make_info(Path("D:/Photos/2026/a.NEF"))
    warnings = []
    monkeypatch.setattr(gui, "open_with_default_app", lambda path: (False, "无法打开文件"))
    monkeypatch.setattr(
        gui.messagebox, "showwarning", lambda *args, **kwargs: warnings.append(args)
    )
    app.open_selected()
    assert warnings
    app.db.close()
    app.quality_store.close()


# --------------------------------------------------------------------------- #
# 用户标签（V3）
# --------------------------------------------------------------------------- #


def test_tag_panel_seeded_with_presets(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    assert len(app.tag_tree.get_children()) == len(app.tag_controller.tags_by_id) == 10
    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_add_tag_updates_row_and_counts(root, db_path: Path) -> None:
    app = seeded_app(root, db_path)
    tag = app.tag_store.ensure_tag("建筑")
    app.tag_store.add_tags([Path("D:/Photos/2026/a.NEF")], [tag.id])
    app.tag_controller.mark_dirty()
    app.refresh_tags()
    app.refresh_results()

    row_values = {
        app.result_tree.item(iid, "values")[0]: app.result_tree.item(iid, "values")
        for iid in app.result_tree.get_children()
    }
    assert "★建筑" in row_values["a.NEF"][6]
    assert "★建筑" not in row_values["b.JPG"][6]
    assert any("建筑" in app.tag_tree.item(iid, "text") and "1" in app.tag_tree.item(iid, "text") for iid in app.tag_tree.get_children())

    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_tag_filter_and_clear(root, db_path: Path) -> None:
    app = seeded_app(root, db_path)
    tag = app.tag_store.ensure_tag("建筑")
    app.tag_store.add_tags([Path("D:/Photos/2026/a.NEF")], [tag.id])
    app.tag_controller.mark_dirty()
    app.refresh_tags()
    app.refresh_results()

    app.active_tags.add(tag.id)
    app._refresh_tag_list()
    app.refresh_results()
    assert row_names(app) == {"a.NEF"}

    app.clear_filters()
    assert row_names(app) == {"a.NEF", "b.JPG"}

    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_detail_tag_chips_and_remove(root, db_path: Path) -> None:
    app = seeded_app(root, db_path)
    tag = app.tag_store.ensure_tag("天空")
    app.tag_store.add_tags([Path("D:/Photos/2026/a.NEF")], [tag.id])
    app.tag_controller.mark_dirty()
    app.refresh_tags()
    app.refresh_results()

    iid = str(Path("D:/Photos/2026/a.NEF").resolve())
    app.result_tree.selection_set(iid)
    app._on_result_select()
    root.update()

    chip_texts = []
    for chip in app.tag_chips_frame.winfo_children():
        labels = [child for child in chip.winfo_children() if isinstance(child, tk.Label)]
        if labels:
            chip_texts.append(labels[0].cget("text"))
    assert "天空" in chip_texts

    app._remove_tag_from_current(tag.id)
    assert app.tag_store.counts().get(tag.id, 0) == 0

    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_batch_add_tags_and_undo(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder("D:/Photos/2026")
    app.db.upsert_many(
        [make_info(Path("D:/Photos/2026/a.NEF")), make_info(Path("D:/Photos/2026/b.NEF"))]
    )
    app.checked_folders.add(str(Path("D:/Photos/2026").resolve()))
    app.refresh_library()
    app.refresh_results()

    iids = app.result_tree.get_children()
    app.result_tree.selection_set(iids)
    monkeypatch.setattr(app, "_ask_tag_choice", lambda *args, **kwargs: "城市")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    app.batch_add_tags()

    tag = app.tag_store.get_by_name("城市")
    assert app.tag_store.counts()[tag.id] == 2
    assert app.tag_controller.undo_available

    app.undo_last_tag_operation()
    assert app.tag_store.counts().get(tag.id, 0) == 0

    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_batch_remove_tags_and_undo(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.db.add_folder("D:/Photos/2026")
    app.db.upsert_many(
        [make_info(Path("D:/Photos/2026/a.NEF")), make_info(Path("D:/Photos/2026/b.NEF"))]
    )
    app.checked_folders.add(str(Path("D:/Photos/2026").resolve()))
    app.refresh_library()
    app.refresh_results()

    tag = app.tag_store.ensure_tag("建筑")
    app.tag_store.add_tags(
        [Path("D:/Photos/2026/a.NEF"), Path("D:/Photos/2026/b.NEF")], [tag.id]
    )
    app.tag_controller.mark_dirty()
    app.refresh_tags()
    app.refresh_results()

    app.result_tree.selection_set(app.result_tree.get_children())
    monkeypatch.setattr(app, "_ask_tag_choice", lambda *args, **kwargs: "建筑")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    app.batch_remove_tags()
    assert app.tag_store.counts().get(tag.id, 0) == 0

    app.undo_last_tag_operation()
    assert app.tag_store.counts()[tag.id] == 2

    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_tag_search_filters_list(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    app.tag_search_var.set("建")
    root.update()
    texts = [app.tag_tree.item(iid, "text") for iid in app.tag_tree.get_children()]
    assert len(texts) == 1 and "建筑" in texts[0]

    app.tag_search_var.set("")
    root.update()
    assert len(app.tag_tree.get_children()) == 10

    app.tag_store.close()
    app.db.close()
    app.quality_store.close()


def test_quality_ok_filter_shows_only_normal(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    seed_quality_rows(app)

    app.quality_ok_var.set(True)
    app._on_quality_ok_changed()
    assert row_names(app) == {"b.NEF"}

    row_values = {
        app.result_tree.item(iid, "values")[0]: app.result_tree.item(iid, "values")
        for iid in app.result_tree.get_children()
    }
    assert row_values["b.NEF"][7] == "正常"

    # 勾具体问题会自动取消“正常”，回到问题筛选
    app.quality_vars["blurry"].set(True)
    app._on_quality_flag_changed()
    assert app.quality_ok_var.get() is False
    assert row_names(app) == {"a.NEF"}

    app.clear_filters()
    assert row_names(app) == {"a.NEF", "b.NEF", "c.NEF"}
    app.db.close()
    app.quality_store.close()


def test_remove_selected_photos_removes_db_record_only(root, db_path: Path, monkeypatch) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    assert app.db.count_photos() == 3

    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    iid = str(Path("D:/Photos/2026/b.NEF").resolve())
    app.result_tree.selection_set(iid)
    app.remove_selected_photos()

    assert app.db.count_photos() == 2
    assert app.db.get_photo("D:/Photos/2026/b.NEF") is None
    assert row_names(app) == {"a.NEF", "c.NEF"}
    assert app.current is None
    app.db.close()
    app.quality_store.close()


def test_sharpness_sort_button_column_and_restore(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    seed_quality_rows(app)
    # 再加一张未分析照片，应该排最后
    app.db.upsert_photo(make_info(Path("D:/Photos/2026/d.NEF"), taken_at="2026-09-18 22:00:00"))
    app.refresh_results()

    assert app.sort_mode == "default"
    assert row_names_in_order(app) == ["a.NEF", "b.NEF", "c.NEF", "d.NEF"]

    app.btn_sort_quality.invoke()
    assert app.sort_mode == "sharpness"
    assert row_names_in_order(app) == ["b.NEF", "c.NEF", "a.NEF", "d.NEF"]

    values = {
        app.result_tree.item(iid, "values")[0]: app.result_tree.item(iid, "values")
        for iid in app.result_tree.get_children()
    }
    assert values["b.NEF"][8] == "500"
    assert values["c.NEF"][8] == "300"
    assert values["d.NEF"][8] == "—"
    assert "✓" in app.btn_sort_quality.cget("text")

    # 筛选后排序仍然成立
    app.quality_ok_var.set(True)
    app._on_quality_ok_changed()
    assert row_names_in_order(app) == ["b.NEF"]

    app.clear_filters()
    app.btn_sort_quality.invoke()
    assert app.sort_mode == "default"
    assert row_names_in_order(app) == ["a.NEF", "b.NEF", "c.NEF", "d.NEF"]
    app.db.close()
    app.quality_store.close()


def test_sharpness_sort_requires_quality_data(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    app.btn_sort_quality.invoke()
    assert app.sort_mode == "default"
    assert "还没有清晰度数据" in app.status_left.cget("text")
    app.db.close()
    app.quality_store.close()


def test_pick_button_marks_and_unmarks(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    assert str(app.btn_pick.cget("state")) == "disabled"

    iid = str(Path("D:/Photos/2026/a.NEF").resolve())
    app.result_tree.selection_set(iid)
    app._on_result_select()
    assert str(app.btn_pick.cget("state")) == "normal"
    assert app.btn_pick.cget("text") == "标记精选"

    app.btn_pick.invoke()
    tag = app.tag_store.get_by_name(PICK_TAG_NAME)
    assert tag is not None
    assert app.tag_controller.tag_counts[tag.id] == 1
    assert "★精选" in app.result_tree.item(iid, "values")[6]
    assert app.btn_pick.cget("text") == "取消精选"

    app.btn_pick.invoke()
    assert app.tag_controller.tag_counts.get(tag.id, 0) == 0
    assert "★精选" not in app.result_tree.item(iid, "values")[6]
    assert app.btn_pick.cget("text") == "标记精选"

    app.btn_pick.invoke()
    app.undo_last_tag_operation()
    assert app.tag_controller.tag_counts.get(tag.id, 0) == 0
    app.db.close()
    app.quality_store.close()


def test_pick_button_multi_select_adds_missing_and_removes_all(root, db_path: Path) -> None:
    app = PhotoClassifierApp(root, db_path)
    seed_rule_rows(app)
    a = str(Path("D:/Photos/2026/a.NEF").resolve())
    b = str(Path("D:/Photos/2026/b.NEF").resolve())
    c = str(Path("D:/Photos/2026/c.NEF").resolve())

    app.result_tree.selection_set((a, b))
    app._on_result_select()
    app.btn_pick.invoke()
    tag = app.tag_store.get_by_name(PICK_TAG_NAME)
    assert app.tag_controller.tag_counts[tag.id] == 2

    # a 已精选、c 未精选：混合选择应该只补 c，而不是取消 a
    app.result_tree.selection_set((a, c))
    app._on_result_select()
    assert app.btn_pick.cget("text") == "标记精选"
    app.btn_pick.invoke()
    assert app.tag_controller.tag_counts[tag.id] == 3

    app.result_tree.selection_set((a, b, c))
    app._on_result_select()
    assert app.btn_pick.cget("text") == "取消精选"
    app.btn_pick.invoke()
    assert app.tag_controller.tag_counts.get(tag.id, 0) == 0
    app.db.close()
    app.quality_store.close()
