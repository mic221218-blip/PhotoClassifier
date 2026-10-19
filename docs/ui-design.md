# PhotoClassifier UI Design (V6 brought forward)

[English](ui-design.md) | [简体中文](ui-design.zh-CN.md)

> Status: **draft for confirmation** — this is a visual design only. Buttons do
> not need to be functional yet; each control is tagged with the version that
> wires it up. No code is implemented in this step.

## 1. Goals

Per the roadmap re-order, the GUI design happens **now**, before V2 rule
classification. The design targets the final shape of the main window so that
every later stage (tags, filters, export) slots into a stable layout, while
the first implementable version only needs to wire up what V1.5 can already
do (scan, browse, show info).

Design principles carried over from the project plan:

- **Read-only by default.** There is *no delete button anywhere* in the UI —
  this is deliberate, mirroring the original-photo safety principle.
- **Progressive disclosure.** Controls that belong to later versions are
  visible but clearly badged ("V3 placeholder" etc.), so the layout never has
  to be rearranged later.
- **Dark theme.** Consistent with photo-editing workflows (Lightroom-like).
- **Chinese-first UI**, with all strings centralized so an English
  localization can be added later.

## 2. Main Window Layout

![Main window mockup](ui-images/ui-main.png)

![Annotated layout](ui-images/ui-main-annotated.png)

| # | Region | Contents | Wired up in |
| --- | --- | --- | --- |
| 1 | **Toolbar** | Folder path input + `Browse…` + `Start Scan`; hint "read-only scan, original files are never touched" | V1.5 (now) |
| 2 | **Library & filter panel** (left) | **Folder library**: every imported folder with photo count and a checkbox — checked folders are scanned together; `+ Add Folder`. Below: **auto tags (EXIF rules, V2 wired)** — night / high ISO / slow shutter / telephoto / wide angle / fast lens, filtering live as you tick; "only photos with GPS"; AND / OR combination; `Clear filters`; numeric conditions (V4 placeholder) | Folder library = V1.5; rule tags = V2 (wired); parameters = V4 — placeholder |
| 3 | **Results grid** (center) | Thumbnail cards: preview, file name, one-line EXIF summary (aperture · shutter · ISO), tag chips; grid/list view toggle; single-click selects, double-click reveals the file in Explorer | Grid with file info = V1.5 (now); thumbnails & tag chips = V3+ |
| 4 | **Detail panel** (right) | Large preview, file name + format/status badges, size & modified time, full EXIF table (capture time, camera, lens, focal length, aperture, shutter, ISO, exposure compensation, GPS, dimensions), EXIF status; actions: `Reveal in Explorer`, `Copy Path` (both V1.5); `Export…` and `Import to Lightroom` (disabled, V5) | V1.5 (now) for info + first two actions |
| 5 | **Status bar** (bottom) | Last scan time, library-wide totals: total files, succeeded (green), errors (orange) + reassurance note, database location | V1.5 (now) |

## 3. Interaction States

### Scanning

![Scanning state](ui-images/ui-scanning.png)

While a scan runs, the results area switches to a progress view: which folder
is being scanned (`folder 1 / 3`), progress bar with `processed / total · %`,
the current file name, a live count of successes vs. errors (errors are logged
and skipped, never fatal), and a `Cancel` button. The `Start Scan` button
becomes a disabled "Scanning…". All folders checked in the library are scanned
in sequence.

### Empty state (first launch)

Not shown in the mockups: the grid area shows a friendly explanation
("Choose a folder and start scanning — the scan is read-only") with the same
toolbar enabled. Trivial to add at implementation time.

### Error display

Files that fail to parse appear in the grid with an orange "error" badge and,
in the detail panel, the exact error message from `exif_reader` — matching the
graduated fault tolerance of V1.5 (corrupted / no-EXIF / non-photo).

## 4. Feature Phasing (what works when)

| Stage | What becomes clickable |
| --- | --- |
| GUI V1.5 (first implementation after this design is confirmed) | Folder library (add folders, choose which to scan, multi-folder scans), scan + live progress, results grid (file info), detail panel, reveal in Explorer, copy path, status-bar report, stats dialog |
| V3 | Tag checkboxes, tag chips on cards, tag-based filtering |
| V4 | ISO / focal length / shutter conditions, AND / OR logic, `Apply Filter` |
| V5 | `Export…` dialog (copy-only, explicit confirmation), `Import to Lightroom`, batch selection |
| V6+ | List view polish, preferences, localization (English UI) |

## 5. Technology Decision

**Target framework: PySide6 (Qt)** — decided on 2026-10-18. Mature desktop
widgets, high-DPI support, good CJK text rendering, model/view for large photo
lists, easy packaging with PyInstaller. It will be added as an optional
dependency so the core stays dependency-free.

> **Implementation note (2026-10-18, GUI V1.5 shell)**: the current development
> environment cannot reach PyPI, so the first working shell is implemented with
> the **standard-library Tkinter**, following the same layout and interactions
> as this design. The database and scan layers are UI-agnostic and fully
> reusable; migration to PySide6 happens when the environment allows installing
> it. This is a pragmatic call under environment constraints, not a reversal of
> the decision.

## 6. Open Questions (for later stages)

1. **NEF thumbnails** — NEF files embed a JPEG preview; extracting it is
   likely doable with the same zero-dependency approach, to be researched at
   GUI-implementation time. Until then cards can show a format-colored
   placeholder.
2. **Window chrome** — mockups draw a custom title bar; the implementation may
   simply use the OS title bar (simpler, recommended for V1 of the GUI).
3. **Exact grid density / card size** — to be tuned with real photo libraries.

## 7. Next Steps

1. User confirms this design (or requests changes).
2. Commit the design doc + mockups.
3. Implement the GUI shell in PySide6, wiring only V1.5 capabilities; later
   stage controls stay disabled with badges.

## 8. Changelog

- r6 (2026-10-20): V3 tags — "My tags" in the left panel (search + checkbox list +
  photo counts + manager entry); user tags merged into the "Auto tags" column with
  a ★ prefix at the front, with small column-width adjustments; a chip editor
  (input + autocomplete) in the detail panel; right-click batch add/remove with
  Ctrl+Z undo; a tag manager dialog (create/rename/delete).
- r5 (2026-10-19): V2.5 wiring — six "Quality analysis" checkboxes in the left
  panel (sharing AND/OR with auto tags); a "Quality" column in the result table;
  a "Quality analysis" block in the detail panel (sharpness, contrast,
  luminance, clipping, duplicate group) placed before "Rule matches"; a
  "Quality analysis" toolbar button (background thread + progress + cancel +
  incremental); default window enlarged to 1660×900, left/right panels scroll
  when content overflows, and the action buttons are pinned to the bottom of
  the detail panel.
- r4 (2026-10-18): filter panel wired to the V2 rule engine (6 EXIF rules,
  live filtering, AND/OR, GPS-only, clear); results gain an "Auto tags"
  column; the detail panel lists rule-match reasons.
- r3 (2026-10-18): **GUI V1.5 shell implemented** (Tkinter — PySide6 cannot be
  installed in this environment; layout and interactions follow this design):
  folder library, background scan with progress + cancel, results list, detail
  panel, reveal in Explorer / copy path; filter / export / Lightroom stay as
  disabled placeholders. The desktop shortcut points at `run_gui.pyw`.
- r2 (2026-10-18): added **folder library** to the left panel (multi-folder
  browsing and multi-folder scans, wired at GUI V1.5); added **Import to
  Lightroom** action next to Export (V5 placeholder); framework decision made
  (PySide6).
- r1 (2026-10-18): initial design.
