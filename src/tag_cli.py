"""V3 标签相关 CLI 命令（由 main.py 注册）。"""

from __future__ import annotations

from pathlib import Path

from src.database import PhotoDatabase
from src.photo_info import PhotoInfo
from src.tag_store import Tag, TagStore
from src.tags import TagError


def resolve_tags(db_path: str | Path, raw: str) -> list[Tag]:
    """把逗号分隔的标签名解析成标签对象；未知名称给出可读错误。"""
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise SystemExit("标签不能为空")
    with TagStore.open(db_path) as store:
        tags = [store.get_by_name(name) for name in names]
    missing = [name for name, tag in zip(names, tags) if tag is None]
    if missing:
        raise SystemExit("未知标签 " + "、".join(missing) + "（先用 `tags` 查看已有标签）")
    return [tag for tag in tags if tag is not None]


def selected_photos(database: PhotoDatabase, paths: list[str], match: str):
    """按显式路径或模糊匹配取出照片；返回 (照片列表, 未知路径列表)。"""
    if paths:
        selected: list[PhotoInfo] = []
        missing: list[str] = []
        for raw in paths:
            info = database.get_photo(raw)
            if info is None:
                missing.append(raw)
            else:
                selected.append(info)
        return selected, missing
    if match:
        return database.find_photos(match, limit=20000), []
    return [], []


def format_tags_report(db_path: str | Path, *, limit: int = 50) -> str:
    """`tags` 命令：标签统计。"""
    with TagStore.open(db_path) as store:
        tags = store.list_tags()
        counts = store.counts()
        total_assignments = store.count_assignments()
    if not tags:
        return "（还没有标签，用 `tag add` 创建）"
    lines = [f"标签：{len(tags)} 个 · 关联 {total_assignments:,} 条"]
    for tag in tags[:limit]:
        lines.append(f"  {tag.name}：{counts.get(tag.id, 0):,} 张（{tag.kind}）")
    if len(tags) > limit:
        lines.append(f"  ……以及另外 {len(tags) - limit} 个（用 --limit 调整）")
    return "\n".join(lines)


def cmd_tag_add(args) -> int:
    with PhotoDatabase.open(args.db) as database:
        photos, missing = selected_photos(database, args.paths, args.match)
    if missing:
        print("以下路径不在照片库中（先运行 scan）：")
        for item in missing:
            print(f"  {item}")
        return 1
    if not photos:
        print("（没有匹配的照片）")
        return 1

    created: list[str] = []
    with TagStore.open(args.db) as store:
        tags: list[Tag] = []
        for name in args.tag:
            existing = store.get_by_name(name)
            if existing is None:
                tag = store.create_tag(name)
                created.append(tag.name)
            else:
                tag = existing
            tags.append(tag)
        print(f"匹配照片：{len(photos):,} 张")
        added_pairs, unknown = store.add_tags(
            [info.path for info in photos], [tag.id for tag in tags]
        )
        if unknown:
            print(f"跳过 {len(unknown)} 个不在照片库中的路径")
        counts = store.counts()
        tag_names = "、".join(tag.name for tag in tags)
    if created:
        print("新建标签：" + "、".join(created))
    print(f"已给 {len(photos):,} 张照片添加标签「{tag_names}」：新增 {len(added_pairs):,} 条关联")
    print("当前关联数：" + " · ".join(f"{tag.name} {counts.get(tag.id, 0):,}" for tag in tags))
    return 0


def cmd_tag_remove(args) -> int:
    with TagStore.open(args.db) as store:
        tags: list[Tag] = []
        for name in args.tag:
            tag = store.get_by_name(name)
            if tag is None:
                print(f"标签不存在：{name}")
                return 2
            tags.append(tag)
    with PhotoDatabase.open(args.db) as database:
        photos, missing = selected_photos(database, args.paths, args.match)
    if missing:
        print("以下路径不在照片库中（先运行 scan）：")
        for item in missing:
            print(f"  {item}")
        return 1
    if not photos:
        print("（没有匹配的照片）")
        return 1
    with TagStore.open(args.db) as store:
        removed = store.remove_tags(
            [info.path for info in photos], [tag.id for tag in tags]
        )
    names = "、".join(tag.name for tag in tags)
    print(f"已从 {len(photos):,} 张照片移除标签「{names}」：删除 {len(removed):,} 条关联")
    return 0


def cmd_tag_list(args) -> int:
    with PhotoDatabase.open(args.db) as database, TagStore.open(args.db) as store:
        if args.tag:
            tag = store.get_by_name(args.tag)
            if tag is None:
                print(f"标签不存在：{args.tag}")
                return 2
            paths = store.photos_with_tag(tag.id)
            print(f"标签「{tag.name}」：{len(paths):,} 张")
            for path in paths:
                print(path)
            return 0
        photos, missing = selected_photos(database, args.paths, args.match)
        if missing:
            for item in missing:
                print(f"不在照片库中：{item}")
            return 1
        if not photos:
            print("（没有匹配的照片）")
            return 1
        tags_by_path = store.tags_for_paths(info.path for info in photos)
        for info in photos:
            tags = tags_by_path.get(str(info.path), [])
            names = "、".join(tag.name for tag in tags) if tags else "（无标签）"
            print(f"{info.file_name}：{names}")
    return 0


def cmd_tag_rename(args) -> int:
    with TagStore.open(args.db) as store:
        tag = store.get_by_name(args.old)
        if tag is None:
            print(f"标签不存在：{args.old}")
            return 2
        try:
            updated = store.rename_tag(tag.id, args.new)
        except TagError as exc:
            print(str(exc))
            return 2
    print(f"已重命名：{tag.name} → {updated.name}")
    return 0


def cmd_tag_delete(args) -> int:
    with TagStore.open(args.db) as store:
        tag = store.get_by_name(args.name)
        if tag is None:
            print(f"标签不存在：{args.name}")
            return 2
        affected = len(store.photos_with_tag(tag.id))
        if not args.yes:
            print(
                f"标签「{tag.name}」将从 {affected:,} 张照片移除（不会删除照片）。\n"
                f"确认删除请加 --yes"
            )
            return 1
        removed = store.delete_tag(tag.id)
    print(f"已删除标签「{tag.name}」，移除 {removed:,} 条关联")
    return 0
