# PhotoClassifier Logo

[English](logo.md) | [简体中文](logo.zh-CN.md)

> Selected concept: **A — Aperture Tag Ring** (confirmed 2026-10-18).
> Concept round: [concepts-r1.png](concepts-r1.png)

## The Idea

The mark is a **camera aperture ring built from six colored arc segments**,
with a true aperture-blade iris inside:

- Each **colored arc is one tag** — the product's core idea "one photo, many
  tags" lives directly in the mark;
- The **iris blades** give it the photography language (aperture = shooting);
- The six hues anticipate the future tag color system: every tag category can
  own one of these colors in the UI.

## Color Palette

| Swatch | Hex | Role |
| --- | --- | --- |
| ![#4C8DFF](https://img.shields.io/badge/%234C8DFF-4C8DFF) | `#4C8DFF` | Primary blue (brand / night tag) |
| ![#B18CFF](https://img.shields.io/badge/%23B18CFF-B18CFF) | `#B18CFF` | Purple |
| ![#38B6C9](https://img.shields.io/badge/%2338B6C9-38B6C9) | `#38B6C9` | Teal |
| ![#3FB27F](https://img.shields.io/badge/%233FB27F-3FB27F) | `#3FB27F` | Green |
| ![#E8A33D](https://img.shields.io/badge/%23E8A33D-E8A33D) | `#E8A33D` | Orange |
| ![#E86A8A](https://img.shields.io/badge/%23E86A8A-E86A8A) | `#E86A8A` | Rose |

App-icon background: dark navy gradient `#2B385C → #181B26` (rounded square,
radius 22%).

## Files

| File | Use |
| --- | --- |
| `assets/photoclassifier.ico` | Multi-size app icon (16–256). Desktop shortcut and window title bar reference it |
| `assets/logo/logo-icon-512.png` | Rounded-square app icon, 512 px (dark background) |
| `assets/logo/logo-mark-512.png` | Transparent-background mark, 512 px — GitHub avatar, dark UI |
| `assets/logo/logo-mark-light-512.png` | Light-background variant (iris blades darkened) |
| `assets/photoclassifier-preview.png` | 256 px preview |

## Usage Rules

- **Minimum size**: 20 px. Below 48 px the iris blades are omitted
  (the multi-size `.ico` already handles this automatically);
- **Clear space**: keep at least 12% of the mark's width free on all sides;
- **Light vs dark**: on light backgrounds use `logo-mark-light-512.png`
  (the blades flip to a darker gray for contrast);
- The ring colors are the reserved tag palette — do not recolor individual
  arcs;
- The mark is generated programmatically (`scripts` in repo history; drawing
  parameters live in the generator), so it stays reproducible and versioned.

## Changelog

- r2 (2026-10-18): concept A finalized; app icon (`.ico`), 512 px exports and
  light-background variant produced; desktop shortcut and GUI title bar
  updated automatically via the `.ico`.
- r1 (2026-10-18): four concepts explored in `concepts-r1.png`; concept A
  selected.
