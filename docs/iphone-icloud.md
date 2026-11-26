# Atlas без сервера: iPhone → iCloud Drive → MacBook

Без оплати, без відкритих портів. iPhone кладе повідомлення файлом в iCloud Drive,
Atlas на Mac забирає його, розбирає через твою підписку Claude (Claude Code) і кладе відповідь.
Mac спить — файли чекають в iCloud; без інтернету — iPhone синхронізує пізніше.

Папки (створює `deploy/mac/install.sh`): `iCloud Drive/Atlas/inbox` і `iCloud Drive/Atlas/outbox`.
База: `~/Atlas/atlas.db` на Mac (не в iCloud), копія щодня в `~/Atlas/backups`.

## Команда «Atlas» (записати)

Назви дій залежать від мови iPhone, тому тут — англійська назва і слово для пошуку
(рядок пошуку дій унизу екрана).

1. Команди → «+» → назва **Atlas**.
2. Ask for Input — пошук «ввід» / «запит»; тип — текст, підказка «Atlas». Під час запуску натисни
   мікрофон на клавіатурі й диктуй. (Або Dictate Text — пошук «дикт».)
3. Current Date — пошук «дата»; потім Format Date: власний формат `yyyyMMdd-HHmmss-SSS`.
   Це назва файлу й унікальний номер повідомлення.
4. Save File — пошук «зберегти файл»: вхід = введений текст; «запитувати, де зберегти» — вимкнено;
   папка стандартна (Shortcuts), шлях `Atlas/` + форматована дата + `.txt`.
   На Mac це `~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Atlas`
   (Atlas запущено з `--inbox …/Atlas --outbox …/Atlas-replies`).
5. Show Notification — пошук «сповіщ»: «Записано в Atlas».

## Команда «Atlas відповіді» (подивитися, що записано)

1. Get Contents of Folder (пошук «папк»): Shortcuts/Atlas-replies: `iCloud Drive/Atlas/outbox`.
2. Filter Files (пошук «фільтр»): сортувати за датою зміни, від нових; обмеження 5.
3. Repeat with Each → Get Text from Input → Show Result (пошук «повтор», «текст», «результат»).

Запуск: кнопка дії iPhone 16 (Параметри → Кнопка дії → Команда → Atlas), Siri, віджет.
