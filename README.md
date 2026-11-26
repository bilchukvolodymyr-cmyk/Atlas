# Atlas — робоча пам'ять

Етап 0: сховище (записи, відкриті цикли, ідеї, щоденні показники, журнал подій).
Етап 1: розбір вільного тексту через Claude з перевіркою кодом.
Етап 2: HTTP-сервер для iPhone (`atlas/server.py`), встановлення на VPS (`deploy/`),
інструкції: [сервер](docs/server.md), [iPhone](docs/iphone.md).
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

## Етап 1: розбір тексту через Claude

Claude нічого не пише в базу. Він повертає «пункти» (що, на його думку, є в повідомленні),
кожен із дослівною цитатою. Код перевіряє кожен пункт і лише тоді передає дії в ядро.

| Правило | Тест |
| --- | --- |
| Цитати немає в повідомленні → пункт відкинуто | `test_invented_quote_is_rejected_and_invented_name_dropped` |
| Ім'я (хто доручив / на кого чекаємо) не з повідомлення → поле не записується | той самий |
| Число показника не стоїть у цитаті → не записується, Atlas питає | `test_daily_metrics_and_number_check` |
| Дати рахує код (Europe/Rome), не Claude: завтра / в суботу / наступний тиждень / кінець місяця | `test_dates_from_thursday_26_november`, `test_three_tasks_from_roberto_with_different_deadlines` |
| «У четвер», сказане в четвер, → справа без строку + питання | `test_ambiguous_date_creates_loop_without_due_and_asks` |
| «Зробив» → перевірка + питання; закриття лише наступним повідомленням-підтвердженням | `test_done_needs_confirmation_in_a_later_message` |
| Повторна доставка не викликає Claude вдруге | `test_redelivery_does_not_call_claude_again` |
| Збій мережі/API → повідомлення чекає і обробляється пізніше (до 5 спроб) | `test_api_error_keeps_message_and_retries_later` |
| Відмова моделі / зламана відповідь → текст зберігається нотаткою + питання | `test_refusal_keeps_message_as_note_with_question` |
| Ядро відхилило дію → одна повторна спроба з описом помилки, далі нотатка | `test_apply_error_gets_one_retry_with_feedback` |
| Кожен виклик Claude записаний у `llm_runs` (відповідь, що відкинуто і чому, токени) | `test_api_error_keeps_message_and_retries_later` |

Нагадування: справа на день — о 18:00 напередодні (або о 7:00 того дня, якщо пізно);
на тиждень — у понеділок о 7:00; до кінця місяця — за 3 дні о 7:00.

Модель `claude-opus-5-5`, structured outputs (JSON-схема), `effort=medium`,
серверні запасні моделі при відмові (`fallbacks="default"`).

```
pip install anthropic
export ANTHROPIC_API_KEY=...
python3 -m atlas say "Роберто дав на завтра підготувати місце біля рампи 31"
python3 -m atlas questions
python3 -m atlas process            # обробити все, що чекає в inbox
python3 evals/run_eval.py           # 40 реальних фраз проти справжнього Claude (платно)
```

## Структура

- `atlas/schema.sql` — таблиці (inbox, entries, loops, ideas, metrics, questions, events).
- `atlas/core.py` — `Store`: `ingest()`, `apply(inbox_id, actions)`, `rebuild()`, `backup()`.
  `apply` приймає список дій — той самий формат, який на етапі 1 видаватиме Claude.
- `atlas/cli.py` — командний рядок.
- `atlas/understand.py` — схема відповіді Claude, промпт, перевірка цитат, перетворення на дії.
- `atlas/dates.py` — «завтра / в суботу / кінець місяця» → дата; правила нагадувань.
- `atlas/pipeline.py` — inbox → Claude → перевірка → apply, повтори, запасний шлях.
- `atlas/server.py`, `atlas/reply.py`, `atlas/backup.py` — сервер, відповідь людською мовою, копії бази.
- `deploy/` — install.sh, systemd, Caddy (HTTPS).
- `tests/` — 66 тестів (Claude замінено імітацією; один тест перевіряє справжній SDK без мережі).
- `evals/phrases.jsonl`, `evals/run_eval.py` — живий тест на 40 фразах.

## Ще не зроблено (наступні етапи)

Пошук і фото (етап 3),
надсилання нагадувань (етап 4; час уже рахується). Повторювані справи,
таблиці attachments/topics/links — теж пізніше.
