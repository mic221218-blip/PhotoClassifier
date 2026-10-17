# PhotoClassifier V2 Plan — Rule-Based Classification

[English](v2-plan.md) | [简体中文](v2-plan.zh-CN.md)

> Status: **implemented** (2026-10-18; decisions D1–D5 all applied) — M1 rule
> engine, M2 CLI, M3 GUI wiring and M4 docs are complete; 160 tests green.

## 1. Goal & Scope

V2 turns the EXIF data already stored by V1.5 into **automatic classification
tags** — without AI, offline, fully explainable. Every rule is a plain
predicate over `PhotoInfo`; every match produces a human-readable reason.

**In scope (V2):**

- Built-in rule catalog (night / high-ISO / slow shutter / telephoto / wide
  angle / fast lens) with configurable thresholds;
- Rule matching engine that never crashes on missing EXIF;
- "Why matched" explanations, e.g. `ISO 6400 ≥ 3200`;
- CLI: `classify` report + `filter` command;
- GUI: wire the existing filter panel (checkboxes + AND/OR + match count) and
  show auto-tag chips in the results grid and detail panel.

**Out of scope (later versions):**

- Image-content classification (V3.5), image quality analysis (V2.5),
  user-defined tags (V3), export/Lightroom (V5), map view (post-V6).

## 2. Rule Catalog (built-in, V2)

| ID | 名称 / Name | Condition | Default threshold | Missing data behavior |
| --- | --- | --- | --- | --- |
| `night` | 夜景 / Night | capture time inside the night window | 18:00–06:00 (configurable) | no `taken_at` → no match |
| `high_iso` | 高感 / High ISO | `iso ≥ T` | 3200 | null ISO → no match |
| `slow_shutter` | 慢门 / Slow shutter | `exposure_time_s < T` | 1/30 s | null shutter → no match |
| `telephoto` | 长焦 / Telephoto | `focal_length_mm ≥ T` | 70 mm | null focal → no match |
| `wide_angle` | 广角 / Wide angle | `focal_length_mm ≤ T` | 24 mm | null focal → no match |
| `fast_lens` | 大光圈 / Fast lens | `f_number ≤ T` | f/2.8 | null aperture → no match |

Missing EXIF never becomes an error: the rule simply does not match, and the
photo stays in the "all photos" view. This matches the graduated fault
tolerance of V1.5.

**Grouping dimensions** (not rules — filters/grouping in CLI and GUI):

- by date (`taken_at` → `YYYY-MM-DD`), by camera (`camera`), by lens
  (`lens_model`), by location (photos with GPS coordinates).

## 3. Architecture

```
src/rules.py
├── RuleConfig        # thresholds (night window, ISO, shutter, focal, aperture)
├── Rule              # id, label, description, predicate, reason formatter
├── RuleMatch         # rule_id, label, reason (str), value
└── RuleEngine        # default rules + evaluate(info) -> list[RuleMatch]
                      # classify(infos) -> {path: matches}
```

- **No new database tables for built-in rules.** Evaluation is done on the fly
  over the `PhotoInfo` list already loaded by the GUI/CLI. Rationale: always
  fresh, zero migration risk, 10k photos × 6 rules ≈ <100 ms (to be verified).
- **Configurable thresholds** live in `RuleConfig` (defaults above), so later
  settings UI or config file can override without touching code.
- **User-defined rules**: deferred to V2.x (decision **D2**). Storing them
  would need a `rules` table (id, name, kind, params JSON); the schema is
  already sketched in the plan doc but not built now.
- **GUI integration**: the filter panel's tag checkboxes become rule
  checkboxes (夜景 / 高感 / 慢门 / 长焦 / 广角 / 大光圈); AND/OR logic applies;
  match count updates live; detail panel gains a "规则命中 / Rule matches"
  section listing reasons; grid cards show auto-tag chips.

## 4. Data Flow

```
photos table (V1.5)
        │  find_photos_under()
        ▼
PhotoInfo list  ──► RuleEngine.classify()  ──►  {path: [RuleMatch…]}
        │                                        │
        ├──► GUI: filter panel + chips + reasons │
        └──► CLI: classify report / filter list  │
                                                 ▼
                                    results view (virtual filter views)
```

Filtering always produces a **view** — no files are moved, renamed, or
deleted, consistent with the original-photo safety principle.

## 5. Report & Explainability

`classify` report sample:

```
规则分类完成（共 3,000 张）
  夜景  Night        ： 412 张（时间窗 18:00–06:00）
  高感  High ISO     ： 286 张（ISO ≥ 3200）
  慢门  Slow shutter ：  95 张（快门 < 1/30s）
  ...
匹配原因示例 / why:
  DSC_0012.NEF  夜景：拍摄时间 20:15 在夜间时段；高感：ISO 6400 ≥ 3200
```

`filter` output lists matched files with reasons (and supports
`--explain` to print details):

```
python -m src.main filter --rules night,high_iso --and
python -m src.main filter --rules night,telephoto --or --explain
```

## 6. Testing Strategy

- **Per-rule unit tests**: match / no-match / boundary (ISO exactly 3200
  matches `≥`; shutter exactly 1/30 does NOT match `< 1/30`), missing-data
  cases;
- **Reason text** assertions (`ISO 6400 ≥ 3200` etc.);
- **Config override** tests (custom night window, custom thresholds);
- **Integration**: scan folder → `classify` counts; CLI filter output;
  GUI: tick checkbox → row count changes;
- **Performance sanity**: 10,000 synthetic `PhotoInfo` objects, full
  classification under a time budget (e.g. < 500 ms) to keep the GUI snappy.

## 7. Milestones (implementation order after approval)

| Slice | Content | Acceptance |
| --- | --- | --- |
| M1 | `src/rules.py` + unit tests | all rule tests green; boundary cases covered |
| M2 | CLI `classify` + `filter` + report formatting | report matches plan format; `--explain` works |
| M3 | GUI wiring: rule checkboxes, AND/OR, live count, chips, detail reasons | manual check: toggling boxes filters the grid correctly |
| M4 | Docs (README + screenshots), plan-doc status update | bilingual docs updated |

## 8. Decision Record (confirmed 2026-10-18)

| Decision | Outcome |
| --- | --- |
| **D1 Night definition** | **Time window now, upgrade later**: V2 uses a configurable fixed window (default 18:00–06:00); V2.x computes true sunrise/sunset locally from GPS + date |
| **D2 Custom rules** | **Not in V2**: built-in 6 rules + adjustable thresholds; the user-defined rule editor goes to V2.x |
| **D3 Location grouping** | **GPS list + coordinates**: V2 lists photos with GPS and shows/copies coordinates; map view and region naming later |
| **D4 Tag chips** | **Show in V2**: rule matches appear as chips on result cards, to be merged with V3 user tags later |
| **D5 Fast-lens rule** | **Include**: `fast_lens` (f/2.8) is part of V2 |

## 9. Impact on Later Versions

- V3 (tags): rule matches become "auto tags"; the user-tag system adds manual
  tags to the same model — no rework expected.
- V4 (advanced filtering): the AND/OR engine built here is reused and extended
  with numeric conditions on top of tags.
- V2.5 (quality analysis): independent rules module extension
  (blur/exposure/duplicates are image-level, not EXIF-level).
