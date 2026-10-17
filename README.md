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

## Current Features (V1.5)

### V1: Folder Scanning & Basic NEF Support ✅

- Recursively scans any real-world photo folder (NEF / JPEG / PNG mixed
  together);
- Identifies file types by extension (case-insensitive): NEF, JPEG, PNG, video
  (MOV / MP4, etc.), and unknown formats;
- Videos and unknown formats are recorded as file info only — they never take
  part in photo analysis and never crash the program;
- Scanning is completely read-only; no original file is ever touched.

### V1.5: EXIF & Photo Information ✅ (current release)

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

## Installation & Usage

Requirements: Python 3.12+ (standard library only, no third-party
dependencies).

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

## Running Tests

```bash
python -m pytest
```

The suite contains 92 tests, all based on temporary files synthesized to the
TIFF / EXIF / PNG specifications (see `tests/exif_maker.py`) — no real photos
are needed and no local files are touched.

## Project Structure

```
PhotoClassifier/
├── src/
│   ├── scanner.py       # V1: read-only folder scanner (type detection, fault tolerance)
│   ├── photo_info.py    # V1.5: photo info data structure (file info + EXIF fields)
│   ├── exif_reader.py   # V1.5: zero-dependency EXIF parser (NEF/JPEG/PNG)
│   ├── database.py      # V1.5: SQLite photo information database
│   └── main.py          # V1.5: CLI entry point (scan / show / stats)
├── tests/
│   ├── test_scanner.py      # V1 scanner tests
│   ├── test_exif_reader.py  # EXIF parsing tests (incl. corrupted-file tolerance)
│   ├── test_database.py     # database read/write tests
│   ├── test_main.py         # CLI & end-to-end integration tests
│   └── exif_maker.py        # helper to synthesize NEF/JPEG/PNG test files
├── data/                # local data directory (gitignored; database default location)
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

## Roadmap

| Stage | Scope | Status |
| --- | --- | --- |
| V0 | Project foundation (repo, venv, structure, README) | ✅ Done |
| V1 | Folder scanning & basic NEF support (type detection, fault tolerance, report) | ✅ Done |
| V1.5 | EXIF & photo information (parser, database, CLI) | ✅ Done (current) |
| V2 | Rule-based classification (night / high-ISO / slow shutter / telephoto / wide-angle / by camera & lens, no AI) | 🔜 Next |
| V2.5 | Image quality analysis (blur / exposure anomalies / duplicates — hints only, never auto-delete) | Planned |
| V3 | Multi-tag system (#night #architecture …; one photo, many tags) | Planned |
| V3.5 | Visual recognition (compare traditional CV / local models / AI APIs) | Planned |
| V4 | Smart filtering (AND / OR combos of tags + EXIF conditions) | Planned |
| V5 | Lightroom workflow (reveal in Explorer / batch paths / export to folder) | Planned |
| V6 | GUI | Planned |

Future extensions: more RAW formats (CR3 / ARW / RAF / DNG / ORF / RW2), map
view, timeline, camera & lens statistics, similar-photo clustering, batch
renaming, and more.

## Original-Photo Safety Principles

The entire project must obey:

- Never delete originals by default;
- Never modify RAW files by default;
- Never move originals by default;
- Any operation that moves or copies files (V5 export) requires explicit user
  confirmation.

---

*See the internal design document "PhotoClassifier 项目总体规划与开发路线"
(PhotoClassifier Project Master Plan & Development Roadmap) for the full plan.*
