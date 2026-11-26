# Atlas без сервера: iPhone → iCloud Drive → MacBook

Без оплати, без відкритих портів. iPhone кладе повідомлення файлом в iCloud Drive,
Atlas на Mac забирає його, розбирає через твою підписку Claude (Claude Code) і кладе відповідь.
Mac спить — файли чекають в iCloud; без інтернету — iPhone синхронізує пізніше.

Папки (створює `deploy/mac/install.sh`): `iCloud Drive/Atlas/inbox` і `iCloud Drive/Atlas/outbox`.
База: `~/Atlas/atlas.db` на Mac (не в iCloud), копія щодня в `~/Atlas/backups`.

## Команда «Atlas» (записати)

Назви дій залежать від мови iPhone: тут італійська, у дужках англійська.
Шукати дії зручно рядком пошуку внизу («input», «data», «salva file», «notifica»).

1. «Comandi» → «+» → назва **Atlas**.
2. **Richiedi input** (Ask for Input): tipo Testo, підказка «Atlas». Під час запуску натисни
   мікрофон на клавіатурі й диктуй (надійніше, ніж «Detta testo» / Dictate Text).
3. **Data attuale** (Current Date) → **Formatta data** (Format Date): формат «Personalizzato»
   `yyyyMMdd-HHmmss-SSS`. Це назва файлу й унікальний номер повідомлення.
4. **Salva file** (Save File): вхід = Input fornito; «Chiedi dove salvare» — вимкнено;
   шлях `Atlas/inbox/` + Data formattata + `.txt`.
5. **Mostra notifica** (Show Notification): «Записано в Atlas».

## Команда «Atlas відповіді» (подивитися, що записано)

1. **Ottieni contenuti della cartella** (Get Contents of Folder): `iCloud Drive/Atlas/outbox`.
2. **Filtra file** (Filter Files): сортувати за датою зміни, від нових; обмеження 5.
3. **Ripeti con ognuno** (Repeat with Each) → **Ottieni testo dall'input** (Get Text from Input) → **Mostra risultato** (Show Result).

Запуск: кнопка дії iPhone 16 (Параметри → Кнопка дії → Команда → Atlas), Siri, віджет.
