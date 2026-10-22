# PhotoClassifier V3 Plan — Multi-Tag System

[English](v3-plan.md) | [简体中文](v3-plan.zh-CN.md)

> Status: **implemented and accepted (2026-10-20)**. This document preserves the
> original plan; deviations and measurements are in §16.
>
> Baseline: V2.5 is complete and accepted (quality analysis, duplicate detection,
> GUI wiring), **249 tests green**, schema version `1`, 1,375 files in the sample
> library.
>
> Upstream reference: V3 "multi-tag system" in 《PhotoClassifier 项目总体规划与开发路线》
> only gives the direction (one photo, many tags; `#night #architecture`). This plan
> fills in the data model, interaction, filtering and acceptance criteria against the
> **actual code**.

---

## 1. Goal and scope

### 1.1 One sentence

Let every photo carry **any number of flat tags**: both system-derived tags
(night / high ISO / blurry / duplicate) and **user-created, editable tags**
(architecture, sky, family, to-retouch…), and filter by tag combinations.

### 1.2 Why V3 is necessary

V2 rules and V2.5 quality flags are **derived** results: computed live from EXIF or
pixels. Users cannot add, rename, or express "this is the photo I want".

Real needs:

- "From this shoot, find everything that is **architecture** and **sky**" — rules cannot;
- "These are **for the client**, those are **for myself**" — manual annotation only;
- "Rough pass first: mark the 30 keepers as **to-retouch**" — needs a persistent layer.

V3 adds user tags and puts tags, rules and quality into **one filter**. It is the
data foundation for V4 (smart filtering) and V5 (Lightroom / XMP export).

### 1.3 Scope

| Capability | Input | Output | V3 |
| --- | --- | --- | --- |
| Tag library | user input | `tags` table (name, color, created) | ✅ |
| Tag photos | one / many photos | `photo_tags` assignment (idempotent) | ✅ |
| Remove tag | photo + tag | delete assignment, never the photo | ✅ |
| Rename / delete tag | tag | update name / delete all assignments | ✅ |
| Single-photo editing | detail panel | chips + input + autocomplete | ✅ |
| Batch editing | multi-selection | context menu + confirm + single-step undo | ✅ |
| Tag filtering | left list + search | shares AND/OR with rules/quality | ✅ |
| Display | result column / detail | ★ prefix + coloured chips | ✅ |
| Tag statistics | database | photo count per tag (including 0) | ✅ |
| Tag colours | name hash | generated and persisted | ✅ |
| Tag hierarchy / groups | — | — | ❌ (V4+) |
| Automatic content tags | — | — | ❌ (V3.5) |
| XMP / EXIF writing | — | — | ❌ (V5 export, to be evaluated) |

### 1.4 Explicitly out of scope

- **No writing tags into photo files / no sidecars**: SQLite only;
- **No hierarchy, groups or synonyms**: flat tags in V3;
- **No "auto-create user tags from rules"**: matches already appear as auto tags;
- **No tag cloud / fancy statistics**: counts and lists only;
- **No deleting, moving or renaming originals**: existing safety rules apply.

---

## 2. Current baseline (code facts, 2026-10-20)

### 2.1 Existing capabilities

| Module | As implemented | Impact on V3 |
| --- | --- | --- |
| `src/database.py` | `photos` (22 columns) + `folders` + `photo_quality`; `SCHEMA_VERSION = 1`, `PRAGMA user_version` already in place | add two tables, bump version to `2`, additive migration |
| `src/rules.py` | 6 EXIF rules, evaluated live, never stored | keep derived; show as a "system tags" section |
| `src/quality.py` | measurements stored, verdicts live; 6 flags | same; another "system tags" section |
| `src/main.py` | `scan / show / stats / classify / filter / analyze / quality / dupes`; `run_filter()` implements rule+quality filtering itself | V3 extracts a shared filter layer used by CLI and GUI |
| `src/gui.py` | left: folder library + 6 rules + 6 quality flags + GPS + AND/OR; 9 result columns; fixed action row; `_passes_filter()` implements filtering itself | V3 adds tag area, editor, batch menu; filtering moves to the shared layer |
| `tests/` | **249** green; GUI tests build real Tk windows | add tag/filter/migration tests, target 300+ |

### 2.2 Limits of the current filter design

GUI and CLI each implement the same rule+quality AND/OR:

- GUI: `PhotoClassifierApp._passes_filter(info, matches, quality_flags)`;
- CLI: a second implementation inside `run_filter(...)`.

Adding tags on top would create a third copy and inconsistent behaviour. This plan
extracts it into `src/filtering.py`; both sides only assemble conditions.

### 2.3 Divergences from the Word roadmap

| Word document | Actual code | Handling |
| --- | --- | --- |
| V3 only lists example tags | rules/quality already exist as "auto tags" | V3 does not duplicate them; it adds **user tags** and unifies filtering |
| no storage defined | SQLite already has a schema version hook | add `tags` / `photo_tags`, version 1→2 |
| no relation to rules | rules live, not stored | keep "system tags derived, user tags stored" |
| no batch operations | result tree supports multi-select but has no actions | multi-select + context menu + confirm + undo |
| no performance notes | 1,375 sample photos; design target 10k | tag cache + SQL counts, refresh budget ≤ 200 ms |

---

## 3. Design principles

1. **System tags derived, user tags stored.**
   Rules and quality stay live (threshold changes take effect immediately) and never
   enter the tag tables; user tags are persistent. The UI presents and filters both
   uniformly, without confusing their origin.
2. **Database only.** Tags never touch EXIF, XMP or files; original safety unchanged.
3. **Idempotent.** Adding the same tag to the same photo twice creates no duplicate
   assignment; batch operations are repeatable.
4. **Flat first.** No hierarchy/groups in V3; do "one photo, many tags" well, leave
   hierarchy to V4 expressions or later.
5. **One filter.** Rules, quality, tags and GPS go into one `FilterSelection`; AND/OR
   semantics are identical in CLI and GUI.
6. **Batch operations reversible.** Batch tagging requires a count confirmation and
   supports undoing the last operation.

---

## 4. Tag model

### 4.1 Vocabulary

| Term | Meaning |
| --- | --- |
| **tag** | a reusable name: "Architecture", "To retouch", "Client A" |
| **assignment** | "this photo has this tag": one `photo_tags` row |
| **source** | how the assignment was produced: `user` (V3), `model` (V3.5), `import` (future) |
| **kind** | the tag's own category: `user` (V3), `system`, `model` |
| **system tag** | computed from rules/quality, **never stored**, runtime only |

### 4.2 Data model

```sql
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT,                 -- "#4c8dff", generated at creation
    kind        TEXT NOT NULL DEFAULT 'user',   -- V3 only 'user'
    created_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_name ON tags (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS photo_tags (
    path        TEXT NOT NULL,        -- same key as photos.path (absolute)
    tag_id      INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user',
    confidence  REAL,                 -- reserved for V3.5 model tags
    created_at  TEXT NOT NULL,
    PRIMARY KEY (path, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_photo_tags_tag ON photo_tags (tag_id);
CREATE INDEX IF NOT EXISTS idx_photo_tags_path ON photo_tags (path);
```

- **No foreign keys**, consistent with `photo_quality` (SQLite FKs are off by default
  and there is no photo-delete path today); deleting a tag removes assignments
  explicitly inside a transaction.
- **`user_version` 1 → 2**: create new tables/indexes only; `photos` untouched,
  no data migration.
- **Size**: 10,000 photos × 5 tags = 50,000 rows ≈ 3–4 MB.
- **Orphans**: `prune()` removes `photo_tags` rows whose path is no longer in `photos`.

### 4.3 Name rules

New `src/tags.py` (pure functions, no I/O):

- trim whitespace and strip a leading `#` (`#architecture` ≡ `architecture`);
- Unicode NFC; collapse internal whitespace;
- reject empty names, newlines/control characters; max 30 characters;
- duplicate detection via `casefold()` (unique index as the last line of defence);
- return normalized name + key for create/lookup.

Errors are readable Chinese messages: `tag name cannot be empty`,
`tag name too long (max 30 characters)`, `tag "architecture" already exists`.

### 4.4 Colours

- Generated from a hash of the name against a fixed palette, stored in `tags.color`;
- palette tuned for the existing dark theme (8–10 colours);
- no manual colour editing in V3; chips use the colour, the result column uses text
  only (Treeview limitation).

### 4.5 Rename / delete semantics

| Operation | Behaviour | Safety |
| --- | --- | --- |
| rename | update `tags.name`; assignments unchanged | reject conflicts and empty names |
| delete | delete its `photo_tags`, then the `tags` row | confirm dialog shows "will remove the tag from N photos"; never deletes photos |
| remove from one photo | delete one assignment | no confirmation; the chip's × is enough |

All writes happen in a transaction; failures roll back with a readable message.

---

## 5. Feature design

### 5.1 Single-photo tags (detail panel)

Add a "My tags" block (before "Quality analysis"):

```text
My tags
[Architecture ×] [Sky ×] [To retouch ×]
[ type a tag…             ] [Add]
```

- chips are coloured; × removes immediately;
- input has **autocomplete** over existing tags (Enter/click to add);
- typing a new name creates the tag and assigns it (with a one-time confirmation);
- inline validation for empty/too long/duplicate input;
- after add/remove: chips, result column, left counts and active filter refresh.

### 5.2 Batch tagging (multi-select)

The result tree already supports multi-select (`Treeview` default `extended`). V3 adds:

- **context menu** on result rows: Open Photo / Reveal in Explorer / Add tag… /
  Remove tag… / Copy Path;
- with multiple rows the menu shows the count: `Add tag… (12 photos)`;
- "Add tag…" dialog: pick an existing tag (searchable) or type a new one; the
  confirmation states "add tag Architecture to 12 photos";
- "Remove tag…" lists only tags **shared by all selected photos**;
- **single-step undo**: status bar shows "Added · Undo (Ctrl+Z)"; undo restores the
  previous batch (one level deep, in memory).

### 5.3 Tag filtering (left panel)

New "My tags" section between quality and the AND/OR selector:

- search box filters the tag list;
- list rows: `☑ Architecture  128`, `☐ Sky  96` (name + photo count from SQL);
- click toggles the checkbox and filters live;
- scrollable so many tags do not squeeze other sections;
- "Manage tags…" opens the manager;
- renamed/deleted tags update the filter state and list;
- "Clear filters" also clears tag selections.

**Relation to existing conditions**: tags, rules and quality share the same
"AND / OR":

- AND: the photo must satisfy every selected condition (including multiple tags);
- OR: any one is enough.

### 5.4 Display

| Location | Presentation |
| --- | --- |
| result table | merged into the existing "Auto tags" column: system tags as before, user tags with a `★` prefix and a vertical-bar separator, e.g. `night · telephoto ｜ ★architecture · ★sky` |
| detail panel | coloured chips (click × to remove) |
| left panel | tag name + count (checkbox for filtering) |
| CLI `show` | append "Tags: architecture, sky" |
| CLI `tags` | full statistics: tag, count, created |

No new result column: the existing 9 columns already fill 1660 px; another would
squeeze filenames.

### 5.5 Tag manager dialog

`Manage tags` (modal Toplevel):

- table: name, photo count, created;
- actions: create, rename, delete;
- rename shows conflicts; delete shows the affected count;
- closing refreshes the main window's list and filter.

### 5.6 Preset tags

On first use, seed a set of **common tags** (no assignments):

`Landscape / Architecture / Night / People / Sky / Water / City / Sport / Flowers / Sunset`

- users may rename or delete them;
- the goal is to avoid an empty "My tags" on first launch;
- a constant controls this; no settings UI in V3.

---

## 6. Filtering architecture (shared layer)

New `src/filtering.py`:

```python
@dataclass(frozen=True)
class FilterSelection:
    rule_ids: frozenset[str] = frozenset()
    quality_ids: frozenset[str] = frozenset()
    tag_ids: frozenset[int] = frozenset()
    and_mode: bool = True
    gps_only: bool = False

    @property
    def is_empty(self) -> bool: ...

def matches(
    info: PhotoInfo,
    *,
    rule_hits: set[str],
    quality_hits: set[str],
    tag_hits: set[int],
    selection: FilterSelection,
) -> bool:
    """the single entry point for rules, quality, tags and GPS"""
```

- GUI and CLI both compute the three hit sets and call `matches()`;
- semantics: `selected = rule_ids | quality_ids | tag_ids`; `and_mode` requires all,
  otherwise any; `gps_only` is an additional hard condition (as today);
- empty selection = everything passes (unless `gps_only`);
- V4 numeric conditions (ISO ≥, focal ≥) extend `FilterSelection` and `matches()`
  without touching either caller again.

| Example | Result |
| --- | --- |
| `tags={architecture,sky}` + AND | has both tags |
| `tags={architecture}` + `rules={night}` + AND | night and architecture |
| `tags={architecture,sky}` + OR | has either |
| `tags={architecture}` + `quality={blurry}` + AND | blurry architecture shots |

---

## 7. CLI design

### 7.1 Commands

| Command | Purpose | Key options |
| --- | --- | --- |
| `tags` | tag statistics | `--db` `--limit` |
| `tag add` | add tags (create if missing) | `--tag NAME` (repeatable) `PATH…` or `--match TEXT` |
| `tag remove` | remove tags from photos | `--tag NAME` `PATH…` or `--match TEXT` |
| `tag list` | list tags of photos | `PATH…` / `--match` `--tag NAME` |
| `tag rename` | rename a tag | `OLD NEW` |
| `tag delete` | delete a tag and its assignments | `NAME` `--yes` |
| `filter --tags` | filter by tags | `--tags A,B`, combined with `--rules` / `--quality` / `--or` |
| `show` | single photo info | append "Tags" |

### 7.2 Examples

```bash
# add two tags to one photo (creating them if needed)
python -m src.main tag add --tag Architecture --tag "To retouch" "D:\Photos\2026\DSC_0012.NEF"

# batch by filename (prints the count and asks for confirmation)
python -m src.main tag add --tag Architecture --match jinjihu

# list tags of a photo
python -m src.main tag list "D:\Photos\2026\DSC_0012.NEF"

# night + architecture (AND), with explanations
python -m src.main filter --rules night --tags Architecture --explain

# architecture OR sky
python -m src.main filter --tags Architecture,Sky --or

# statistics / delete a tag
python -m src.main tags
python -m src.main tag delete "To retouch" --yes
```

### 7.3 Exit codes and safety

- `0` success; `1` nothing matched / no tags; `2` bad arguments; `3` database
  unavailable (consistent with existing commands);
- batch `--match` prints "will affect N photos" before applying; `tag delete`
  without `--yes` only prints the affected count and returns `1`;
- `tag add PATH…` requires the photos to exist in `photos` (run `scan` first);
  unknown paths produce a readable error and return `1`;
- everything writes the database only, never originals.

---

## 8. GUI layout details

| Area | Change |
| --- | --- |
| left · My tags | search + checkbox list (name/count) + "Manage tags…"; between quality and AND/OR |
| result table | user tags merged into "Auto tags" (★ prefix); multi-select preserved; context menu |
| detail panel | new "My tags" block (chips + input + autocomplete) before quality |
| status bar | after batch: "Added tag Architecture to 12 photos · Undo (Ctrl+Z)" |
| shortcuts | `Ctrl+Z` undo last batch; `Ctrl+T` open batch add dialog (optional) |

Window stays 1660×900; the left panel already scrolls, so the tag area cannot squeeze
existing controls.

---

## 9. Performance budget

| Item | Target |
| --- | --- |
| tag cache load (path → tag ids) | 10,000 photos / 50,000 assignments ≤ 100 ms |
| refresh after selecting a tag | ≤ 200 ms (incl. rules/quality) |
| tag list counts | ≤ 50 ms (SQL GROUP BY) |
| batch add 1,000 assignments | ≤ 200 ms (single transaction) |
| database growth | 50,000 rows ≈ 3–4 MB |

Caching: tag assignments use the same revision-based invalidation as quality;
tag CRUD bumps the revision instead of querying on every filter.

---

## 10. Test plan

| Layer | Cases |
| --- | --- |
| normalization (`tags.py`) | `#` prefix, whitespace, empty, too long, control chars, NFC, case duplicates |
| storage (`tag_store.py`) | create/lookup/conflict, idempotent add, remove, rename, delete cascade, counts, orphan prune, rollback |
| migration | `user_version 1 → 2`: existing photos/quality unchanged; new tables exist |
| filtering (`filtering.py`) | tag AND/OR, matrix with rules/quality/GPS, empty selection, clearing selections after tag deletion |
| CLI | `tag add/remove/list/rename/delete`, `--match` confirmation, `filter --tags`, exit codes |
| GUI | detail add/remove chips, autocomplete, left checkbox + counts, multi-select batch add/remove, context menu, undo, manager dialog |
| performance | 10,000 photos / 50,000 assignments: cache load and filtering within budget |
| regression | all existing 249 tests stay green; `pytest` green |

---

## 11. Milestones

| Slice | Content | Acceptance | Estimate |
| --- | --- | --- | --- |
| **M1 data layer** | `tags` / `photo_tags`, `SCHEMA_VERSION=2`, `src/tags.py`, `src/tag_store.py`, migration | storage and migration tests green; conflict/cascade/idempotency correct | 1.5 d |
| **M2 shared filtering + CLI** | `src/filtering.py`; refactor CLI `run_filter` and GUI predicate; `tag` / `tags` commands, `filter --tags` | GUI and CLI agree; combination matrix green | 1.5 d |
| **M3 single-photo GUI** | detail editor (chips/input/autocomplete), ★ in result column, left tag list + search + counts | single add/remove live; counts correct | 2 d |
| **M4 batch + manager GUI** | multi-select context menu, batch dialog + confirmation, single-step undo, manager dialog, `Ctrl+Z` | 100-photo batch works; undo restores correctly | 1.5 d |
| **M5 docs** | bilingual README, plan status, UI design r6, measured performance | docs consistent; 300+ tests green | 0.5–1 d |

**Total ≈ 7–8 working days** (solo, excluding manual click-through testing).

---

## 12. Decision record (to confirm)

| ID | Decision | Recommendation | Alternative | Impact |
| --- | --- | --- | --- | --- |
| **D1** | store system tags? | **no**, rules/quality stay live; user tags stored | materialize rule results as tags | whether threshold changes require recomputation; tag staleness |
| **D2** | hierarchy | **flat in V3** | add hierarchy now | schema and UI complexity |
| **D3** | filter semantics | rules + quality + tags share **one global AND/OR** | grouped logic / expressions | V4 interface and user understanding |
| **D4** | result column | merge user tags into **"Auto tags"** with ★ | a new "My tags" column | width and readability |
| **D5** | preset tags | seed 10 common tags (no assignments, deletable) | start empty | first-run experience |
| **D6** | batch operations | multi-select + context menu + count confirmation + **single-step undo** | confirmation only | error cost vs implementation |
| **D7** | colours | auto colour from name, persisted; no manual picker | colour picker | UI complexity |
| **D8** | write tags to files | **no EXIF/XMP**, database only | write sidecars now | Lightroom interop, original safety |

---

## 13. Risks and limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| left panel keeps growing | crowded on small screens | it already scrolls; tag area can be collapsed/searched; fall back to tabs if needed |
| user tag shares a name with a system tag ("night") | confusion | separate sections + ★ prefix + documentation |
| accidental batch tagging | manual cleanup | count confirmation + single-step undo + context-menu removal |
| stale assignments after files move/delete | wrong counts | `prune()`; the library is path-keyed |
| tags live only in the database | lost when the DB is rebuilt | document backing up `data/photos.db`; XMP export in V5 |
| case/full-width duplicates | multiple tags for one concept | normalization + NOCASE unique index; no full-width conversion (may over-normalize) |

---

## 14. Impact on later versions

- **V3.5 visual recognition**: model tags write to `tags(kind='model')` /
  `photo_tags(source='model', confidence=…)` — no new tables; the UI can show source
  and confidence in the auto-tag area.
- **V4 smart filtering**: `FilterSelection` gains numeric conditions (ISO/focal/
  shutter); `matches()` becomes an expression evaluator; tag ids are stable keys.
- **V5 Lightroom / export**: user tags map to XMP `dc:subject`; export can build
  folders by tag or export only selected tags. No file writes in this stage.
- **V6 performance**: the revision-based tag cache mirrors the V2.5 quality cache;
  large libraries load assignments once instead of per photo.

---

## 15. What I need you to confirm

1. **D1**: keep system tags (rules/quality) derived and out of the tag tables?
2. **D4**: merge user tags into the existing "Auto tags" column (★ prefix), or add a
   separate "My tags" column?
3. **D5**: is this preset list right — Landscape / Architecture / Night / People /
   Sky / Water / City / Sport / Flowers / Sunset? Anything to add or remove?
4. **D6**: do you want single-step undo for batch tagging? (recommended)
5. **D3**: is one shared AND/OR for tags + rules + quality enough for now, or do you
   already need grouped logic (AND inside a group, OR between groups)?
6. Should videos be taggable? (recommended yes; tags are file-level attributes)

Once confirmed, implementation proceeds M1 → M5. No code is written before confirmation.

---

## 16. Implementation record and deviations (2026-10-20)

> V3 is **implemented and accepted**. This section records where the delivered code
> differs from the plan.

### 16.1 Actual modules

| File | Content |
| --- | --- |
| `src/tags.py` | name normalization (NFC, leading `#`, length, control chars), colours, 10 presets |
| `src/tag_store.py` | `tags` / `photo_tags` CRUD, idempotent adds, cascade delete, counts, orphan prune, exact-pair operations for undo |
| `src/filtering.py` | `FilterSelection` + `matches()`: the single predicate for rules/quality/tags/GPS |
| `src/database.py` | schema `1 → 2`, two new tables, presets seeded during migration |
| `src/main.py` | `tags`, `tag add/remove/list/rename/delete`, `filter --tags`, tags in `show`; `run_filter` uses the shared predicate |
| `src/gui.py` | tag search/checkboxes/counts, chip editor, ★ in results, right-click batch, single-step undo, manager |

### 16.2 Deviations from the plan

| Item | Planned | Implemented | Why |
| --- | --- | --- | --- |
| preset timing | "on first tag library creation" | **during the schema 1→2 migration** | migration happens once; deleting all tags later must not bring them back |
| result column order | system tags first, user tags after | **user tags (★) first**, system tags after | the column is narrow; long system tags would push ★ out of view |
| result column widths | keep the original 9 widths | name 190→180, camera 120→110, auto-tags 150→180, quality 95→90, size 85→80 | make room for ★ without changing total width |
| left tag list | list + counts | added a **scrollbar** and a "search tags" label | 10 presets already exceed the visible height |
| path handling | one `Path.resolve()` everywhere | resolve for writes/single lookups; **no resolve in the filter hot path** | per-photo `resolve()` cost ~2.7 s for 10k photos |
| tag manager | create/rename/delete | as planned; no merge/sorting | keep V3 simple |
| undo scope | batch add/remove | as planned: one level, in memory, not across restarts | keep complexity low |

### 16.3 Measurements

| Item | Result |
| --- | --- |
| Tests | **325 green** (76 new since V3) |
| Real database migration | 1,375 photos, user_version `1 → 2`, 10 presets, original data unchanged |
| 10,000 photos / 50,000 assignments | cache load + path mapping **114 ms**; rules+tags filter **11 ms**; counts **4 ms** |
| Batch writes | tag 1,000 photos **21 ms**; 50,000 assignments **22 ms** |
| GUI | left filtering, detail chips, batch add/remove, Ctrl+Z, manager verified for M3/M4 |

### 16.4 Known limitations

- The tag list is ordered by SQLite name order (binary), not pinyin;
- tags are flat only — no hierarchy/groups;
- undo covers only the latest batch operation; single-chip × is not undoable;
- tags live only in the database, so Lightroom cannot see them (XMP in V5);
- the manager has no "merge duplicate tags" action yet.

### 16.5 P0/P1 optimization record (2026-10-20)

| Item | Before | After | Note |
| --- | --- | --- | --- |
| GUI filter refresh (1,375 rows) | ~260 ms | **~40 ms** | `duplicates.info_for` no longer calls `Path.resolve()` per row; `src/paths.py` centralizes keys |
| CLI filter over 10,000 photos | ~2.7 s extra | — | same resolve removed |
| 50,000 tag assignments | ~2.9 s | **~22 ms** | `executemany` + existing-pair lookup; 1,000 rows ~21 ms |
| SQLite concurrency | no WAL | WAL + `busy_timeout=5000` | analysis writer and tagging no longer fight for the lock |
| Schema source | duplicated DDL in `quality_store` | `QUALITY_SCHEMA` only in `database.py` | avoids drift |
| GUI file size | 1,872 lines | **739 lines** | panels/detail/dialogs/theme moved to `ui_panels.py`, `ui_detail.py`, `ui_tag_dialogs.py`, `ui_theme.py` |
| CLI file size | 1,169 lines | **869 lines** | tag commands moved to `tag_cli.py`; command dispatch table |
| Tag state | mixed into GUI | `tag_controller.py` (pure) | cache, batch and undo are independently testable |

> Performance budget adjusted: tag cache + path mapping measures ~114 ms, so the
> original 100 ms target was relaxed to **≤150 ms**; all other targets stand.

---

*This plan has been implemented; keep this section and the README in sync for future changes.*
