# tg_nk_google_helper_bot — важная почта для Никиты

Цель проекта одна: Никита должен узнавать о важных письмах, а оценка важности
должна постепенно подстраиваться под его решения.

Проект состоит из двух независимых инструментов:

- `gmail_tool.py` — явный доступ агентов к семи Gmail-ящикам: поиск, чтение,
  черновики; отправка закрыта отдельным OAuth-разрешением и флагом `--yes`;
- `mail_watch.py` — постоянный worker: Gmail → надёжная очередь → классификация
  → Telegram-бот `@nk_google_helper_bot` → обратная связь кнопками.

Оба агента (`some` и `some_codex`) видят `gmail_tool.py` через симлинки. Worker
самостоятелен и продолжает следить за почтой, даже когда агенты выключены.

Для Клоди есть отдельная durable-подписка, не зависящая от категории Никиты.
Из `business@ddinsights.org` ей через peer bus приходят уведомления платформ
(`platform-notice` для автоматических Snapchat/Google/CrazyGames), отдельные
события CrazyGames и Google Play, а также любые человеческие входящие ответы
площадок. Событие содержит только mailbox, gmail_id, sender, subject и topic;
тело Клоди при необходимости читает сама через `gmail_tool.py read`. Доставка
at-least-once, дедупликация — по gmail_id.

## Что видит Никита

Срочные и важные письма приходят отдельными обычными Telegram-уведомлениями.
Под каждым четыре кнопки:

- `🔴 срочно`;
- `🟡 важное`;
- `⚪ неважное`;
- `· мусор`.

Нажатие сохраняется как персональный пример. Следующие похожие письма
классификатор получает вместе с релевантными прошлыми решениями. Один клик не
создаёт вечного правила для всего домена: тема, ящик и повторяемость остаются
важны, а решение можно исправить другой кнопкой.

Уверенно неважные письма не присылаются автоматически: цель бота — сообщать о
важном, а не переносить почтовый шум в Telegram. Они остаются в журнале;
команда `/recent` показывает последние письма, включая скрытые, и позволяет
поднять ошибочно скрытое письмо кнопкой `↑ важное`.

Команды бота:

```text
/status   здоровье очереди и число поправок
/recent   последние 12 писем
/help     краткая инструкция
```

## Почему письмо не теряется

Состояние живёт в DigitalOcean Managed MySQL, база `agent_mail`, отдельный
пользователь `mail_watch`, соединение TLS с проверкой CA.

Порядок обработки:

1. Gmail отдаёт новые message id и следующий `historyId`.
2. Все id и новый cursor фиксируются одной MySQL-транзакцией.
3. Отдельно подтягиваются только метаданные, затем классификация.
4. Только успешный `sendMessage` помечает письмо доставленным.
5. Любая ошибка оставляет запись в очереди для следующей попытки.
6. Релевантные Клоди письма получают независимую subscriber-outbox; её ack
   ставится только после успешного `peer.py tell`.

Папки Spam и Trash не опрашиваются. Кроме фильтров Gmail, worker повторно
требует текущую метку Inbox и отсутствие Spam/Trash/Draft/Sent при получении
metadata. Не прошедшие этот барьер письма fail-closed отбрасываются до
классификации и любых subscriber-событий.

Если MySQL недоступен, Gmail-cursor не продвигается. Если Telegram недоступен,
письмо уже лежит в outbox. При неоднозначном сетевом таймауте возможен редкий
дубль уведомления — это сознательная модель at-least-once: дубль лучше потери.

Другие закрытые дыры старой версии:

- после восьмого важного письма больше нет голого счётчика: хвост остаётся в
  очереди и досылается;
- лимит первых 50 metadata больше не отбрасывает остальные id;
- протухший Gmail `historyId` запускает полный постраничный sync Inbox и только
  после его завершения двигает cursor;
- второй worker/ручной `once` получает явный отказ через локальный lock и
  глобальный MySQL lease;
- callbacks принимаются только от привязанного Telegram user id;
- поломка Opus fail-open: письмо считается `important`.

## Приватность и классификация

В `claude-opus-5` уходят только:

- адрес и имя отправителя;
- тема;
- назначение ящика;
- Gmail labels и признак массовой рассылки;
- релевантные прошлые оценки Никиты.

Тело письма не запрашивается watcher-ом и в модель не уходит. Заголовки
считаются недоверенными данными. Классификатор запускается без tools, MCP,
hooks, project-инструкций и session persistence; prompt передаётся через stdin.
Поэтому тема письма может повлиять только на предложенную категорию, но не
получает shell/files/network. Локально/в MySQL сохраняются только эти
метаданные, категория, причина и доставка.

## Gmail-инструмент для агентов

Имя ящика — его полный адрес:

```bash
gmail_tool.py list
gmail_tool.py search me@company.com "is:unread" -n 5 --snippet
gmail_tool.py read me@company.com <message_id>
gmail_tool.py draft me@company.com --to a@b.c --subject "Тема" --body -
```

Подключение:

```bash
gmail_tool.py add me@company.com
gmail_tool.py add me@company.com --with-send   # только по явному решению
gmail_tool.py describe me@company.com --purpose "для чего этот ящик"
```

`send` требует одновременно scope `gmail.send` у конкретного ящика и явный
`--yes`. По умолчанию агенты читают и создают черновики.

## Установка и provisioning

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python provision_mysql.py \
  --cluster db-mysql-fra1-42798 \
  --database agent_mail \
  --user mail_watch

install -m 644 mail-watch.service ~/.config/systemd/user/mail-watch.service
systemctl --user daemon-reload
systemctl --user enable --now mail-watch.service
```

`provision_mysql.py` берёт DigitalOcean API token из
`~/.config/aeolian/do_token`, создаёт отдельные database/user, скачивает CA и
пишет runtime-config без вывода пароля в терминал. Provisioner добавляет текущий
public IP в DigitalOcean trusted sources через GET→merge→PUT→readback, выполняет
миграцию схемы и затем оставляет runtime-пользователю только DML в `agent_mail`.

Первичная привязка Telegram выполняется только при остановленном worker:

```bash
systemctl --user stop mail-watch
.venv/bin/python mail_watch.py discover --owner-user-id <ваш Telegram user id>
systemctl --user start mail-watch
```

Старая `watch_state.json` мигрируется в MySQL один раз и не удаляется.

## Файлы и секреты

| Назначение | Путь |
|---|---|
| OAuth client Gmail | `~/.config/agent_gmail/client_secret.json` |
| Gmail refresh tokens | `~/.config/agent_gmail/tokens/<адрес>.json` |
| Назначения ящиков | `~/.config/agent_gmail/mailboxes.json` |
| Старое состояние/cursor | `~/.config/agent_gmail/watch_state.json` |
| MySQL runtime config | `~/.config/agent_gmail/mysql.json` |
| DigitalOcean MySQL CA | `~/.config/agent_gmail/do_mysql_ca.crt` |
| Telegram bot token | `~/.config/agent_gmail/bot_token.txt` |

Все секретные файлы имеют mode `0600` и не входят в репозиторий.

## Операции и проверки

```bash
.venv/bin/python mail_watch.py status
.venv/bin/python mail_watch.py once       # только при остановленном service
.venv/bin/python mail_watch.py announce   # прислать владельцу /help

.venv/bin/python -m unittest discover -s tests -v
MAIL_WATCH_INTEGRATION=1 \
  .venv/bin/python -m unittest discover -s tests \
  -p 'test_mail_store_integration.py' -v
```

Настройки окружения:

| Переменная | По умолчанию |
|---|---|
| `MAIL_WATCH_INTERVAL` | `120` секунд |
| `MAIL_WATCH_TELEGRAM_POLL` | `15` секунд |
| `MAIL_WATCH_MODEL` | `claude-opus-5` |
| `MAIL_WATCH_CLASSIFIER_TIMEOUT` | `60` секунд; один сбой включает fail-open на цикл |
| `MAIL_WATCH_CONFIDENCE_FLOOR` | `0.72` |
| `MAIL_WATCH_MYSQL_CONFIG` | `~/.config/agent_gmail/mysql.json` |
| `MAIL_WATCH_TOKEN_FILE` | `~/.config/agent_gmail/bot_token.txt` |
| `MAIL_WATCH_PEER_SCRIPT` | `some_codex/bot_workspace/scripts/peer.py` |

## Граница для игр

Мобильная игра никогда не подключается к MySQL напрямую: пароль оказался бы в
APK. Если кластер понадобится для телеметрии, экономики или Remote Config,
между игрой и базой обязателен серверный API с аутентификацией, rate limits и
минимальными правами отдельного пользователя.
