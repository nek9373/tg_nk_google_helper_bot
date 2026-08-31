#!/usr/bin/env python3
"""Доступ агентов к нескольким почтовым ящикам Google.

Зачем отдельный инструмент. Плагины (codex `gmail@openai-curated`, MCP-
коннекторы) дают ровно один ящик — тот, под которым выполнен вход. Здесь
ящиков несколько, и они в разных Workspace, поэтому у каждого свой
refresh token, а выбор ящика — явный аргумент команды.

Права по умолчанию: ЧИТАТЬ и СОЗДАВАТЬ ЧЕРНОВИКИ. Отправку агент не
получает, пока ящик не подключён с --with-send. Письмо, ушедшее не тому
адресату, не отзывается, а ошибается агент молча — поэтому по умолчанию
он готовит, а кнопку нажимает человек.

Первая настройка (делает Никита, один раз):
    mkdir -p ~/.config/agent_gmail/tokens && chmod 700 ~/.config/agent_gmail
    cp <скачанный client_secret>.json ~/.config/agent_gmail/client_secret.json
    chmod 600 ~/.config/agent_gmail/client_secret.json

Подключение ящика. Имя ящика — сам адрес: в одном домене их бывает
несколько, и короткая метка их перепутает. Браузер сам не открывается,
в консоль печатается ссылка — открой её там, где нужный аккаунт уже
залогинен:
    python3 scripts/gmail_tool.py add me@company.com --purpose "деловая"
    python3 scripts/gmail_tool.py add me@gmail.com --with-send

Если браузер вообще на другой машине — локальный коллбэк оттуда не
дойдёт, тогда --paste: вставишь адрес с кодом руками.
    python3 scripts/gmail_tool.py add me@company.com --paste

Работа (это уже вызывает агент сам):
    python3 scripts/gmail_tool.py list
    python3 scripts/gmail_tool.py search me@company.com "is:unread" -n 5
    python3 scripts/gmail_tool.py read me@company.com <id>
    python3 scripts/gmail_tool.py draft me@company.com --to a@b.c --subject "Т" --body -
    python3 scripts/gmail_tool.py describe me@company.com "для чего ящик"
"""

import argparse
import base64
import json
import os
import sys
import time
from email.message import EmailMessage
from pathlib import Path

HOME_CFG = Path.home() / '.config' / 'agent_gmail'
CLIENT_SECRET = Path(os.environ.get('GMAIL_CLIENT_SECRET',
                                    HOME_CFG / 'client_secret.json'))
TOKENS = Path(os.environ.get('GMAIL_TOKENS_DIR', HOME_CFG / 'tokens'))

READ_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.compose',   # черновики, без отправки
]
SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'

# Consent screen в режиме Testing выдаёт refresh token на 7 дней — потом
# ящик придётся подключать заново. Предупреждаем заранее, а не когда всё
# отвалится посреди работы.
TESTING_TOKEN_TTL = 7 * 24 * 3600

# Назначение ящиков — отдельным файлом, а не внутри токена: токен
# пересоздаётся при каждом переподключении (в режиме Testing это раз в
# неделю), и описание терялось бы вместе с ним. Плюс этот файл не
# секретный: его можно править руками и целиком показывать агенту.
MAILBOXES = Path(os.environ.get("GMAIL_MAILBOXES", HOME_CFG / "mailboxes.json"))


def _load_meta() -> dict:
    try:
        return json.loads(MAILBOXES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_meta(meta: dict) -> None:
    MAILBOXES.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAILBOXES.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, MAILBOXES)


def _set_purpose(alias: str, purpose: str, email: str = "") -> None:
    if not purpose and not email:
        return
    meta = _load_meta()
    entry = meta.get(alias, {})
    if purpose:
        entry["purpose"] = purpose
    if email:
        entry["email"] = email
    meta[alias] = entry
    _save_meta(meta)


def _fail(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _token_path(alias: str) -> Path:
    """Файл токена по алиасу.

    Алиасом служит сам адрес (nikita@ibitcy.com): в одном домене ящиков
    бывает несколько, и короткая метка вроде «ibitcy» их схлопнула бы.
    Символы @ . - _ в именах файлов допустимы, поэтому адрес остаётся
    читаемым; режем только то, что ломает путь.
    """
    safe = ''.join(c for c in alias if c.isalnum() or c in '-_.@')
    if not safe or safe.startswith('.') or '..' in safe or '/' in alias:
        _fail(f'плохой алиас: {alias!r}')
    return TOKENS / f'{safe}.json'


def _load_creds(alias: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    path = _token_path(alias)
    if not path.exists():
        _fail(f'ящик {alias!r} не подключён. Сначала: gmail_tool.py add {alias}')
    creds = Credentials.from_authorized_user_file(str(path))
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_creds(alias, creds, meta_only_touch=True)
        except Exception as e:
            _fail(f'не удалось обновить токен {alias!r}: {e}\n'
                  f'Скорее всего протух refresh token (в режиме Testing он живёт '
                  f'7 дней) или доступ отозван. Переподключи: '
                  f'gmail_tool.py add {alias}')
    return creds


def _save_creds(alias: str, creds, email: str = '', meta_only_touch: bool = False):
    TOKENS.mkdir(parents=True, exist_ok=True)
    path = _token_path(alias)
    data = json.loads(creds.to_json())
    if not meta_only_touch:
        data['_alias'] = alias
        data['_email'] = email
        data['_connected_at'] = time.time()
    elif path.exists():
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
            for k in ('_alias', '_email', '_connected_at'):
                if k in old:
                    data[k] = old[k]
        except json.JSONDecodeError:
            pass
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _service(alias: str):
    from googleapiclient.discovery import build
    return build('gmail', 'v1', credentials=_load_creds(alias),
                 cache_discovery=False)


# ============================================================
# Подключение ящиков
# ============================================================

def cmd_add(args) -> int:
    """Подключает один или несколько ящиков подряд.

    От человека нужна только авторизация, поэтому ввод минимальный:
        gmail_tool.py add me@company.com
    Назначение ящика проставит бот позже через describe.
    """
    rc = 0
    many = len(args.alias) > 1
    for i, alias in enumerate(args.alias):
        if many:
            print(f'\n[{i + 1}/{len(args.alias)}] {alias}')
        rc |= _add_one(alias, args, skip_busy=many)
    return rc


def _add_one(alias: str, args, skip_busy: bool = False) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET.exists():
        _fail(f'нет файла клиента: {CLIENT_SECRET}\n'
              f'Скопируй туда client_secret из Google Cloud и повтори:\n'
              f'  mkdir -p {HOME_CFG} && chmod 700 {HOME_CFG}\n'
              f'  cp <файл>.json {CLIENT_SECRET} && chmod 600 {CLIENT_SECRET}')

    # Алиас — это сам адрес, поэтому --email указывать незачем: берём его
    # из алиаса, и сверка «под кем реально вошли» работает сама.
    email = args.email or (alias if '@' in alias else '')

    scopes = list(READ_SCOPES)
    if args.with_send:
        scopes.append(SEND_SCOPE)

    path = _token_path(alias)
    if path.exists() and not args.force:
        if skip_busy:
            # В пачке занятый ящик — не повод обрывать остальные
            print('  уже подключён, пропускаю')
            return 0
        # Занятый алиас почти всегда означает «человек подключает ВТОРОЙ
        # ящик и взял то же имя», а не «хочет заменить первый». Подсказка
        # про --force в такой ситуации вредна: она стирает рабочий токен.
        try:
            busy = json.loads(path.read_text(encoding='utf-8')).get('_email', '?')
        except (json.JSONDecodeError, OSError):
            busy = '?'
        hint = ''
        if email and email.lower() != str(busy).lower():
            # Однозначный алиас — сам адрес: в одном домене ящиков бывает
            # несколько, и любая короткая метка их перепутает.
            hint = (f'\nЭто ДРУГОЙ ящик — назови его адресом:\n'
                    f'  gmail_tool.py add {email}\n')
        _fail(f'алиас {alias!r} уже занят: {busy}.{hint}'
              f'\nЕсли правда нужно ЗАМЕНИТЬ {busy} — добавь --force '
              f'(старый токен будет отозван и удалён).')

    if args.with_send:
        print('  права: чтение + черновики + ОТПРАВКА')
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), scopes)
    # login_hint подставляет нужный адрес в форму выбора аккаунта; consent
    # форсируем, иначе Google переиспользует прошлое согласие и не отдаст
    # refresh token для второго и последующих ящиков.
    kwargs = {'prompt': 'consent', 'access_type': 'offline'}
    if email:
        kwargs['login_hint'] = email

    if args.paste:
        # Браузер на другой машине: локальный порт оттуда недостижим, поэтому
        # ловим не коллбэк, а вручную вставленный адрес, на который Google
        # перебросил. Код лежит в его query-параметре.
        flow.redirect_uri = 'http://localhost:1/'
        auth_url, _ = flow.authorization_url(**kwargs)
        print('\nОткрой ссылку в любом браузере:\n')
        print(auth_url)
        print('\nПосле подтверждения браузер уйдёт на localhost и покажет '
              'ошибку — это нормально.\nСкопируй из адресной строки ВЕСЬ '
              'адрес (там есть ?code=...) и вставь сюда:')
        pasted = input('> ').strip()
        if not pasted:
            _fail('пустой ответ — ничего не сохранено')
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(pasted).query)
            code = (qs.get('code') or [''])[0] or pasted
            flow.fetch_token(code=code)
        except Exception as e:
            _fail(f'не удалось обменять код: {e}\n'
                  f'Проверь, что вставил адрес целиком, вместе с ?code=...')
        creds = flow.credentials
    else:
        # Браузер не открываем сами — печатаем ссылку, а локальный сервер
        # ждёт коллбэка. Так видно, куда именно идёшь, и можно открыть
        # ссылку в том браузере, где нужный аккаунт уже залогинен.
        creds = flow.run_local_server(
            port=0,
            open_browser=False,
            authorization_prompt_message='\n{url}\n\n  ↑ открой и подтверди\n',
            success_message=(
                'Готово, доступ выдан. Можно закрывать вкладку и вернуться '
                'в терминал.'),
            **kwargs)

    # Сверяем, под каким адресом реально вошли — чтобы алиас не разъехался
    # с ящиком (частая ошибка: браузер помнит другой аккаунт).
    from googleapiclient.discovery import build
    profile = build('gmail', 'v1', credentials=creds,
                    cache_discovery=False).users().getProfile(userId='me').execute()
    real = profile.get('emailAddress', '')
    if email and real.lower() != email.lower():
        _fail(f'вошли под {real}, а просили {email}. Токен НЕ сохранён — '
              f'выйди из лишнего аккаунта в браузере и повтори.')

    _save_creds(alias, creds, email=real)
    # Назначение по умолчанию не спрашиваем: от человека нужна только
    # авторизация, описание допишет бот через describe.
    _set_purpose(alias, args.purpose, email=real)
    print(f'  ✓ {real}')
    return 0


def cmd_list(args) -> int:
    if not TOKENS.exists() or not any(TOKENS.glob('*.json')):
        print('подключённых ящиков нет')
        return 0
    meta = _load_meta()
    for path in sorted(TOKENS.glob('*.json')):
        try:
            d = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            print(f'{path.stem:12} — файл повреждён')
            continue
        scopes = d.get('scopes', [])
        can_send = any(s.endswith('gmail.send') for s in scopes)
        age = time.time() - d.get('_connected_at', 0)
        warn = ''
        if d.get('_connected_at') and age > TESTING_TOKEN_TTL:
            warn = '  ⚠ старше 7 дней — если consent screen в Testing, уже протух'
        purpose = meta.get(path.stem, {}).get("purpose", "")
        rights = "чтение+черновики+ОТПРАВКА" if can_send else "чтение+черновики"
        print(f"{path.stem:26} {rights}{warn}")
        print(f"    {purpose or '— назначение не указано'}")
    return 0


def cmd_describe(args) -> int:
    """Задать или изменить назначение ящика."""
    if not _token_path(args.alias).exists():
        _fail(f'ящик {args.alias!r} не подключён')
    purpose = args.purpose or getattr(args, 'purpose_flag', '')
    if not purpose:
        _fail(f'нечего записывать. Пример:\n  gmail_tool.py describe {args.alias} "для чего этот ящик"')
    args.purpose = purpose
    _set_purpose(args.alias, purpose)
    print(f'{args.alias}: {args.purpose}')
    return 0


def cmd_rename(args) -> int:
    """Переименовать алиас, не трогая сам доступ.

    Имя ящика — то, чем агент выбирает, куда лезть, поэтому оно должно
    быть говорящим: увидев work, агент полезет в рабочую почту, даже если
    исторически так назвали личную.
    """
    src, dst = _token_path(args.old), _token_path(args.new)
    if not src.exists():
        _fail(f'ящик {args.old!r} не подключён')
    if dst.exists():
        try:
            busy = json.loads(dst.read_text(encoding='utf-8')).get('_email', '?')
        except (json.JSONDecodeError, OSError):
            busy = '?'
        _fail(f'алиас {args.new!r} уже занят: {busy}')
    data = json.loads(src.read_text(encoding='utf-8'))
    data['_alias'] = args.new      # метаданные не должны врать про имя
    tmp = dst.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)
    src.unlink()
    meta = _load_meta()          # описание должно переехать вместе с ящиком
    if args.old in meta:
        meta[args.new] = meta.pop(args.old)
        _save_meta(meta)
    print(f'{args.old} → {args.new}  ({data.get("_email", "?")})')
    return 0


def cmd_revoke(args) -> int:
    import urllib.request
    import urllib.parse
    path = _token_path(args.alias)
    if not path.exists():
        print(f'ящик {args.alias!r} и так не подключён')
        return 0
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        token = d.get('refresh_token') or d.get('token')
        if token:
            urllib.request.urlopen(
                'https://oauth2.googleapis.com/revoke',
                data=urllib.parse.urlencode({'token': token}).encode(),
                timeout=15)
            print('доступ отозван на стороне Google')
    except Exception as e:
        print(f'отзыв на стороне Google не удался ({e}) — удаляю локально',
              file=sys.stderr)
    path.unlink()
    meta = _load_meta()
    if meta.pop(args.alias, None) is not None:
        _save_meta(meta)
    print(f'токен {args.alias} удалён')
    return 0


# ============================================================
# Чтение
# ============================================================

def _header(msg, name, default=''):
    for h in msg.get('payload', {}).get('headers', []):
        if h.get('name', '').lower() == name.lower():
            return h.get('value', default)
    return default


def cmd_search(args) -> int:
    svc = _service(args.alias)
    res = svc.users().messages().list(
        userId='me', q=args.query, maxResults=args.limit).execute()
    ids = [m['id'] for m in res.get('messages', [])]
    if not ids:
        print('ничего не найдено')
        return 0
    for mid in ids:
        msg = svc.users().messages().get(
            userId='me', id=mid, format='metadata',
            metadataHeaders=['From', 'Subject', 'Date']).execute()
        print(f"{mid}  {_header(msg,'Date')[:25]:25} "
              f"{_header(msg,'From')[:38]:38} {_header(msg,'Subject')[:60]}")
        if args.snippet and msg.get('snippet'):
            print(f"      {msg['snippet'][:150]}")
    return 0


def _extract_body(payload) -> str:
    """Достаёт текст письма, предпочитая text/plain."""
    if payload.get('mimeType') == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', 'replace')
    for part in payload.get('parts', []) or []:
        text = _extract_body(part)
        if text:
            return text
    # Ни одной plain-части: отдаём html как есть, разбирать его — не наша забота
    if payload.get('mimeType', '').startswith('text/'):
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', 'replace')
    return ''


def cmd_read(args) -> int:
    svc = _service(args.alias)
    msg = svc.users().messages().get(
        userId='me', id=args.msg_id, format='full').execute()
    print(f"От:   {_header(msg,'From')}")
    print(f"Кому: {_header(msg,'To')}")
    print(f"Дата: {_header(msg,'Date')}")
    print(f"Тема: {_header(msg,'Subject')}")
    print('-' * 70)
    body = _extract_body(msg.get('payload', {})) or msg.get('snippet', '')
    print(body[:args.limit_chars])
    if len(body) > args.limit_chars:
        print(f'\n[…обрезано, всего {len(body)} символов]')
    return 0


# ============================================================
# Черновик и отправка
# ============================================================

def _build_message(args) -> EmailMessage:
    body = args.body
    if body == '-':
        body = sys.stdin.read()
    m = EmailMessage()
    m['To'] = args.to
    m['Subject'] = args.subject
    if args.cc:
        m['Cc'] = args.cc
    m.set_content(body)
    return m


def cmd_draft(args) -> int:
    svc = _service(args.alias)
    raw = base64.urlsafe_b64encode(_build_message(args).as_bytes()).decode()
    draft = svc.users().drafts().create(
        userId='me', body={'message': {'raw': raw}}).execute()
    print(f"черновик создан: {draft['id']}")
    print('Он лежит в «Черновиках» — проверь и отправь сам.')
    return 0


def cmd_send(args) -> int:
    """Отправка. Доступна только если ящик подключён с --with-send."""
    creds = _load_creds(args.alias)
    if not any(s.endswith('gmail.send') for s in (creds.scopes or [])):
        _fail(f'у ящика {args.alias!r} нет права отправки. Это намеренно: '
              f'сделай черновик (draft) или переподключи ящик с --with-send, '
              f'если отправка от агента действительно нужна.')
    if not args.yes:
        _fail('отправка требует явного --yes: письмо не отзывается')
    svc = _service(args.alias)
    raw = base64.urlsafe_b64encode(_build_message(args).as_bytes()).decode()
    sent = svc.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"отправлено: {sent['id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='gmail_tool.py',
        description='Доступ агентов к нескольким почтовым ящикам Google')
    sub = p.add_subparsers(dest='cmd', required=True)

    sp = sub.add_parser('add', help='подключить ящик (откроет браузер)')
    sp.add_argument('alias', nargs='+',
                    help='адрес ящика (можно несколько подряд)')
    sp.add_argument('--email', default='',
                    help=argparse.SUPPRESS)   # редкий случай: имя ≠ адрес
    sp.add_argument('--purpose', default='', help=argparse.SUPPRESS)
    sp.add_argument('--with-send', action='store_true',
                    help='дать право ОТПРАВКИ (по умолчанию только черновики)')
    sp.add_argument('--force', action='store_true', help='перезаписать существующий')
    sp.add_argument('--paste', action='store_true',
                    help='браузер на другой машине: вставить адрес с кодом вручную')

    sub.add_parser('list', help='какие ящики подключены и с какими правами')

    sp = sub.add_parser('describe', help='задать назначение ящика')
    sp.add_argument('alias')
    # И позиционно, и флагом: у add описание задаётся через --purpose,
    # и разнобой между командами только путает.
    sp.add_argument('purpose', nargs='?', default='')
    sp.add_argument('--purpose', dest='purpose_flag', default='')

    sp = sub.add_parser('rename', help='переименовать алиас (доступ не трогается)')
    sp.add_argument('old')
    sp.add_argument('new')

    sp = sub.add_parser('revoke', help='отозвать доступ и удалить токен')
    sp.add_argument('alias')

    sp = sub.add_parser('search', help='поиск писем (синтаксис Gmail)')
    sp.add_argument('alias')
    sp.add_argument('query')
    sp.add_argument('-n', '--limit', type=int, default=10)
    sp.add_argument('--snippet', action='store_true', help='показать превью')

    sp = sub.add_parser('read', help='прочитать письмо')
    sp.add_argument('alias')
    sp.add_argument('msg_id')
    sp.add_argument('--limit-chars', type=int, default=4000)

    for name, help_ in (('draft', 'создать черновик'),
                        ('send', 'отправить (нужен ящик с --with-send)')):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument('alias')
        sp.add_argument('--to', required=True)
        sp.add_argument('--subject', required=True)
        sp.add_argument('--body', required=True, help='текст или "-" для stdin')
        sp.add_argument('--cc', default='')
        if name == 'send':
            sp.add_argument('--yes', action='store_true',
                            help='подтверждение: письмо не отзывается')
    return p


HANDLERS = {'add': cmd_add, 'list': cmd_list, 'rename': cmd_rename,
            'describe': cmd_describe,
            'revoke': cmd_revoke,
            'search': cmd_search, 'read': cmd_read, 'draft': cmd_draft,
            'send': cmd_send}


def main(argv=None) -> int:
    # Частая ошибка: пишут адрес первым аргументом, забыв команду.
    # Голое argparse-сообщение «invalid choice» в этом не помогает.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and '@' in raw[0] and raw[0] not in HANDLERS:
        known = _token_path(raw[0]).exists()
        what = 'describe' if known else 'add'
        # Кавычки обязательны: подсказку копируют целиком, а без них
        # многословное описание развалится на отдельные аргументы
        import shlex
        tail = ' '.join(shlex.quote(a) for a in raw[1:])
        _fail(f'не хватает команды перед адресом.\n'
              f'Этот ящик {"уже подключён" if known else "ещё не подключён"}, значит нужно:\n'
              f'  gmail_tool.py {what} {raw[0]} {tail}'.rstrip())
    args = build_parser().parse_args(argv)
    return HANDLERS[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
