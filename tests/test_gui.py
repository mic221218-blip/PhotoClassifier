"""`src.gui` 的测试（GUI V1.5 外壳）。

在真实 Tk 环境中构建窗口（Windows 桌面会话可用），不弹窗、不进入主循环：
- 文件夹库与结果列表正确显示数据库内容；
- 选中结果行时详情面板联动；
- 直接调用扫描 worker 时，真实完成"扫描 → EXIF → 入库 → 刷新"流程。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from src.gui import PhotoClassifierApp
from src.quality import ANALYZER_VERSION, FLAG_IDS, QualityMeasurement

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
    assert row_values["b.NEF"][7] == ""
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
