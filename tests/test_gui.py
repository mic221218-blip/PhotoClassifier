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
