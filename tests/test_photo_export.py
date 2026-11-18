from pathlib import Path

from src import photo_export


def named_groups(sources, prefix=""):
    return tuple(photo_export.name_export_groups(photo_export.group_export_sources(sources), prefix))


def test_export_copies_originals_and_keeps_existing_names(tmp_path):
    source_folder = tmp_path / "originals"
    source_folder.mkdir()
    destination = tmp_path / "export"
    destination.mkdir()
    jpeg = source_folder / "DSC.JPG"
    raw = source_folder / "DSC.NEF"
    existing = destination / jpeg.name
    jpeg.write_bytes(b"JPEG with EXIF")
    raw.write_bytes(b"NEF with EXIF")
    existing.write_bytes(b"another photo")
    (destination / "DSC (2).JPG").mkdir()
    missing = source_folder / "missing.JPG"
    progress = []

    groups = named_groups([jpeg, raw, existing, missing, jpeg])
    report = photo_export.export_photos(groups, destination, progress=lambda *event: progress.append(event))

    assert [outcome.status for outcome in report.outcomes] == ["copied", "copied", "skipped", "failed"]
    assert report.outcomes[-1].file.source == missing
    assert progress == [(1, jpeg.name), (2, raw.name), (3, existing.name), (4, missing.name)]
    assert (destination / "DSC (3).JPG").read_bytes() == jpeg.read_bytes() == b"JPEG with EXIF"
    assert (destination / "DSC (3).NEF").read_bytes() == raw.read_bytes() == b"NEF with EXIF"
    assert existing.read_bytes() == b"another photo"


def test_failed_copy_cleans_up_partial_file_and_continues(tmp_path, monkeypatch):
    bad = tmp_path / "bad.JPG"
    good = tmp_path / "good.JPG"
    bad.write_bytes(b"original")
    good.write_bytes(b"second original")
    destination = tmp_path / "export"
    destination.mkdir()
    copyfile = photo_export.shutil.copyfile

    def copy_with_error(source, target):
        if source == bad:
            Path(target).write_bytes(b"partial")
            raise OSError("simulated copy failure")
        return copyfile(source, target)

    monkeypatch.setattr(photo_export.shutil, "copyfile", copy_with_error)
    report = photo_export.export_photos(named_groups([bad, good]), destination)

    assert [outcome.status for outcome in report.outcomes] == ["failed", "copied"]
    assert report.outcomes[0].file.source == bad
    assert report.outcomes[0].reason == "simulated copy failure"
    assert list(destination.iterdir()) == [destination / good.name]
    assert (destination / good.name).read_bytes() == good.read_bytes()
    assert bad.read_bytes() == b"original"


def test_sequence_uses_one_number_per_pair_and_skips_pair_on_conflict(tmp_path):
    raw, jpg, png = (tmp_path / name for name in ("DSC.NEF", "dsc.jpg", "DSC.PNG"))
    for source in (raw, jpg, png):
        source.write_bytes(source.suffix.encode())
    groups = named_groups([raw, png, jpg], "精选")
    assert [[file.name for file in group] for group in groups] == [["精选_001.NEF", "精选_001.jpg"], ["精选_002.PNG"]]
    destination = tmp_path / "export"
    destination.mkdir()
    existing = destination / "精选_001.jpg"
    existing.write_bytes(b"existing")

    report = photo_export.export_photos(groups, destination, skip_existing=True)

    assert [outcome.status for outcome in report.outcomes] == ["skipped", "skipped", "copied"]
    assert sorted(path.name for path in destination.iterdir()) == ["精选_001.jpg", "精选_002.PNG"]
    assert existing.read_bytes() == b"existing"


def test_cancel_finishes_current_pair_and_retry_preserves_assigned_name(tmp_path):
    raw, jpg, later = (tmp_path / name for name in ("pair.NEF", "pair.JPG", "later.PNG"))
    raw.write_bytes(b"original raw")
    later.write_bytes(b"other photo")
    destination = tmp_path / "export" / "精选"
    destination.parent.mkdir()
    processed = []
    groups = named_groups([raw, jpg, later], "album")

    report = photo_export.export_photos(
        groups, destination, create_subfolder=True, cancelled=lambda: bool(processed),
        progress=lambda _done, name: processed.append(name),
    )

    assert report.cancelled
    assert processed == ["pair.NEF", "pair.JPG"]
    assert [outcome.status for outcome in report.outcomes] == ["copied", "failed"]
    assert (destination / "album_001.NEF").read_bytes() == raw.read_bytes()
    assert not (destination / "album_002.PNG").exists()

    jpg.write_bytes(b"original jpeg")
    retry = photo_export.export_photos(((report.outcomes[1].file,),), destination, keep_names=True)

    assert [outcome.status for outcome in retry.outcomes] == ["copied"]
    assert sorted(path.name for path in destination.iterdir()) == ["album_001.JPG", "album_001.NEF"]
    assert (destination / "album_001.JPG").read_bytes() == jpg.read_bytes()
