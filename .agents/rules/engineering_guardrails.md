# Инженерный регламент Antigravity Hub (защита от регрессий, `v=146+`)

<!-- Постоянный файл в Git. Читается AGY CLI (вся папка .agents/rules), Claude Code и Codex (hub_project.md + остальные *.md по алфавиту). -->

## 1. Обязательный гейт
- Любая правка кода, тестов или UI сдаётся только после `.venv\Scripts\python.exe tools\verify_hub.py` со статусом `VERIFY_HUB_OK`.
- Перед коммитом автоматически работает версионируемый хук `tools/git_hooks/pre-commit` (`git config core.hooksPath tools/git_hooks`, установка — `python tools/verify_hub.py --install-hooks`). Он запускает `verify_hub.py --staged-guard`.
- Обход хука (`git commit --no-verify`, `-n`, временная смена `core.hooksPath`) запрещён. Если гейт красный, нужно исправить причину, а не отключать проверку.
- В индекс добавлять только явные пути. `git add .`, `git add -A`, `git push` без прямой просьбы пользователя запрещены.
- В коммит не должны попадать `data/`, `users/`, `shared_projects/`, `tmp/`, `scratch/`, `*.db*`, `.env*`, кроме `.env.example`. `--staged-guard` блокирует их.

## 2. Накопление защиты
- Каждая новая функция или исправление бага сопровождается изолированным регрессионным тестом в `tests/` (временные каталоги и SQLite-фикстуры, без обращения к живой `data/hub.db`, сети и интерактивной станции `WinSta0`).
- Ключевой инвариант новой доработки добавляется в кортеж `GOLDEN` в `tools/verify_hub.py`. Тест в `GOLDEN` должен быть быстрым, детерминированным и проходить в общем пакетном прогоне.
- Существующие тесты из `GOLDEN` нельзя удалять, пропускать (`skip`/`xfail`) или ослаблять ради зелёного прогона без явного решения в «Штабе».

## 3. Неприкосновенные продуктовые инварианты
- Запрещено автоматическое понижение выбранной модели и уровня мышления (например, `Gemini High → Medium/Low`) при ошибках, квотах или таймаутах. Возможны честная ошибка, повтор или ручной выбор пользователя.
- Запрещено удалять или переименовывать живые DOM `id` и `data-*` из `tests/test_frontend_contracts.py` без синхронного обновления контракта и всех обращений в `app/static/app.js`.
- Не останавливать и не перезапускать живой сервер (`run_server.py`, `stop_hub.bat`, `start_hub.bat`) из рабочего хода агента. Не изменять `data/hub.db` и `data/`.
- Дочерние процессы на Windows запускаются скрыто (`subprocess_hidden_kwargs()`, `CREATE_NO_WINDOW`). ASR и сервисы работают с `0% GPU`.
- `.agents/rules/hub_project.md` генерируется Хабом автоматически, его нельзя редактировать вручную. Постоянные правила хранятся в этом файле.

## 4. Архитектура UI (`v=141+`)
- Новые стили пишутся только в `app/static/ui-system.css` в слоях `@layer tokens / primitives / shell / components / utilities`.
- `@layer legacy` (`app/static/styles.css`) не расширяется: число `!important` контролируется тестом и не должно расти.
- Поповеры, меню и выпадающие списки позиционируются через `window.AgyUI` (`app/static/ui-shell.js`: `computeFloatingPosition`, `flip`, `shift`, `maxHeight`) с проверкой попадания во вьюпорт на `1440×900`, `390×844`, `320×568`.
- Иконки берутся из `AgyUI.icon(...)` (локальный набор Lucide), без внешних CDN.
- Адаптивность обеспечивают `100dvh`, `min-width: 0` и `min-height: 0`. Горизонтальный скролл страницы недопустим: `document.documentElement.scrollWidth <= window.innerWidth`.

## 5. Кэш PWA (`Cache-First`)
- `app/static/sw.js` отдаёт версионированную статику по схеме `Cache-First`. Если версия не поднята, смартфон останется на старом JS и CSS.
- При изменении любого `app/static/*.{js,css,html}` в одном коммите синхронно поднимается `N`: `?v=N` в `app/static/index.html`, а также `CACHE_NAME = "agy-hub-vN"` и `SHELL_ASSETS` в `app/static/sw.js`.
- `--staged-guard` блокирует коммит, если в индексе изменены `app/static/*.js` или `*.css`, а `CACHE_NAME` в `sw.js` не стал больше версии из `HEAD`.

## 6. Технические требования
- Исходники хранятся в UTF-8 без BOM, символы-заменители `U+FFFD` недопустимы. Сохраняйте существующие окончания строк файла (часть файлов использует CRLF).
- Используется интерпретатор `D:\Projects\Antigravity_Hub\.venv\Scripts\python.exe`.
- После изменений обязательны `node --check` для изменённых JS, `py_compile` для Python и `git diff --check`, все они входят в `verify_hub.py`.
