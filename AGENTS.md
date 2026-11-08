# Работа над проектом

Цель: CQA: оценка по рубрике со свидетельствами и апелляциями

Категории: ai-ml, backend, frontend, full-stack, hackathon.
Режим: hackathon. Рабочая область: текущий проект; соседние проекты не изменять.

## Стек и архитектура
- Python 3.11+ standard library
- SQLite
- HTML / CSS / JavaScript
- Local Ollama structured-output adapter
- Local HTTP application bound to loopback
- Versioned editable rubric; model returns criterion decisions and evidence
- Deterministic Decimal aggregation and evidence validation
- Immutable original evaluation plus append-only appeals and review decisions
- Offline contract-test mode visibly distinct from live model analysis

## Доступные процессы
- `$hub-hackathon` — ON_DISK
- `$hub-research` — ON_DISK
- `$hub-innovation` — ON_DISK
- `$hub-engineering` — ON_DISK
Подробности и условные роли: `.bootstrap/runs/`, файлы `plan.json` и `capabilities.json`.
Загружать только нужный навык и его зависимости. Проверяющие роли читают исходные
доказательства; их инструкции не создают отдельную песочницу.

## Качество и ограничения
- Official eight criteria and discrete scales applied exactly
- N/A proportional redistribution, two decimal places and separate gross-error flag
- Upload organizer JSON or a new transcript; preserve roles and timestamps
- Change criterion in UI and apply new rubric version
- Evidence links resolve to exact source message; wrong speaker and invented quotes rejected
- Appeal, review, recompute, persist, JSON/CSV export
- Provider failure must never be a successful evaluation
- Human-GT evaluation harness reports per-criterion error and leakage warnings
- No paid AI calls or cloud transcript transfer without explicit authorization
- No publication or submission
- Ground Truth and official accuracy metric UNKNOWN
- No changes to global settings or user-selected models
- Keep signed dataset URL and transcript data out of public repository
Хранить секреты вне исходников. Сохранять лицензии и происхождение данных/ассетов.
Фиксировать реальные команды, результаты и ограничения; не придумывать измерения,
источники, научную новизну или требования организаторов/университета.

## Проверки
- python -m unittest discover -s tests -v
- python -m cqa serve
- python tools/verify_project.py
Отличать структурную проверку от запуска приложения, эксперимента и браузера.
Сначала изучать существующие инструкции в затрагиваемых подкаталогах.
