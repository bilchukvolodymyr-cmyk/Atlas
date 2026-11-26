# Atlas — етап 0: ядро

Сховище без LLM: записи, відкриті цикли, ідеї, щоденні показники, журнал подій.
Дизайн: https://claude.ai/artifact/GfvFEhzhXDEmX1JeNqxG46

## Що гарантує ядро (перевірено тестами)

| Гарантія | Тест |
| --- | --- |
| Повідомлення зберігається до обробки | `test_failed_action_writes_nothing_and_marks_inbox_failed` |
| Повторна доставка (той самий UUID) не створює дублікатів | `test_redelivery_ten_times_creates_one_loop` |
| Усі зміни одного повідомлення — одна транзакція | `test_failed_action_writes_nothing...` |
| kill -9 посеред запису → база ціла, нічого не записано наполовину, повідомлення можна обробити знову | `test_kill_minus_9_inside_transaction` |
| Закриття циклу лише через перевірку (`verifying` → `verify_loop`) | `test_cannot_close_directly`, `test_cannot_skip_verification` |
| «Зроблено» зберігає ваші слова як доказ | `test_done_claimed_stores_user_message_as_evidence` |
| Журнал подій не можна змінити чи видалити | `test_events_are_append_only` |
| Стан повністю відновлюється з журналу | `test_rebuild_from_journal_gives_identical_state` |
| Показник за той самий день оновлюється, а не дублюється | `test_metric_same_day_updates_not_duplicates` |

## Запуск

Потрібен Python 3.11+. Залежностей для роботи немає (лише стандартна бібліотека); для тестів — pytest.

```
python3 -m pytest -q                 # тести
python3 -m atlas --db atlas.db list  # відкриті цикли
```

Команди CLI:

```
python3 -m atlas add "Сьогодні 103 людини" --kind event
python3 -m atlas loop new "Підготувати місце" --assigned-by "Roberto Poli" --due 2026-11-29
python3 -m atlas loop move 1 done_claimed --evidence "звільнили"
python3 -m atlas loop move 1 verifying
python3 -m atlas loop verify 1 --evidence "Роберто перевірив"
python3 -m atlas metric 2026-11-26 trucks_unload 10
python3 -m atlas show 1              # цикл + вся історія
python3 -m atlas rebuild             # відновити стан з журналу і порівняти
```

`--id <uuid>` робить повтор команди безпечним: той самий id = той самий запис.

## Структура

- `atlas/schema.sql` — таблиці (inbox, entries, loops, ideas, metrics, questions, events).
- `atlas/core.py` — `Store`: `ingest()`, `apply(inbox_id, actions)`, `rebuild()`, `backup()`.
  `apply` приймає список дій — той самий формат, який на етапі 1 видаватиме Claude.
- `atlas/cli.py` — командний рядок.
- `tests/test_core.py` — 32 тести.

## Ще не зроблено (наступні етапи)

Розбір тексту через Claude (етап 1), сервер і iPhone (етап 2), пошук і фото (етап 3),
нагадування (етап 4). Перетворення «завтра / в суботу» на дату, повторювані справи,
таблиці attachments/topics/links — теж пізніше.
