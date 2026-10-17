# PhotoClassifier Logo

[English](logo.md) | [简体中文](logo.zh-CN.md)

> 选定方案：**A —— 光圈标签环**（2026-10-18 确认）。概念轮：[concepts-r1.png](concepts-r1.png)

## 创意说明

标志是一个**由 6 段彩色弧组成的相机光圈环**，内部带有真实的光圈叶片结构：

- 每一段**彩色弧就是一个标签**——产品的核心理念「一张照片，多个标签」
  直接体现在标志里；
- **光圈叶片**带来摄影语言（光圈 = 拍摄）；
- 六种色相预埋了未来的标签颜色体系：界面里每个标签类别都可以认领其中一色。

## 色板

| 色样 | 色值 | 角色 |
| --- | --- | --- |
| ![#4C8DFF](https://img.shields.io/badge/%234C8DFF-4C8DFF) | `#4C8DFF` | 主色蓝（品牌 / 夜景标签） |
| ![#B18CFF](https://img.shields.io/badge/%23B18CFF-B18CFF) | `#B18CFF` | 紫 |
| ![#38B6C9](https://img.shields.io/badge/%2338B6C9-38B6C9) | `#38B6C9` | 青 |
| ![#3FB27F](https://img.shields.io/badge/%233FB27F-3FB27F) | `#3FB27F` | 绿 |
| ![#E8A33D](https://img.shields.io/badge/%23E8A33D-E8A33D) | `#E8A33D` | 橙 |
| ![#E86A8A](https://img.shields.io/badge/%23E86A8A-E86A8A) | `#E86A8A` | 玫红 |

应用图标底色：深藏青渐变 `#2B385C → #181B26`（圆角方形，圆角 22%）。

## 文件清单

| 文件 | 用途 |
| --- | --- |
| `assets/photoclassifier.ico` | 多尺寸应用图标（16–256）。桌面快捷方式与窗口标题栏引用它 |
| `assets/logo/logo-icon-512.png` | 圆角方底应用图标，512 px（深色底） |
| `assets/logo/logo-mark-512.png` | 透明底标志，512 px——GitHub 头像、深色界面 |
| `assets/logo/logo-mark-light-512.png` | 浅色背景适配版（叶片改为深灰） |
| `assets/photoclassifier-preview.png` | 256 px 预览图 |

## 使用规则

- **最小尺寸**：20 px。48 px 以下省略光圈叶片（多尺寸 `.ico` 已自动处理）；
- **留白**：标志四周至少保留标志宽度 12% 的空白；
- **深浅背景**：浅色背景请使用 `logo-mark-light-512.png`
  （叶片翻转为深灰以保证对比度）；
- 环上的六色是预留的标签色板——不要单独改动某段弧的颜色；
- 标志由脚本参数化生成（生成代码随仓库版本管理），可复现、可迭代。

## 修订记录

- r2（2026-10-18）：方案 A 定稿；产出应用图标（`.ico`）、512 px 各版本与
  浅色背景适配版；桌面快捷方式与 GUI 标题栏经 `.ico` 自动更新。
- r1（2026-10-18）：`concepts-r1.png` 探索四个方向，选定方案 A。
