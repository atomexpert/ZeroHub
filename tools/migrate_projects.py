#!/usr/bin/env python3
"""
CLI utility for Desktop Antigravity -> Antigravity Hub automatic project & chat migration (v96).

Usage examples:
  python tools/migrate_projects.py --dry-run
  python tools/migrate_projects.py --apply
  python tools/migrate_projects.py --apply --project II_Parser --rebuild-venv
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Enforce 0% GPU invariant before importing app modules
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from app import database, project_migrator  # noqa: E402

_ACTION_LABELS = {
    "copy_to_hub": "📦 Перенос в Hub",
    "already_in_hub": "✅ Уже в Hub",
    "conflict_both_non_empty": "⚠️ Конфликт папок",
    "missing_source": "👻 Папка удалена (только чаты)",
}


def _short_path(p: str, max_len: int = 44) -> str:
    s = str(p or "")
    if len(s) <= max_len:
        return s
    return "..." + s[-(max_len - 3) :]


def _print_plan_table(plan: dict, only_projects: set[str] | None = None) -> None:
    items = plan.get("projects") or []
    if only_projects:
        items = [it for it in items if it["project_name"].lower() in only_projects]

    print("=" * 116)
    print(
        f"📦 ПЛАН МИГРАЦИИ ПРОЕКТОВ DESKTOP ANTIGRAVITY -> {plan.get('projects_root')}"
    )
    print(
        f"Просканировано диалогов Desktop: {plan.get('total_desktop_conversations_scanned', 0)} | "
        f"Отфильтровано системных путей: {plan.get('filtered_system_paths_count', 0)}"
    )
    print("=" * 116)

    if not items:
        print("Внешних проектов для переноса не обнаружено.")
        return

    header = (
        f"{'Проект':<22} | {'Откуда':<36} | {'Куда':<26} | {'Файлов':>6} | {'Чатов (+нов)':>12} | {'Статус'}"
    )
    print(header)
    print("-" * 116)
    for it in items:
        pname = it["project_name"][:22]
        src_s = _short_path(it["source_path"], 36)
        dst_s = _short_path(it["dest_path"], 26)
        f_cnt = it["real_files_count"]
        tot_c = it["total_conversations_count"]
        unimp_c = it["unimported_conversations_count"]
        chats_col = f"{tot_c} (+{unimp_c})"
        status = _ACTION_LABELS.get(it["action"], it["action"])
        if it.get("dest_is_skeleton") and it["action"] == "copy_to_hub":
            status += " [заполнит скелет]"
        print(f"{pname:<22} | {src_s:<36} | {dst_s:<26} | {f_cnt:>6} | {chats_col:>12} | {status}")
    print("-" * 116)
    counts = plan.get("counts_by_action") or {}
    print(
        f"К переносу кода: {counts.get('copy_to_hub', 0)} | "
        f"Уже в Hub: {counts.get('already_in_hub', 0)} | "
        f"Конфликтов: {counts.get('conflict_both_non_empty', 0)} | "
        f"Новых чатов к импорту: {plan.get('total_unimported_conversations', 0)}"
    )


async def _run_async(args: argparse.Namespace) -> int:
    selected_list: list[str] | None = None
    selected_low: set[str] | None = None
    if args.project:
        selected_list = []
        for raw_p in args.project:
            for part in str(raw_p).split(","):
                clean = part.strip()
                if clean:
                    selected_list.append(clean)
        selected_low = {p.lower() for p in selected_list}

    plan = project_migrator.discover_desktop_migration_plan()
    _print_plan_table(plan, only_projects=selected_low)

    if not args.apply:
        print(
            "\nℹ️ Режим предпросмотра (--dry-run): файлы и база данных НЕ изменялись."
            "\nДля запуска безопасного копирования выполните: python tools/migrate_projects.py --apply"
        )
        return 0

    if args.move_siblings:
        print("\n🚀 Выполняется консолидация проектов в Antigravity Hub (соседние папки переносятся, внешние копируются)...")
    else:
        print("\n🚀 Выполняется неразрушающее копирование (исходные папки остаются нетронутыми)...")
    await database.init_db()
    report = await project_migrator.execute_desktop_migration(
        selected_projects=selected_list,
        import_chats=(not args.no_chats),
        merge_missing_on_conflict=bool(args.merge_missing),
        rebuild_venv=bool(args.rebuild_venv),
        move_legacy_siblings=bool(args.move_siblings),
    )

    print("\n✅ МИГРАЦИЯ ЗАВЕРШЕНА:")
    print(f"  • Перенесено проектов:      {report['migrated_projects_count']}")
    print(f"  • Скопировано файлов:       {report['total_copied_files']}")
    print(f"  • Импортировано чатов:      {report['total_imported_chats']}")
    print(f"  • Перепривязано чатов Hub:  {report['total_relinked_chats']}")
    for r in report.get("results") or []:
        line = (
            f"    - {r['project_name']}: статус={r['status']}, "
            f"файлов={r.get('copied_files', 0)}, "
            f"новых чатов={r.get('imported_chats_count', 0)}"
        )
        if r.get("venv_rebuild"):
            vr = r["venv_rebuild"]
            line += f", .venv={'OK' if vr.get('ok') else 'ERR: ' + str(vr.get('error'))}"
        if r.get("warning"):
            line += f" [⚠️ {r['warning']}]"
        print(line)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Безопасный автоперенос проектов и диалогов из Desktop Antigravity в Antigravity Hub."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать таблицу найденных проектов без изменения файлов и БД (по умолчанию).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Выполнить копирование файлов (shutil.copy2) и авто-импорт диалогов в hub.db.",
    )
    parser.add_argument(
        "--project",
        action="append",
        help="Перенести только указанный проект (можно указать несколько раз или через запятую).",
    )
    parser.add_argument(
        "--merge-missing",
        action="store_true",
        help="При конфликте двух непустых папок скопировать только недостающие файлы без перезаписи.",
    )
    parser.add_argument(
        "--no-chats",
        action="store_true",
        help="Не импортировать автоматически диалоги из Desktop Antigravity.",
    )
    parser.add_argument(
        "--rebuild-venv",
        action="store_true",
        help="Пересоздать чистый .venv и установить requirements.txt для перенесённых Python-проектов.",
    )
    parser.add_argument(
        "--move-siblings",
        action="store_true",
        help="Переместить соседние папки из старого корня (D:\\Projects\\*) внутрь users/admin/projects/.",
    )
    args = parser.parse_args()
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
