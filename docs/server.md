# Сервер Atlas (VPS)

Сервер: будь-який VPS з Ubuntu 24.04 у ЄС, 2 ГБ RAM достатньо (напр. Hetzner CX22, Німеччина).

Встановлення (від root, один раз; повторний запуск оновлює код і нічого не стирає):

```
curl -fsSL https://raw.githubusercontent.com/bilchukvolodymyr-cmyk/atlas/main/deploy/install.sh -o install.sh
bash install.sh
```

Що робить `install.sh`:
- ставить Python, Caddy (HTTPS), брандмауер: відкриті лише SSH, 80, 443;
- окремий системний користувач `atlas`, база `/var/lib/atlas/atlas.db` (доступ лише йому);
- просить ключ Anthropic (ввід приховано), генерує токен; обидва лише в `/etc/atlas/env` (права 640);
- адреса без купівлі домену: `<ip>.sslip.io`, сертифікат HTTPS Caddy отримує сам;
- служба `atlas` (перезапуск при збої, обмежені права), щоночі о 03:30 копія бази з перевіркою
  цілісності, зберігаються 30 останніх;
- в кінці перевіряє `https://<адреса>/health`.

Корисні команди:

```
systemctl status atlas            # чи працює
journalctl -u atlas -n 50         # журнал (тексти повідомлень туди не пишуться)
ls /var/lib/atlas/backups         # копії бази
```
