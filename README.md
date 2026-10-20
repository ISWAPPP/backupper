# Backuper infrastructure

Очищений знімок Backup API та активної інфраструктури, отриманий 20 жовтня 2026 року перед виведенням сервера з експлуатації. Код застосунку збережено без функціонального рефакторингу; адресу сервера й публічний SSH host key замінено шаблонами.

## Що всередині

- `rootfs/opt/backup-api/` — FastAPI, клієнтський агент, rsync-wrapper, архівація, очищення, зафіксовані Python-залежності.
- `rootfs/etc/` — активний nginx IP-vhost, systemd-сервіси/таймери, cron, logrotate, скорочена конфігурація fail2ban без старого IP адміністратора.
- `rootfs/usr/local/sbin/` — наскрізна перевірка бекапів і Discord-сповіщення.
- `render.py` — підстановка IPv4 та SSH host key нового сервера в окрему директорію, без встановлення чи перезапису наявної директорії.
- `test_render.py`, `test_smoke.py` — перевірки рендерингу й ізольованого API/архівації.

Не включено старий `.git`, резервні версії коду, приватні ключі, TLS-сертифікати, Discord webhook, журнали, робочі архіви, Hermes/IDE та сторонній експеримент у домашньому каталозі. Вихідні каталоги архівів та incoming були порожні під час інвентаризації. Це не повний образ диска.

## Потік даних

Клієнт → HTTPS `/register_key` → тимчасовий SSH-ключ → rsync на `backupuser` через forced-command wrapper → `/create_archive` → tar.gz → `/status/<UUID>`.

API слухає тільки `127.0.0.1:8000`; зовні потрібні TCP 22, 80, 443. Дані: `/home/backupuser/incoming`, `/var/www/backups`; логи сесій: `/home/backupuser/tmp-logs`. Бази даних і Docker для цього сервісу немає.

**Перед використанням прочитайте [SECURITY.md](SECURITY.md).** Сервіс має наявні обмеження безпеки й збереження даних; цей експорт їх не виправляє.

## Перевірки без розгортання

Під час експорту успішно виконано: встановлення зафіксованих залежностей у чистий venv (`pip check` без помилок), обидва тести рендерера, ізольований smoke-тест, Python compileall і bash syntax check. Згенеровані конфіги пройшли `nginx -t`, `fail2ban-client -t`, `systemd-analyze verify`. На вихідному сервісі `backup-probe` підтвердив TLS, API, реєстрацію, rsync, архівацію, відновлення вмісту й завантаження. Це не замінює повне відновлення на окремому VPS; Discord-відправлення повторно не тестували, щоб не надсилати зайві повідомлення.

Потрібні Linux, GNU tar/coreutils, Python 3.14 (як на вихідному сервері). Рендерер і його тести використовують лише stdlib і також працюють на macOS.

```sh
python3 -m unittest test_render -v
python3 -m venv .venv
.venv/bin/pip install -r rootfs/opt/backup-api/requirements.txt
.venv/bin/python test_smoke.py
```

Smoke-тест запускає окремий процес на випадковому loopback-порту з тимчасовими каталогами, перевіряє 400/403/429, tar.gz, вміст відновленого файлу й очищення ключа. Не звертається до справжнього authorized_keys; позитивну реєстрацію й rsync перевіряє встановлений `backup-probe`. Для тесту потрібен файловий розділ із понад 1 GiB вільного місця.

## Відновлення на чистому VPS

Наведені команди — для **нового виділеного Ubuntu VPS**, від root. Не копіюйте `rootfs` поверх сервера з наявними сервісами. Повне відновлення на другому чистому VPS ще не перевірено.

### 1. Пакети й рендеринг

Скопіюйте репозиторій на новий сервер, перейдіть у його корінь. Усі наступні блоки виконуються в одній shell-сесії. Замініть приклади реальними значеннями.

```sh
set -eu
export PUBLIC_HOST=203.0.113.10
export LE_EMAIL=admin@example.com
apt-get update
apt-get install -y python3 python3-venv nginx rsync openssh-server curl cron fail2ban
ssh-keygen -A
python3 render.py --host "$PUBLIC_HOST" \
  --ssh-host-key "$(cat /etc/ssh/ssh_host_ed25519_key.pub)" --output build/rootfs
```

Публічний host key береться з локального файла нового сервера, не через неперевірений `ssh-keyscan`. Перевірте власний адміністративний SSH-доступ та host fingerprint окремо. Шаблон підтримує IPv4/TLS на IP; DNS, IPv6 і Terraform навмисно не додано.

### 2. Файли та права

```sh
! test -e /opt/backup-api || { printf 'Existing deployment: stop\n' >&2; exit 1; }
id backupuser >/dev/null 2>&1 || useradd --create-home --shell /bin/bash backupuser
install -d -o backupuser -g backupuser -m 0750 /home/backupuser/incoming /home/backupuser/tmp-logs
install -d -o backupuser -g backupuser -m 0700 /home/backupuser/.ssh
install -o backupuser -g backupuser -m 0600 /dev/null /home/backupuser/.ssh/authorized_keys
install -d -o backupuser -g backupuser -m 0775 /var/www/backups
install -d -m 0755 /var/www/acme-ip
install -d -m 0700 /etc/backup-probe /var/lib/backup-probe
cp -a build/rootfs/. /
chmod 0755 /opt/backup-api/*.sh /usr/local/sbin/backup-probe /usr/local/sbin/backup-ready-notify
install -o backupuser -g backupuser -m 0755 /opt/backup-api/ssh-wrapper.sh /home/backupuser/ssh-wrapper.sh
python3 -m venv /opt/backup-api/venv
/opt/backup-api/venv/bin/pip install -r /opt/backup-api/requirements.txt
printf '127.0.0.1 %s\n' "$(cut -d ' ' -f 1,2 /etc/ssh/ssh_host_ed25519_key.pub)" > /etc/backup-probe/known_hosts
chmod 0600 /etc/backup-probe/known_hosts
```

### 3. TLS на IP

На вихідному сервері використано Certbot 5.8.0, окреме сховище `/etc/letsencrypt-ip-prod` та профіль `shortlived`. Спочатку увімкніть тільки HTTP-блок, щоб nginx не вимагав ще не створені сертифікати.

```sh
python3 - <<'PY'
from pathlib import Path
p = Path('/etc/nginx/sites-available/backup-ip-http')
p.write_text(Path('build/rootfs/etc/nginx/sites-available/backup-ip-http').read_text().split('\nserver {', 1)[0] + '\n')
PY
rm -f /etc/nginx/sites-enabled/default
ln -s /etc/nginx/sites-available/backup-ip-http /etc/nginx/sites-enabled/backup-ip-http
nginx -t
systemctl enable --now nginx
systemctl reload nginx
python3 -m venv /opt/certbot-ip
/opt/certbot-ip/bin/pip install certbot==5.8.0
/opt/certbot-ip/bin/certbot certonly --non-interactive --agree-tos --email "$LE_EMAIL" \
  --webroot -w /var/www/acme-ip --ip-address "$PUBLIC_HOST" --cert-name "$PUBLIC_HOST" \
  --required-profile shortlived --server https://acme-v02.api.letsencrypt.org/directory \
  --config-dir /etc/letsencrypt-ip-prod --work-dir /var/lib/letsencrypt-ip-prod \
  --logs-dir /var/log/letsencrypt-ip-prod
cp build/rootfs/etc/nginx/sites-available/backup-ip-http /etc/nginx/sites-available/backup-ip-http
nginx -t && systemctl reload nginx
```

Вхідний TCP 80 має бути доступний для HTTP-01. Приватні TLS-ключі генеруються заново й не потрапляють у Git.

### 4. Сервіси та перевірка

```sh
systemctl daemon-reload
systemctl enable --now cron backup-api.service backup-ip-cert-renew.timer
fail2ban-client -t
systemctl enable fail2ban
systemctl restart fail2ban
curl -fsS "https://$PUBLIC_HOST/disk_space"
/usr/local/sbin/backup-probe
systemctl enable --now backup-probe.timer
```

`backup-probe` створює власну тимчасову сесію, перевіряє TLS → реєстрацію → rsync → tar → відновлення вмісту → завантаження, потім прибирає свої файли. Не додає клієнтський лог, тому не запускає ready-сповіщення. У разі помилки перевірки без webhook у журналі буде `ALERT DELIVERY UNCONFIGURED`; доставка аварійних повідомлень до Discord потребує наступного кроку.

Перевірте доступ із зовнішнього клієнта. Агент розрахований на GNU/Linux із bash, rsync, OpenSSH, curl, coreutils. Завантажте, перегляньте та запустіть його; не обов'язково використовувати pipe-to-shell:

```sh
curl -fsS "https://$PUBLIC_HOST/agent.sh" -o agent.sh
less agent.sh
bash agent.sh
```

### 5. Discord — за потреби

Webhook створіть заново. Введіть його без echo; значення не потрапляє в Git чи командний рядок процесу:

```sh
python3 - <<'PY'
from getpass import getpass
from pathlib import Path
import os
os.umask(0o077)
value = getpass('New Discord webhook: ').strip()
if not value.startswith('https://discord.com/api/webhooks/') or any(c in value for c in '\r\n"\\'):
    raise SystemExit('Invalid webhook')
Path('/etc/backup-probe/discord-webhook').write_text(value + '\n')
Path('/etc/backup-probe/curl.conf').write_text('url = "' + value + '"\n')
for name in ('discord-webhook', 'curl.conf'):
    Path('/etc/backup-probe', name).chmod(0o600)
PY
systemctl enable --now backup-ready-notify.timer
```

## Обслуговування

- `systemctl status backup-api` та `journalctl -u backup-probe` — API й наскрізний тест.
- `systemctl list-timers 'backup-*'` — probe щодня о 04:30 UTC + до 5 хвилин; ready-notify щохвилини; TLS renew двічі на добу + до 15 хвилин.
- `/etc/cron.d/backup-api` — очищення щодня о 03:00 у часовій зоні ОС. Якщо потрібен UTC: `timedatectl set-timezone UTC`.
- `fail2ban-client status sshd` / `fail2ban-client status nginx-404` — активні jails. Додайте поточний IP адміністратора до `ignoreip` перед тестуванням банів. nginx-404 блокує часті 404, не замінює автентифікацію.
- Налаштуйте firewall на доступні вам SSH/80/443. Старі правила, паролі та SSH-налаштування не імпортуються.
- Ліцензію для публічного поширення ще не обрано власником. Remote Git-репозиторій не задано; цей каталог готовий до додавання remote та push після остаточного перегляду.
