# PhotoClassifier

[English](README.md) | [简体中文](README.zh-CN.md)

A smart RAW photo organizing and filtering tool for photography workflows.

A single shoot easily produces hundreds or thousands of photos: RAW files, JPEGs,
and videos mixed together, plus corrupted files and duplicates. The traditional
"folder by date" approach simply cannot answer questions like:

> "I want to edit all my night-scene architecture photos right now."

PhotoClassifier's goal: **scan and understand photo information (EXIF / file
metadata / image analysis in future stages), so photographers can quickly find
the photos they want to work on** — and then hand them off to Lightroom or other
post-processing software.

## Project Positioning

- PhotoClassifier is **not a Lightroom replacement** — it is the photo analysis
  and filtering tool that runs **before** Lightroom;
- It **never moves, copies, or modifies original photos** — classification is
  "virtual", stored in a database; the RAW files on disk always stay exactly
  where they are;
- Core workflow: `camera → photo folder → PhotoClassifier (scan + analyze +
  filter) → Lightroom (post-processing)`.

## Core Design Principles

1. **One photo ≠ one category.** A night-scene architecture photo can be
   `#night`, `#architecture`, and `#city` at the same time, so PhotoClassifier
   uses a "multi-tag / multi-attribute" model instead of dropping each photo
   into a single folder.
2. **Original files are kept exactly once.** Tags and attributes live in the
   database; RAW files are never duplicated across categories.
3. **Original-photo safety first.** By default the tool only
   "reads → analyzes → builds the database → filters". It never deletes,
   modifies, or moves originals on its own.
4. **A single bad file must never take down the whole job.** Photo #1532 of
   3000 is corrupted? Log the error, skip it, keep going — and produce a full
   report at the end.
5. **AI is an optional, pluggable enhancement — not a core dependency.**
   Whatever EXIF and rule-based logic can solve, we solve without AI; visual
   recognition will be added later as needed.

## Current Features (V3)

### V1: Folder Scanning & Basic NEF Support ✅

- Recursively scans any real-world photo folder (NEF / JPEG / PNG mixed
  together);
- Identifies file types by extension (case-insensitive): NEF, JPEG, PNG, video
  (MOV / MP4, etc.), and unknown formats;
- Videos and unknown formats are recorded as file info only — they never take
  part in photo analysis and never crash the program;
- Scanning is completely read-only; no original file is ever touched.

### V1.5: EXIF & Photo Information ✅

- **Zero-dependency EXIF parser** (`src/exif_reader.py`): parses NEF (TIFF
  structure) and JPEG (APP1 segment) directly, reading —
  - capture time, camera (make + model), lens model
  - focal length, aperture, shutter speed, ISO, exposure compensation
  - GPS coordinates (decimal degrees)
  - image dimensions
  - both big-endian and little-endian TIFF are supported;
- **SQLite photo information database** (`src/database.py`): keyed by absolute
  file path; repeated scans update records instead of duplicating them — no
  third-party dependencies;
- **Graduated fault tolerance**: the tool distinguishes "file corrupted"
  (flagged as an error), "no EXIF" (processed normally, just missing data),
  and "non-photo file" (file info only);
- **Command-line tool** (`src/main.py`):
  - `scan`: scan a folder → read EXIF → write the database → print a scan
    report;
  - `show`: view photo records in the database (fuzzy match by file name or
    path);
  - `stats`: database statistics (by file type / EXIF status / camera / lens).

### V2: Rule-Based Classification ✅

- **6 built-in EXIF rules** (all thresholds configurable via `RuleConfig` in
  `src/rules.py`):
  - Night: capture time inside the night window (default 18:00–06:00)
  - High ISO: ISO ≥ 3200; Slow shutter: slower than 1/30 s; Telephoto: focal
    length ≥ 70 mm
  - Wide angle: focal length ≤ 24 mm; Fast lens: aperture ≤ f/2.8
- **Explainable**: every match carries a reason with actual values, e.g.
  `ISO 6400 ≥ 3200`, `焦距 120 mm ≥ 70 mm`;
- **Missing EXIF never errors**: rules simply do not match; the photo stays in
  the all-photos view;
- **One photo can match several rules** (the "one photo, many tags" idea);
- **CLI**: `classify` (per-rule report) + `filter` (rule-based filtering with
  AND/OR, `--explain` for reasons, `--gps` for GPS-only photos);
- **GUI**: the filter panel checkboxes are wired to the rule engine and filter
  the results list live; results gain an "Auto tags" column; the detail panel
  shows "Rule matches" with reasons; a "only photos with GPS" filter is
  available.

### V2.5: Image Quality Analysis ✅

The first stage that **reads pixels**. `analyze` stores measurements in the
database, while verdicts are still derived live — changing a threshold never
requires re-decoding an image.

- **Pixel source**: NEF uses its **embedded JPEG preview** (IFD0 / SubIFD,
  6048×4032 in practice); JPEG / PNG are decoded directly. **No RAW decoding**,
  so libraw / rawpy is not needed;
- **Six abilities**: blur, motion blur, overexposure, underexposure, black
  frames, duplicate photos;
- **Explainable**: every flag carries numbers, e.g.
  `sharpness 116 < 120 (Laplacian variance, 512px)` or
  `highlight clipping 21% ≥ 12%`;
- **Duplicates**: exact (same size + a head/tail 256 KiB fingerprint first, full
  SHA-256 only on collisions) and near (dHash ≤ 8 within a 10 s capture window);
- **Timelapse / burst sequences**: long regular runs (gap ≤ 12 s, ≥ 20 frames,
  ≥ 60 s) are recognised from capture rhythm; similar frames inside a sequence
  are reported but **never flagged as duplicates** — a folder that contains such
  a sequence is exempt by default (`--strict` disables this). This rule came out
  of calibrating on real timelapse footage;
- **Same-name RAW+JPEG is not a duplicate** (two formats of one photo, identical
  dHash);
- **CLI**: `analyze` (analysis + report + `--csv` export for calibration),
  `quality` (statistics and flagged list), `dupes` (duplicate groups,
  `--sequences` for sequences), `filter --quality` (shares AND/OR with rules);
- **GUI**: six quality checkboxes in the filter panel, a "Quality" column in the
  results, a "Quality analysis" block in the detail panel (sharpness, luminance,
  clipping, duplicates and reasons), and a toolbar "Quality analysis" button
  (background thread + progress + cancel + incremental);
- **Optional dependency**: quality analysis needs Pillow
  (`requirements-quality.txt`); without it, scanning, EXIF and rule-based
  classification/filtering keep working unchanged.

### V3: Multi-Tag System ✅ (current release)

Every photo can carry any number of **flat tags**: rules and quality remain
system tags (derived live), while user tags are stored in SQLite. Both are
presented and filtered through the same layer.

- **Data model**: new `tags` + `photo_tags` (schema `1 → 2`, additive migration);
  case-insensitive unique names, generated colours, deleting a tag cascades its
  assignments (photos are never deleted);
- **Single-photo editing**: a "My tags" block in the detail panel with coloured
  chips, an input field and autocomplete; × removes a tag; new names are created
  after confirmation;
- **Batch**: multi-select rows + context menu (Open Photo / Reveal in Explorer /
  Add tag… / Remove tag… / Copy Path / **Remove from library…**), count
  confirmation, and **Ctrl+Z** single-step undo; the Delete key removes selected
  records too (database only, files are never deleted);
- **Filtering**: "My tags" in the left panel with search, checkboxes and photo
  counts; it shares the same AND/OR as rules and quality (single implementation
  in `src/filtering.py`);
- **Display**: user tags come first in the result column with a `★` prefix,
  e.g. `★architecture ｜ night · slow shutter` (so long system tags cannot hide
  them); the detail panel shows coloured chips;
- **Manager**: create / rename / delete dialog; deletion shows the affected count;
- **Preset tags**: 10 common tags are seeded once on upgrade (Landscape /
  Architecture / Night / People / Sky / Water / City / Sport / Flowers / Sunset);
  they are not assigned automatically and can be deleted;
- **CLI**: `tags`, `tag add/remove/list/rename/delete`, `filter --tags`; `show`
  prints a photo's tags.

### GUI Shell (GUI V1.5) ✅

- **Folder library**: import multiple folders, tick the ones to scan, scan
  them one after another;
- **Background scanning**: progress bar + current file + live success/error
  counts, cancellable at any time;
- **Results list**: file name, type, capture time, camera, ISO, shutter, size;
- **Detail panel**: full photo information (same fields as the database) +
  `Open Photo` (Windows Photos), `Reveal in Explorer` and `Copy Path`;
- **Double-click a result row** to open the photo in Windows Photos (it no
  longer just reveals the containing folder);
- **Quality analysis**: the "Quality analysis (V2.5)" checkboxes (blur / motion
  blur / overexposed / underexposed / black frame / duplicate) share the same
  AND/OR logic as auto tags; a "Quality" column, and a "Quality analysis" block
  in the detail panel (sharpness, contrast, luminance, clipping, duplicate
  group);
- **Sharpness sort (P1)**: a "Sharpness first" button in the result header sorts
  by `blur_score` high to low; a numeric "Sharpness" column; unanalyzed / video /
  failed photos go last; the sort applies to the current view and is not persisted
  after closing the app;
- **My tags (V3)**: searchable checkbox list with photo counts on the left;
  chip editing in the detail panel; right-click batch add/remove with single-step
  undo; a tag manager dialog;
- **Placeholders**: parameter conditions (V4), export / import to Lightroom (V5)
  are drawn but disabled until their versions land;
- Default window 1660×900; the left and right panels scroll with the mouse wheel
  when their content is taller than the window;
- Launch: the desktop shortcut (pointing at `run_gui.pyw`), or
  `python -m src.gui` / `pythonw run_gui.pyw`.

## Installation & Usage

Requirements: Python 3.12+. Core features use the standard library only;
**image quality analysis needs the optional Pillow**:

```bash
pip install -r requirements-quality.txt
```

Without Pillow, `analyze` prints installation guidance and everything else keeps
working.

```bash
# 1. Create and activate a virtual environment (Windows PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Scan a real photo folder (database defaults to data/photos.db)
python -m src.main scan "D:\Photos\2026"

# 3. Show the full information of a specific photo
python -m src.main show --match DSC_0012

# 4. Show statistics
python -m src.main stats

# 5. Rule classification report (V2)
python -m src.main classify

# 6. Filter by rules (V2): night AND high ISO, with explanations
python -m src.main filter --rules night,high_iso --explain

# 7. Night OR telephoto; or only photos with GPS
python -m src.main filter --rules night,telephoto --or
python -m src.main filter --gps

# 8. Analyse image quality (incremental; read-only)
python -m src.main analyze

# 9. Quality statistics; only blurry photos with reasons
python -m src.main quality
python -m src.main filter --quality blurry --explain

# 10. Duplicate groups (with timelapse/burst sequences)
python -m src.main dupes --sequences

# 11. Create / update the desktop shortcut (Windows)
python tools/install_shortcut.py

# 12. User tags (V3)
python -m src.main tags
python -m src.main tag add --tag Architecture --tag "To retouch" "D:\Photos\2026\DSC_0012.NEF"
python -m src.main tag add --tag Architecture --match jinjihu
python -m src.main tag list --tag Architecture
python -m src.main filter --tags Architecture
python -m src.main filter --rules night --tags Architecture
python -m src.main filter --quality-ok
python -m src.main tag rename "To retouch" "To refine"
python -m src.main tag delete "To refine" --yes
```

> Note: the CLI currently prints reports in Chinese; localization is planned
> for a later stage. The samples below show actual program output.

`scan` output report (following the fault-tolerance design in the project
plan):

```
扫描完成
根目录：D:\Photos\2026
总文件：9
成功处理：
  NEF：3（含 EXIF：3）
  JPEG：2（含 EXIF：1）
  PNG：1
跳过：
  视频：1
  其他：1
异常：
  NEF：1
照片信息数据库：data\photos.db（本次写入 9 条，合计 9 条）
任务完成。
```

Actual `analyze` output (2026-10-19, a 1,375-file timelapse folder):

```
质量分析完成（候选 1,375 张）
  本次分析：1,368 张（跳过：非照片 7）
  预览来源：内嵌 JPEG 667 · 原图 701 · 无预览 0
  耗时：81.0 秒（16.9 张/秒，6 线程）
  模糊 38 · 运动模糊 2 · 过曝 0 · 欠曝 0 · 黑屏 0
  重复：0 组（精确 0 / 近似 0，涉及 0 张）；延时/连拍序列 31 组（不标记为重复）
  结果已写入：data\photos.db（photo_quality 表）
```

`show` output for a single photo:

```
DSC_0001.NEF  [NEF]
  路径：D:\Photos\2026\DSC_0001.NEF
  大小：384 B
  修改时间：2026-10-17 22:38:25
  拍摄时间：2026-09-18 20:15:32
  相机：NIKON Z6III
  镜头：NIKKOR Z 24-120mm f/4 S
  焦距：120mm
  光圈：f/4
  快门：1/250s
  ISO：1600
  曝光补偿：-0.3 EV
  GPS：31.230425, 121.473701
  尺寸：6048 x 4024
  EXIF：已读取
```

> The `data/` directory is in `.gitignore`: the database and any real photo
> data stay on your machine and are never uploaded to GitHub.

### Troubleshooting: "Reveal in Explorer" shows 0x80070005 / 0xc0000142

If the project folder carries a Windows **Low integrity label** (left behind by a
sandbox), the `pythonw.exe` started from it also runs at Low; Windows then refuses
its request to make Explorer select a file (`0x80070005`, access denied) or to
start `explorer.exe` (`0xc0000142`).

Run this once:

```bash
python tools/install_shortcut.py
```

It changes the integrity level of `.venv\pythonw.exe` / `.venv\python.exe` to
**Medium** and recreates the shortcut. **Restart the GUI** and "Reveal in Explorer"
will open the folder with the file selected. If it still fails, the app copies the
path to the clipboard so you can paste it into Explorer's address bar.

## Running Tests

```bash
python -m pytest
```

The suite contains **325 tests**. Scanning, EXIF, rules, database and CLI tests
use temporary files synthesized to the TIFF / EXIF / PNG specifications
(`tests/exif_maker.py`); quality tests draw small images with Pillow
(`tests/image_maker.py`) and cover NEF embedded previews, blur/exposure,
duplicate grouping and timelapse-sequence exemption. GUI tests build the real
window off-screen without entering the main loop. Without Pillow, the
decode-dependent cases skip automatically and the rest still run.

## Project Structure

```
PhotoClassifier/
├── src/
│   ├── scanner.py       # V1: read-only folder scanner (type detection, fault tolerance)
│   ├── photo_info.py    # V1.5: photo info data structure (file info + EXIF fields)
│   ├── exif_reader.py   # V1.5: zero-dependency EXIF parser (NEF/JPEG/PNG)
│   ├── database.py      # V1.5: SQLite photo information database (incl. folder library)
│   ├── rules.py         # V2: EXIF rule engine (6 built-in rules, configurable)
│   ├── quality.py       # V2.5: quality metrics + verdicts (pure, no Pillow)
│   ├── preview.py       # V2.5: pixel source (NEF preview / JPEG / PNG; Pillow optional)
│   ├── duplicates.py    # V2.5: exact/near duplicates + timelapse sequences (pure)
│   ├── quality_store.py # V2.5: photo_quality table persistence
│   ├── quality_scan.py  # V2.5: analysis pipeline (incremental / concurrent / hashing / report)
│   ├── tags.py          # V3: tag normalization / colours / presets (pure)
│   ├── tag_store.py     # V3: tags / photo_tags persistence (batch writes)
│   ├── tag_controller.py# V3: tag cache / batch ops / single-step undo (pure)
│   ├── tag_cli.py       # V3: tags / tag subcommands
│   ├── filtering.py     # V3: unified rules + quality + tags + GPS filtering
│   ├── paths.py         # unified path keys (no resolve in hot paths)
│   ├── formatting.py    # shared display formatting (CLI / GUI / rules)
│   ├── main.py          # CLI entry + command dispatch table
│   ├── ui_theme.py      # GUI theme / columns / pure display helpers
│   ├── ui_panels.py     # GUI layout builders (toolbar / panels / result table)
│   ├── ui_detail.py     # detail panel refresh + tag chip editor
│   ├── ui_tag_dialogs.py# tag picker / batch ops / tag manager dialogs
│   └── gui.py           # GUI entry + state orchestration (Tkinter)
├── tests/
│   ├── test_scanner.py      # V1 scanner tests
│   ├── test_exif_reader.py  # EXIF parsing tests (incl. corrupted-file tolerance)
│   ├── test_database.py     # database read/write tests
│   ├── test_rules.py        # rule engine tests (boundaries + performance baseline)
│   ├── test_quality.py      # metric & verdict tests (pure-Python grayscale)
│   ├── test_preview.py      # NEF preview / decoding / fault tolerance (needs Pillow)
│   ├── test_duplicates.py   # exact / near duplicates / timelapse sequences
│   ├── test_quality_store.py# quality table + schema migration tests
│   ├── test_quality_cli.py  # analyze / quality / dupes / filter --quality tests
│   ├── test_tags.py         # tag name normalization / colour tests
│   ├── test_tag_store.py    # tag CRUD / cascade / migration tests
│   ├── test_filtering.py    # unified filter matrix tests
│   ├── test_tag_cli.py      # tags / tag / filter --tags tests
│   ├── test_tag_controller.py # tag cache / batch / undo tests
│   ├── test_paths.py        # path key tests
│   ├── test_perf_regressions.py # resolve hot-path / WAL regression tests
│   ├── test_main.py         # CLI & end-to-end integration tests
│   ├── test_gui.py          # GUI shell tests (incl. rule and quality filtering)
│   ├── exif_maker.py        # helper to synthesize NEF/JPEG/PNG test files
│   └── image_maker.py       # helper to draw test images (Pillow)
├── tools/
│   └── install_shortcut.py  # create/update the desktop shortcut (incl. icon)
├── assets/              # application icon (logo, ico)
├── data/                # local data directory (gitignored; database default location)
├── run_gui.pyw          # GUI launcher (target of the desktop shortcut)
├── requirements-quality.txt # optional dependency (Pillow, quality analysis only)
├── pytest.ini
├── README.md            # this file (English)
└── README.zh-CN.md      # Chinese README
```

## Technical Decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| EXIF reading | Minimal in-house parser on the standard library | NEF is essentially TIFF and JPEG EXIF lives in APP1; all needed fields are standard tags. Zero dependencies, fully offline, and it can precisely distinguish "corrupted" from "no EXIF" |
| Database | SQLite (stdlib `sqlite3`) | Single file, zero configuration, sufficient; absolute-path primary key supports de-duplicated rescans |
| Platform | Windows first | The real-world scenario for stage one; the code uses no platform-specific APIs |
| AI | Optional & pluggable | EXIF + rules first; visual recognition (V3.5) will compare traditional CV / local models / cloud APIs |
| Quality pixel source | NEF embedded JPEG preview; no RAW decoding | The preview is enough for sharpness, exposure and similarity, is an order of magnitude faster than full RAW decoding, and avoids binary dependencies such as libraw |
| Quality storage | Store **measurements only**, derive verdicts live | Same pattern as V2 "rules are never stored": threshold changes need no pixel re-read; `photo_quality` is a separate table (zero migration risk) |
| Image decoding | Pillow as an **optional dependency**, only in `preview.py` | Mature, wheel-only, supports JPEG `draft()` downscaling; the core works without it |
| Duplicates | Exact (full hash) + near (dHash + time window) + sequence exemption | In real timelapse footage, similar consecutive frames are normal; same-name RAW+JPEG pairs are two formats of one photo |
| Tag storage | User tags stored; system tags (rules/quality) derived live | Threshold changes take effect immediately and never leave stale tags; V3.5 model tags can use the same table |
| Tag structure | Flat `tags` + `photo_tags` | Do "one photo, many tags" properly first; hierarchy/expressions belong to V4 |
| Tag filtering | Rules + quality + tags share `FilterSelection` | GUI and CLI behave identically; V4 only extends the selection with numeric conditions |
| Tag persistence | Database only, no EXIF/XMP writes | Original-photo safety; keyword/XMP export belongs to V5 |

## Roadmap

| Stage | Scope | Status |
| --- | --- | --- |
| V0 | Project foundation (repo, venv, structure, README) | ✅ Done |
| V1 | Folder scanning & basic NEF support (type detection, fault tolerance, report) | ✅ Done |
| V1.5 | EXIF & photo information (parser, database, CLI) | ✅ Done |
| V2 | Rule-based classification (night / high-ISO / slow shutter / telephoto / wide-angle / fast lens; CLI + GUI filtering) | ✅ Done |
| V2.5 | Image quality analysis (blur / motion blur / exposure anomalies / black frames / duplicates / timelapse sequences; CLI + GUI, hints only, never auto-delete) | ✅ Done |
| V3 | Multi-tag system (user tags + rule/quality system tags, unified filtering, batch and undo; CLI + GUI) | ✅ Done (current) |
| V3.5 | Visual recognition (compare traditional CV / local models / AI APIs) | Planned |
| V4 | Smart filtering (AND / OR combos of tags + EXIF conditions) | Planned |
| V5 | Lightroom workflow (reveal in Explorer / batch paths / export to folder) | Planned |
| V6 | GUI (pulled forward: shell implemented — folder library, scan progress, detail panel; Tkinter for now, PySide6 to be evaluated when the environment allows) | 🔨 Shell implemented |

Future extensions: more RAW formats (CR3 / ARW / RAF / DNG / ORF / RW2), map
view, timeline, camera & lens statistics, similar-photo clustering, batch
renaming, and more.

### Measured performance (2026-10-19, Nikon Z6III timelapse footage)

| Item | Result |
| --- | --- |
| Data | 1,375 files (667 NEF + 701 JPEG + 7 videos), 1,368 photos, 17.9 GB |
| First analysis | 81 s (6 workers, ~16.9 photos/s) |
| Incremental re-run | 0.2 s (everything up to date, no re-decoding) |
| Preview source | embedded JPEG 667 · full image 701 · failures 0 |
| Flags | blur 38 · motion blur 2 · over/under-exposed 0 · black 0 · 0 duplicate groups · 31 sequences (1,292 photos) |

> Manual spot checks confirmed the 38 blurry frames are real (one accidental
> shot of a chair back; the rest clearly soft at slow shutter speeds), and the
> timelapse frames were not misreported as duplicates.

V3 / P0 performance (10,000 photos / 50,000 assignments, synthetic benchmark):

| Item | Result |
| --- | --- |
| Tag assignment cache + path mapping | ~114 ms |
| Rules + tags filtering over 10,000 photos | ~11 ms |
| Tag counts (GROUP BY) | ~4 ms |
| Tag one tag onto 1,000 photos | ~21 ms |
| Insert 50,000 assignments in batches | ~22 ms |
| GUI filter refresh (1,375 rows) | ~40 ms |

> Key optimizations: database paths are already absolute, so the filtering hot
> path no longer calls `Path.resolve()` per photo (a GUI refresh used to spend
> ~250 ms of 260 ms there); batch tag writes use `executemany` (50k assignments
> dropped from ~2.9 s to ~22 ms).

## Original-Photo Safety Principles

The entire project must obey:

- Never delete originals by default;
- Never modify RAW files by default;
- Never move originals by default;
- Any operation that moves or copies files (V5 export) requires explicit user
  confirmation;
- "Remove from library" deletes database records only (photo, quality and tag
  links); **files on disk are never deleted**, and a rescan brings them back.

---

*See the internal design document "PhotoClassifier 项目总体规划与开发路线"
(PhotoClassifier Project Master Plan & Development Roadmap), plus the per-stage
plans in `docs/v2-plan.md` and `docs/v2.5-plan.md`.*
