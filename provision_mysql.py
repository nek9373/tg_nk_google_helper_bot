#!/usr/bin/env python3
"""Provision a least-privilege application database on DigitalOcean MySQL.

The DigitalOcean API token is read locally.  The generated service password is
written directly to a 0600 config file and is never printed.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


API = "https://api.digitalocean.com/v2"
DEFAULT_TOKEN = Path.home() / ".config/aeolian/do_token"
DEFAULT_CONFIG = Path.home() / ".config/agent_gmail/mysql.json"
DEFAULT_CA = Path.home() / ".config/agent_gmail/do_mysql_ca.crt"


class Api:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            API + path, data=payload, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"DigitalOcean API {method} {path}: {exc.code}: {detail}") from exc
        return json.loads(raw) if raw else {}

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)

    def put(self, path: str, body: dict) -> dict:
        return self.request("PUT", path, body)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)


def _all(api: Api, path: str, key: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    join = "&" if "?" in path else "?"
    while True:
        payload = api.get(f"{path}{join}per_page=200&page={page}")
        batch = payload.get(key, [])
        items.extend(batch)
        if len(batch) < 200:
            return items
        page += 1


def _public_ip() -> str:
    values = []
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        with urllib.request.urlopen(url, timeout=15) as response:
            values.append(response.read().decode("ascii").strip())
    if len(set(values)) != 1:
        raise RuntimeError(f"источники public IP не сошлись: {values}")
    return str(ipaddress.ip_address(values[0]))


def _ensure_firewall(api: Api, cid: str, trusted_ip: str) -> None:
    current = api.get(f"/databases/{cid}/firewall").get("rules", [])
    rules = []
    for item in current:
        rule = {"type": item["type"], "value": item["value"]}
        if item.get("description"):
            rule["description"] = item["description"]
        rules.append(rule)
    expected = {("ip_addr", trusted_ip)}
    existing = {(item["type"], item["value"]) for item in rules}
    if not expected <= existing:
        rules.append(
            {
                "type": "ip_addr",
                "value": trusted_ip,
                "description": "nk_google_helper mail watcher",
            }
        )
        api.put(f"/databases/{cid}/firewall", {"rules": rules})
    readback = api.get(f"/databases/{cid}/firewall").get("rules", [])
    if ("ip_addr", trusted_ip) not in {
        (item.get("type"), item.get("value")) for item in readback
    }:
        raise RuntimeError("trusted source не появился в readback firewall")


def _revoke_current_grants(cursor, account: str) -> None:
    """Revoke only grants SHOW GRANTS actually reports.

    Managed doadmin cannot run broad REVOKE ALL because that operation touches
    protected system schemas. Exact scoped revokes avoid that provider trap.
    """
    cursor.execute(f"SHOW GRANTS FOR {account}")
    grants = [str(row[0]) for row in cursor.fetchall()]
    for grant in grants:
        match = re.match(r"^GRANT (.+?) ON (.+?) TO ", grant, re.I)
        if not match:
            raise RuntimeError(f"неизвестный формат SHOW GRANTS: {grant}")
        privileges, scope = match.groups()
        if privileges.upper() == "USAGE":
            continue
        if not re.fullmatch(r"[A-Za-z0-9_`,\".* ]+", privileges + scope):
            raise RuntimeError(f"небезопасный SHOW GRANTS fragment: {grant}")
        cursor.execute(f"REVOKE {privileges} ON {scope} FROM {account}")
        if "WITH GRANT OPTION" in grant.upper():
            cursor.execute(f"REVOKE GRANT OPTION ON {scope} FROM {account}")


def _secret_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def provision(args: argparse.Namespace) -> int:
    for label, value in (("database", args.database), ("user", args.user)):
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise SystemExit(f"{label} может содержать только A-Z, a-z, 0-9 и _")
    if args.database != "agent_mail" or args.user != "mail_watch":
        raise SystemExit(
            "этот provisioner fail-closed: database=agent_mail и user=mail_watch"
        )
    try:
        token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"не читается DigitalOcean token: {exc}")
    if not token:
        raise SystemExit("DigitalOcean token пуст")
    api = Api(token)

    clusters = api.get("/databases?per_page=200").get("databases", [])
    matches = [item for item in clusters if item.get("name") == args.cluster]
    if len(matches) != 1:
        raise SystemExit(
            f"ожидался ровно один кластер {args.cluster!r}, найдено {len(matches)}"
        )
    cluster = matches[0]
    if cluster.get("engine") != "mysql" or cluster.get("status") != "online":
        raise SystemExit(
            f"кластер не готов: engine={cluster.get('engine')} status={cluster.get('status')}"
        )
    cid = cluster["id"]
    trusted_ip = _public_ip() if args.trusted_ip == "auto" else str(
        ipaddress.ip_address(args.trusted_ip)
    )
    _ensure_firewall(api, cid, trusted_ip)

    databases = {d["name"] for d in _all(api, f"/databases/{cid}/dbs", "dbs")}
    if args.database not in databases:
        api.post(f"/databases/{cid}/dbs", {"name": args.database})
        created_database = True
    else:
        created_database = False

    users = {
        u["name"]: u for u in _all(api, f"/databases/{cid}/users", "users")
    }
    config_path = args.config.expanduser()
    old_config = {}
    if config_path.exists():
        try:
            old_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old_config = {}

    if args.user in users:
        known = (
            old_config.get("cluster_id") == cid
            and old_config.get("database") == args.database
            and old_config.get("user") == args.user
        )
        if not known or not old_config.get("password"):
            raise SystemExit(
                f"существующий {args.user!r} не подтверждён exact config {config_path}; "
                "чужого пользователя не перепрофилирую"
            )
        password = old_config["password"]
        created_user = False
    else:
        response = api.post(f"/databases/{cid}/users", {"name": args.user})
        user = response.get("user") or {}
        password = user.get("password")
        if not password:
            raise RuntimeError("DigitalOcean создал пользователя без пароля в ответе")
        created_user = True

    details = api.get(f"/databases/{cid}").get("database", {})
    connection = details.get("connection") or {}
    certificate_raw = (
        api.get(f"/databases/{cid}/ca").get("ca", {}).get("certificate", "")
    )
    certificate = certificate_raw
    if certificate and not certificate.startswith("-----BEGIN CERTIFICATE-----"):
        try:
            certificate = base64.b64decode(certificate, validate=True).decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("DigitalOcean вернул CA в неизвестном формате") from exc
    if not certificate.startswith("-----BEGIN CERTIFICATE-----"):
        raise RuntimeError("DigitalOcean не вернул CA certificate")
    ca_path = args.ca.expanduser()
    _secret_write(ca_path, certificate.rstrip() + "\n")

    # DigitalOcean's freshly created "normal" MySQL users currently receive
    # broad global grants. Grant DDL only for the versioned migration, then
    # leave the running worker with DML on its own schema.
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("PyMySQL нужен для ограничения прав пользователя") from exc
    try:
        admin = pymysql.connect(
            host=connection["host"],
            port=int(connection["port"]),
            user=connection["user"],
            password=connection["password"],
            database=connection["database"],
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
            ssl={"ca": str(ca_path), "check_hostname": True},
        )
        account = f"'{args.user}'@'%'"
        database_ident = f"`{args.database}`"
        try:
            with admin.cursor() as cursor:
                _revoke_current_grants(cursor, account)
                cursor.execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
                    f"REFERENCES ON {database_ident}.* TO {account}"
                )

            config = {
                "cluster_id": cid,
                "cluster_name": args.cluster,
                "host": connection["host"],
                "port": int(connection["port"]),
                "user": args.user,
                "password": password,
                "database": args.database,
                "ca": str(ca_path),
            }
            temp_config = config_path.with_suffix(config_path.suffix + ".provisioning")
            _secret_write(
                temp_config, json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            )
            try:
                from mail_store import MailStore
                migrated = MailStore(temp_config, migrate=True)
                migrated.close()
            finally:
                temp_config.unlink(missing_ok=True)

            with admin.cursor() as cursor:
                _revoke_current_grants(cursor, account)
                cursor.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {database_ident}.* TO {account}"
                )
                cursor.execute(f"SHOW GRANTS FOR {account}")
                grants = [str(row[0]) for row in cursor.fetchall()]
        finally:
            admin.close()

        joined = "\n".join(grants).upper()
        required = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        forbidden = {"CREATE", "ALTER", "INDEX", "REFERENCES", "GRANT OPTION"}
        schema_scoped = any(
            marker in joined
            for marker in (
                f"`{args.database.upper()}`.*",
                f'"{args.database.upper()}".*',
            )
        )
        if not all(word in joined for word in required) or any(
            word in joined for word in forbidden
        ) or not schema_scoped:
            raise RuntimeError(f"runtime grants не совпали с DML-контрактом: {grants}")

        app = pymysql.connect(
            host=connection["host"], port=int(connection["port"]), user=args.user,
            password=password, database=args.database, charset="utf8mb4",
            autocommit=True, connect_timeout=10,
            ssl={"ca": str(ca_path), "check_hostname": True},
        )
        try:
            with app.cursor() as cursor:
                cursor.execute("SELECT meta_value FROM meta WHERE meta_key='schema_version'")
                if not cursor.fetchone():
                    raise RuntimeError("runtime readback не видит schema_version")
        finally:
            app.close()
    except Exception:
        if created_user:
            try:
                api.delete(f"/databases/{cid}/users/{args.user}")
            except Exception as cleanup_exc:
                raise RuntimeError(
                    f"provisioning упал и broad user cleanup тоже не удался: {cleanup_exc}"
                )
        raise

    _secret_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "cluster": args.cluster,
                "database": args.database,
                "user": args.user,
                "config": str(config_path),
                "created_database": created_database,
                "created_user": created_user,
                "tls_ca": str(ca_path),
                "least_privilege_verified": True,
                "trusted_ip": trusted_ip,
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--database", default="agent_mail")
    parser.add_argument("--user", default="mail_watch")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ca", type=Path, default=DEFAULT_CA)
    parser.add_argument(
        "--trusted-ip", default="auto",
        help="единственный public IP watcher host или auto (двойной readback)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return provision(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
