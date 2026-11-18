from pathlib import Path

from src import photo_export


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

    report = photo_export.export_photos(
        [jpeg, raw, existing, missing, jpeg], destination, progress=lambda *event: progress.append(event),
    )

    assert (report.copied, report.skipped) == (2, 1)
    assert [path for path, _ in report.failures] == [missing]
    assert progress == [(1, jpeg.name), (2, raw.name), (3, existing.name), (4, missing.name)]
    assert (destination / "DSC (3).JPG").read_bytes() == jpeg.read_bytes() == b"JPEG with EXIF"
    assert (destination / raw.name).read_bytes() == raw.read_bytes() == b"NEF with EXIF"
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
    report = photo_export.export_photos([bad, good], destination)

    assert report.copied == 1
    assert report.failures == [(bad, "simulated copy failure")]
    assert list(destination.iterdir()) == [destination / good.name]
    assert (destination / good.name).read_bytes() == good.read_bytes()
    assert bad.read_bytes() == b"original"
