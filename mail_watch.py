#!/usr/bin/env python3
"""Воркер входящей почты: следит за новыми письмами, сортирует, шлёт в Telegram.

Про опрос. Вебхуки у Gmail есть (users.watch + Cloud Pub/Sub), но им нужен
проект в Cloud, топик, подписка и живой endpoint. Здесь дешевле опрос по
historyId: с сервера приходят ТОЛЬКО изменения с прошлой проверки, а не
список писем. Один проход по шести ящикам — шесть лёгких запросов, даже
раз в минуту это пустяк по квоте.

Сортировка. Отправитель и тема (без тела письма) уходят в claude-opus-5,
он проставляет категорию и одну фразу «почему». Категории:
    urgent    — требует реакции сегодня
    important — важное, но подождёт
    routine   — информационное
    noise     — рассылки, автоуведомления, реклама
В Telegram уходят только urgent и important; routine и noise собираются в
одну строку дайджеста, чтобы не превращать уведомления в шум.

Запуск:
    mail_watch.py discover        # найти свой chat_id (нужно один раз)
    mail_watch.py once            # один проход, для проверки
    mail_watch.py run             # цикл, для systemd
    mail_watch.py status          # что воркер знает про ящики
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmail_tool as gt

TOKEN_FILE = Path(os.environ.get('MAIL_WATCH_TOKEN_FILE', '/tmp/bot_api.txt'))
STATE = Path(os.environ.get('MAIL_WATCH_STATE',
                            gt.HOME_CFG / 'watch_state.json'))
POLL_INTERVAL = int(os.environ.get('MAIL_WATCH_INTERVAL', '120'))
CLASSIFIER_MODEL = os.environ.get('MAIL_WATCH_MODEL', 'claude-opus-5')
CLASSIFIER_TIMEOUT = 180
BATCH = 20            # писем в одном запросе к классификатору
MAX_NOTIFY = 8        # уведомлений за проход: при наплыве остальное в дайджест
COLD_START_DAYS = 1   # на первом запуске показываем письма за сутки
COLD_START_LIMIT = 15

NOTIFY = ('urgent', 'important')
EMOJI = {'urgent': '🔴', 'important': '🟡', 'routine': '⚪', 'noise': '·'}


def log(msg: str) -> None:
    print(f'{time.strftime("%H:%M:%S")} {msg}', flush=True)


# ============================================================
# Telegram
# ============================================================

def _token() -> str:
    """Токен бота из файла. Терпим и голый токен, и строку вида KEY=токен."""
    try:
        raw = TOKEN_FILE.read_text(encoding='utf-8').strip()
    except OSError as e:
        raise SystemExit(f'не читается {TOKEN_FILE}: {e}')
    m = re.search(r'\d{6,}:[A-Za-z0-9_-]{30,}', raw)
    if not m:
        raise SystemExit(f'в {TOKEN_FILE} не нашёл токен бота')
    return m.group(0)


def _tg(method: str, **params):
    import requests
    r = requests.post(f'https://api.telegram.org/bot{_token()}/{method}',
                      json=params, timeout=30)
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError(f'telegram {method}: {data.get("description")}')
    return data['result']


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    os.replace(tmp, STATE)


def cmd_discover(args) -> int:
    """Находит chat_id по сообщению, которое Никита прислал боту."""
    if getattr(args, 'drop_webhook', False):
        _tg('deleteWebhook')
        print('вебхук снят')
    me = _tg('getMe')
    print(f'бот: @{me.get("username")} ({me.get("first_name")})')
    updates = _tg('getUpdates', limit=20)
    if not updates:
        # Пусто бывает по трём причинам, и каждая лечится по-своему
        hook = _tg('getWebhookInfo')
        if hook.get('url'):
            print(f'\nУ бота стоит вебхук: {hook["url"]}\n'
                  'Пока он стоит, getUpdates всегда пуст. Снять:\n'
                  '  mail_watch.py discover --drop-webhook')
        else:
            print('\nСообщений нет. Telegram хранит их 24 часа — если писал '
                  'давно, просто напиши боту ещё раз и повтори discover.')
        return 1
    found = {}
    for u in updates:
        msg = u.get('message') or u.get('edited_message') or {}
        chat = msg.get('chat') or {}
        if chat.get('id'):
            who = chat.get('username') or chat.get('first_name') or '?'
            found[chat['id']] = (who, msg.get('text', '')[:40])
    if not found:
        print('в апдейтах нет чатов')
        return 1
    for cid, (who, text) in found.items():
        print(f'  chat_id={cid}  @{who}  «{text}»')
    state = _load_state()
    state['chat_id'] = list(found)[-1]
    _save_state(state)
    print(f'\nзапомнил chat_id={state["chat_id"]} в {STATE}')
    return 0


def _chat_id() -> int:
    cid = _load_state().get('chat_id')
    if not cid:
        raise SystemExit('chat_id неизвестен. Сначала: mail_watch.py discover')
    return cid


# ============================================================
# Новые письма через history API
# ============================================================

def _header(msg, name):
    for h in msg.get('payload', {}).get('headers', []):
        if h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


def _new_messages(alias: str, state: dict) -> list:
    """Возвращает новые письма во входящих с прошлой проверки.

    Первый запуск только запоминает точку отсчёта: иначе воркер вывалил бы
    в Telegram всю накопленную почту (а в одном ящике её 106 тысяч).
    """
    svc = gt._service(alias)
    box = state.setdefault('boxes', {}).setdefault(alias, {})
    profile = svc.users().getProfile(userId='me').execute()
    current = profile.get('historyId')

    if not box.get('history_id'):
        box['history_id'] = current
        # Холодный старт. Молчать нельзя: письмо, пришедшее за час до
        # запуска, человек уже видел в почте, а бот про него не скажет —
        # выглядит как сломанный воркер. Но и всю историю поднимать не
        # надо, поэтому берём узкое окно последних часов.
        try:
            resp = svc.users().messages().list(
                userId='me', q=f'in:inbox newer_than:{COLD_START_DAYS}d',
                maxResults=COLD_START_LIMIT).execute()
            ids = [m['id'] for m in resp.get('messages', [])]
        except Exception as e:
            log(f'{alias}: холодный старт не удался ({e})')
            ids = []
        log(f'{alias}: первый запуск, точка отсчёта {current}, '
            f'свежих писем {len(ids)}')
        return _fetch_meta(svc, alias, ids)

    ids, page = [], None
    try:
        while True:
            resp = svc.users().history().list(
                userId='me', startHistoryId=box['history_id'],
                historyTypes=['messageAdded'], labelId='INBOX',
                pageToken=page).execute()
            for h in resp.get('history', []):
                for added in h.get('messagesAdded', []):
                    m = added.get('message', {})
                    labels = m.get('labelIds', [])
                    # Свои отправленные и черновики во входящих не считаем
                    if 'INBOX' in labels and 'DRAFT' not in labels \
                            and 'SENT' not in labels:
                        ids.append(m['id'])
            page = resp.get('nextPageToken')
            if not page:
                box['history_id'] = resp.get('historyId', current)
                break
    except Exception as e:
        # historyId старше ~недели протухает — начинаем отсчёт заново,
        # молча, без попытки догнать пропущенное
        if '404' in str(e) or 'not found' in str(e).lower():
            log(f'{alias}: historyId протух, беру новую точку отсчёта')
            box['history_id'] = current
            return []
        raise

    return _fetch_meta(svc, alias, ids)


def _fetch_meta(svc, alias: str, ids: list) -> list:
    """Дотягивает отправителя и тему. Тело письма не запрашиваем вовсе."""
    items = []
    for mid in ids[:50]:          # потолок на проход, чтобы не залипнуть
        try:
            msg = svc.users().messages().get(
                userId='me', id=mid, format='metadata',
                metadataHeaders=['From', 'Subject']).execute()
        except Exception as e:
            log(f'{alias}: не прочитал {mid}: {e}')
            continue
        items.append({
            'alias': alias,
            'id': mid,
            'from': _header(msg, 'From'),
            'subject': _header(msg, 'Subject') or '(без темы)',
        })
    return items


# ============================================================
# Сортировка через Opus 5
# ============================================================

PROMPT = '''Ты сортируешь входящую почту. Для каждого письма дай категорию и
одну короткую фразу по-русски, почему именно такая.

Категории:
  urgent    — требует реакции сегодня: дедлайн, инцидент, счёт к оплате,
              живой человек ждёт ответа, доступы/безопасность
  important — важное по сути, но подождёт день-другой
  routine   — информационное: отчёты, статусы, подтверждения
  noise     — рассылки, маркетинг, автоуведомления сервисов, соцсети

Учитывай назначение ящика: письмо по работе в личный ящик — обычно важнее,
чем то же самое в рабочий.

Письма:
{items}

Ответь ТОЛЬКО JSON-массивом, без пояснений и без markdown:
[{{"i": 1, "category": "urgent", "why": "короткая причина"}}]'''


def _classify(items: list, meta: dict) -> list:
    """Отдаёт список категорий той же длины, что items.

    В модель уходят только отправитель и тема — тело письма не покидает
    машину. Если классификатор недоступен, всё считаем important: лучше
    лишнее уведомление, чем пропущенное письмо.
    """
    if not items:
        return []
    lines = []
    for n, it in enumerate(items, 1):
        purpose = meta.get(it['alias'], {}).get('purpose', '')
        box = f"{it['alias']}" + (f" ({purpose})" if purpose else '')
        lines.append(f"{n}. [{box}] От: {it['from']} | Тема: {it['subject']}")
    prompt = PROMPT.format(items='\n'.join(lines))

    try:
        proc = subprocess.run(
            [gt_claude_bin(), '-p', '--model', CLASSIFIER_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True, timeout=CLASSIFIER_TIMEOUT)
        # claude --output-format json отдаёт МАССИВ событий (rate_limit,
        # system, assistant, result), а не объект. Ответ лежит в элементе
        # с type=result; вызов .get() прямо на списке падал, и всё письмо
        # уходило в fallback «important» — включая явную рекламу.
        data = json.loads(proc.stdout) if proc.stdout.strip() else []
        if isinstance(data, list):
            raw = next((e.get('result', '') for e in reversed(data)
                        if isinstance(e, dict) and e.get('type') == 'result'), '')
        else:
            raw = data.get('result', '')
        if not raw:
            raise ValueError(f'пустой ответ, rc={proc.returncode}, '
                             f'stderr={proc.stderr[:200]}')
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        verdicts = json.loads(m.group(0)) if m else []
        if not verdicts:
            raise ValueError(f'не нашёл JSON в ответе: {raw[:200]}')
    except Exception as e:
        log(f'классификатор недоступен ({e}) — считаю всё important')
        verdicts = []

    by_i = {v.get('i'): v for v in verdicts if isinstance(v, dict)}
    out = []
    for n, it in enumerate(items, 1):
        v = by_i.get(n, {})
        cat = v.get('category', 'important')
        out.append({**it,
                    'category': cat if cat in EMOJI else 'important',
                    'why': v.get('why', '')})
    return out


def gt_claude_bin() -> str:
    return os.environ.get('CLAUDE_BIN', 'claude')


# ============================================================
# Проход
# ============================================================

def _notify(sorted_items: list) -> None:
    chat = _chat_id()
    hot = [i for i in sorted_items if i['category'] in NOTIFY]
    cold = [i for i in sorted_items if i['category'] not in NOTIFY]

    for it in hot[:MAX_NOTIFY]:
        text = (f"{EMOJI[it['category']]} {it['subject']}\n"
                f"от: {it['from']}\n"
                f"ящик: {it['alias']}")
        if it['why']:
            text += f"\n{it['why']}"
        try:
            _tg('sendMessage', chat_id=chat, text=text,
                disable_web_page_preview=True)
        except Exception as e:
            log(f'не отправил уведомление: {e}')

    tail = []
    if len(hot) > MAX_NOTIFY:
        tail.append(f'ещё {len(hot) - MAX_NOTIFY} важных')
    if cold:
        r = sum(1 for i in cold if i['category'] == 'routine')
        n = sum(1 for i in cold if i['category'] == 'noise')
        parts = ([f'{r} обычных'] if r else []) + ([f'{n} рассылок'] if n else [])
        tail.append(', '.join(parts))
    if tail:
        try:
            _tg('sendMessage', chat_id=chat, text='· ' + '; '.join(tail),
                disable_web_page_preview=True)
        except Exception as e:
            log(f'не отправил дайджест: {e}')


def _pass_once(state: dict) -> int:
    aliases = sorted(p.stem for p in gt.TOKENS.glob('*.json'))
    if not aliases:
        log('нет подключённых ящиков')
        return 0
    meta = gt._load_meta()
    found = []
    for alias in aliases:
        try:
            items = _new_messages(alias, state)
        except SystemExit as e:
            log(f'{alias}: {e}')
            continue
        except Exception as e:
            log(f'{alias}: ошибка опроса: {e}')
            continue
        if items:
            log(f'{alias}: новых писем {len(items)}')
            found.extend(items)
    _save_state(state)

    if not found:
        return 0
    sorted_items = []
    for i in range(0, len(found), BATCH):
        sorted_items.extend(_classify(found[i:i + BATCH], meta))
    for it in sorted_items:
        log(f"  {EMOJI[it['category']]} {it['category']:9} "
            f"{it['subject'][:50]}")
    _notify(sorted_items)
    return len(sorted_items)


def cmd_once(args) -> int:
    state = _load_state()
    n = _pass_once(state)
    log(f'проход завершён, писем обработано: {n}')
    return 0


def cmd_run(args) -> int:
    log(f'воркер запущен, интервал {POLL_INTERVAL}с, модель {CLASSIFIER_MODEL}')
    state = _load_state()
    while True:
        try:
            _pass_once(state)
        except Exception as e:
            log(f'проход упал: {e}')
        time.sleep(POLL_INTERVAL)


def cmd_status(args) -> int:
    state = _load_state()
    print(f'chat_id: {state.get("chat_id", "— не найден, запусти discover")}')
    boxes = state.get('boxes', {})
    meta = gt._load_meta()
    for path in sorted(gt.TOKENS.glob('*.json')):
        alias = path.stem
        hid = boxes.get(alias, {}).get('history_id')
        purpose = meta.get(alias, {}).get('purpose', '')
        print(f'  {alias:30} {"следим" if hid else "ещё не опрошен":16} {purpose}')
    return 0


HANDLERS = {'discover': cmd_discover, 'once': cmd_once, 'run': cmd_run,
            'status': cmd_status}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='mail_watch.py', description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    sp_discover = None
    for name, help_ in (('discover', 'найти chat_id по сообщению боту'),
                        ('once', 'один проход'),
                        ('run', 'цикл (для systemd)'),
                        ('status', 'что воркер знает про ящики')):
        sp = sub.add_parser(name, help=help_)
        if name == 'discover':
            sp.add_argument('--drop-webhook', action='store_true',
                            help='снять вебхук, если он мешает getUpdates')
    args = p.parse_args(argv)
    return HANDLERS[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
