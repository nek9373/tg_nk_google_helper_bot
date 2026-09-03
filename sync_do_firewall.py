#!/usr/bin/env python3
"""Keep the mail watcher's DigitalOcean trusted-source IPv4 current.

The command is deliberately fail-closed: two independent public-IP services
must agree and the database cluster id must come from the existing runtime
config. A concurrent firewall change detected by the preflight read aborts the
update. Only the rule carrying the exact managed description is replaced;
every other rule in that snapshot is preserved.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable


API_ROOT = "https://api.digitalocean.com/v2"
DEFAULT_CONFIG = Path.home() / ".config/agent_gmail/mysql.json"
DEFAULT_TOKEN = Path.home() / ".config/aeolian/do_token"
MANAGED_DESCRIPTION = "nk_google_helper mail watcher"
PUBLIC_IPV4_URLS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)

Opener = Callable[..., object]


def _read_nonempty(path: Path, label: str) -> str:
    try:
        value = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"не читается {label} ({path.expanduser()}): {exc}") from exc
    if not value:
        raise RuntimeError(f"{label} пуст ({path.expanduser()})")
    return value


def _cluster_id(config_path: Path) -> str:
    raw = _read_nonempty(config_path, "MySQL config")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MySQL config содержит некорректный JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("MySQL config должен быть JSON-объектом")
    value = config.get("cluster_id")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("в MySQL config нет непустого cluster_id")
    try:
        parsed = uuid.UUID(value.strip())
    except ValueError as exc:
        raise RuntimeError("cluster_id в MySQL config не является UUID") from exc
    return str(parsed)


def _response_bytes(opener: Opener, request, timeout: float) -> bytes:
    try:
        with opener(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        method = request.get_method() if isinstance(request, urllib.request.Request) else "GET"
        raise RuntimeError(f"HTTPS {method} завершился HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HTTPS-запрос недоступен: {exc.reason}") from exc


def resolve_public_ipv4(*, opener: Opener = urllib.request.urlopen, timeout: float = 15) -> str:
    values: list[str] = []
    for url in PUBLIC_IPV4_URLS:
        raw = _response_bytes(opener, url, timeout)
        try:
            candidate = raw.decode("ascii").strip()
            address = ipaddress.ip_address(candidate)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("источник public IP вернул невалидный адрес") from exc
        if address.version != 4:
            raise RuntimeError("источник public IP вернул не IPv4")
        values.append(str(address))
    if len(values) != 2 or values[0] != values[1]:
        raise RuntimeError("два независимых источника public IPv4 не сошлись")
    return values[0]


class DigitalOceanApi:
    def __init__(
        self,
        token: str,
        *,
        opener: Opener = urllib.request.urlopen,
        timeout: float = 30,
    ):
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            API_ROOT + path,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "nk-google-helper-firewall-sync/1",
            },
            method=method,
        )
        raw = _response_bytes(self._opener, request, self._timeout)
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DigitalOcean API {method} вернул не JSON") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"DigitalOcean API {method} вернул не объект")
        return decoded

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def put(self, path: str, body: dict) -> dict:
        return self.request("PUT", path, body)


def _writable_rule(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RuntimeError("DigitalOcean firewall содержит правило не-объект")
    rule_type = raw.get("type")
    value = raw.get("value")
    description = raw.get("description")
    if not isinstance(rule_type, str) or not rule_type:
        raise RuntimeError("DigitalOcean firewall rule не содержит type")
    if not isinstance(value, str) or not value:
        raise RuntimeError("DigitalOcean firewall rule не содержит value")
    if description is not None and not isinstance(description, str):
        raise RuntimeError("DigitalOcean firewall rule содержит нестроковый description")
    result = {"type": rule_type, "value": value}
    if description:
        result["description"] = description
    return result


def _firewall_rules(api: DigitalOceanApi, cluster_id: str) -> list[dict[str, str]]:
    payload = api.get(f"/databases/{cluster_id}/firewall")
    if "rules" not in payload or not isinstance(payload["rules"], list):
        raise RuntimeError("DigitalOcean firewall response не содержит список rules")
    return [_writable_rule(item) for item in payload["rules"]]


def _canonical(rules: list[dict[str, str]]) -> list[str]:
    return sorted(json.dumps(rule, sort_keys=True, separators=(",", ":")) for rule in rules)


def _desired_rules(current: list[dict[str, str]], public_ip: str) -> list[dict[str, str]]:
    desired: list[dict[str, str]] = []
    insertion_index: int | None = None
    target_identity = ("ip_addr", public_ip)
    for rule in current:
        if rule.get("description") == MANAGED_DESCRIPTION:
            if insertion_index is None:
                insertion_index = len(desired)
            continue
        if (rule["type"], rule["value"]) == target_identity:
            raise RuntimeError(
                "текущий public IPv4 уже занят чужим firewall rule; чужое правило не меняю"
            )
        desired.append(dict(rule))
    managed = {
        "type": "ip_addr",
        "value": public_ip,
        "description": MANAGED_DESCRIPTION,
    }
    desired.insert(insertion_index if insertion_index is not None else len(desired), managed)
    return desired


def synchronize(
    config_path: Path,
    token_path: Path,
    *,
    opener: Opener = urllib.request.urlopen,
    ip_timeout: float = 15,
    api_timeout: float = 30,
) -> bool:
    cluster_id = _cluster_id(config_path)
    token = _read_nonempty(token_path, "DigitalOcean token")
    public_ip = resolve_public_ipv4(opener=opener, timeout=ip_timeout)
    api = DigitalOceanApi(token, opener=opener, timeout=api_timeout)

    current = _firewall_rules(api, cluster_id)
    desired = _desired_rules(current, public_ip)
    changed = _canonical(current) != _canonical(desired)
    path = f"/databases/{cluster_id}/firewall"

    if changed:
        # A full firewall PUT has no ETag/CAS.  A second GET narrows the race and
        # prevents overwriting a foreign rule that changed during this run.
        preflight = _firewall_rules(api, cluster_id)
        if _canonical(preflight) != _canonical(current):
            raise RuntimeError("firewall изменился между чтениями; PUT отменён")
        api.put(path, {"rules": desired})

    readback = _firewall_rules(api, cluster_id)
    if _canonical(readback) != _canonical(desired):
        raise RuntimeError("exact readback DigitalOcean firewall не совпал с ожидаемым")
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--ip-timeout", type=float, default=15)
    parser.add_argument("--api-timeout", type=float, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.ip_timeout <= 0 or args.api_timeout <= 0:
        print("mail-watch firewall sync: timeout должен быть больше нуля", file=sys.stderr)
        return 2
    try:
        changed = synchronize(
            args.config,
            args.token_file,
            ip_timeout=args.ip_timeout,
            api_timeout=args.api_timeout,
        )
    except Exception as exc:
        print(f"mail-watch firewall sync: FAILED: {exc}", file=sys.stderr)
        return 1
    state = "updated" if changed else "already-current"
    print(f"mail-watch firewall sync: OK ({state})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
