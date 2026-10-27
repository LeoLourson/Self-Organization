# Пул агентов: кто лучший организатор

Пять одинаковых агентов по очереди организуют прогноз P-индекса, затем голосуют
за лучшего организатора. Протокол — в `Задачи.md`, ход запусков — в `Журнал.md`.

## Что где лежит

| Путь | Содержимое |
|---|---|
| `config.json` | Параметры протокола, движки API, профили моделей, пути песочницы |
| `data/series.csv` | Ряд P-индекса: колонки `week,value`, по неделям по порядку |
| `prompts/` | Всё, что видят агенты; `summary.json` задаёт состав итогов раунда по условиям |
| `netexp/run.py` | Автомат прогона |
| `netexp/llm.py` | OpenAI-совместимый API с кэшем в `requests.sqlite3` |
| `netexp/sandbox.py` | Временное удаление инструкций Claude Code, файл субагента |
| `agents/orchestrator.md` | Инструкция сессии Claude Code, которая ведёт субагентов |
| `runs/state/`, `runs/results/` | Состояние и итог каждого прогона |
| `tests/` | Проверки без платных вызовов |

## Запуск

```powershell
python -m pip install -r requirements.txt
python -m unittest discover tests -v
python run.py plan --profile deepseek --repeats 5
```

API (`deepseek`, `sonnet_api`): нужен ключ из `engines.*.key_env`. Прогон идёт целиком:

```powershell
python run.py start --profile deepseek --condition full --repeat 0
python run.py advance --id ID
```

Субагенты (`sonnet_sub`): см. `agents/orchestrator.md`.

## Сохранность

- Файл `STOP` в корне прекращает новые шаги.
- Состояние сохраняется после каждого ответа; после обрыва повторите `advance`.
  Ответы API кэшируются по ID запроса и повторно не оплачиваются.
- Правка `prompts/`, данных или параметров протокола меняет хеш `protocol`:
  начатые прогоны не продолжаются, новые получают другие ID.
- `sandbox on` переносит `~/.claude/CLAUDE.md`, `~/.claude/skills` и память
  проекта в `backup/sandbox/` и выключает плагины. `sandbox off` возвращает их
  и отказывается перезаписывать файлы, изменённые за это время.
