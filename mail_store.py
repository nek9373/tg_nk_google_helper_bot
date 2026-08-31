#!/usr/bin/env python3
"""Durable MySQL state for the Gmail -> Telegram watcher.

The Gmail history cursor and discovered message ids are committed in one
transaction. Delivery happens later and is intentionally at-least-once: an
ambiguous Telegram timeout may duplicate an alert, but must never erase mail.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 3
DEFAULT_CONFIG = Path.home() / ".config/agent_gmail/mysql.json"


def message_token(mailbox: str, gmail_id: str) -> str:
    raw = f"{mailbox}\0{gmail_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class MailStore:
    """Repository for the central DigitalOcean MySQL database."""

    def __init__(
        self,
        config_path: Path | str = DEFAULT_CONFIG,
        *,
        migrate: bool | None = None,
    ):
        try:
            import pymysql
            import pymysql.cursors
        except ImportError as exc:
            raise RuntimeError(
                "PyMySQL не установлен: .venv/bin/pip install -r requirements.txt"
            ) from exc

        self._pymysql = pymysql
        self.config_path = Path(config_path).expanduser()
        try:
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeError(f"не читается MySQL config {self.config_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MySQL config повреждён: {self.config_path}: {exc}") from exc
        required = {"host", "port", "user", "password", "database", "ca"}
        missing = sorted(required - self.config.keys())
        if missing:
            raise RuntimeError(f"в MySQL config нет полей: {', '.join(missing)}")
        ca = Path(self.config["ca"]).expanduser()
        if not ca.is_file():
            raise RuntimeError(f"не найден CA certificate: {ca}")

        self._ca = ca
        self._lease_db = None
        self.db = self._connect()
        if migrate is None:
            migrate = os.environ.get("MAIL_WATCH_SCHEMA_MIGRATE", "0") == "1"
        if migrate:
            self._init_schema()
        self._validate_schema()

    def _connect(self):
        return self._pymysql.connect(
            host=self.config["host"],
            port=int(self.config["port"]),
            user=self.config["user"],
            password=self.config["password"],
            database=self.config["database"],
            charset="utf8mb4",
            cursorclass=self._pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            ssl={"ca": str(self._ca), "check_hostname": True},
        )

    def _ensure_connection(self) -> None:
        try:
            self.db.ping()
        except Exception:
            try:
                self.db.close()
            except Exception:
                pass
            self.db = self._connect()

    def close(self) -> None:
        self.release_worker_lease()
        self.db.close()

    def acquire_worker_lease(self, name: str = "nk_google_helper_mail_watch") -> None:
        """Take a MySQL named lock before any Gmail/Telegram polling."""
        if self._lease_db is not None:
            return
        lease_db = self._connect()
        try:
            with lease_db.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (name,))
                row = cursor.fetchone() or {}
            if int(row.get("acquired") or 0) != 1:
                raise RuntimeError("mail watcher уже запущен на другом хосте")
        except Exception:
            lease_db.close()
            raise
        self._lease_db = lease_db

    def release_worker_lease(self, name: str = "nk_google_helper_mail_watch") -> None:
        lease_db, self._lease_db = self._lease_db, None
        if lease_db is None:
            return
        try:
            with lease_db.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
        finally:
            lease_db.close()

    @contextmanager
    def _tx(self):
        self._ensure_connection()
        cursor = self.db.cursor()
        try:
            yield cursor
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            cursor.close()

    def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        self._ensure_connection()
        with self.db.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        self._ensure_connection()
        with self.db.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    def _init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS meta (
                meta_key VARCHAR(128) PRIMARY KEY,
                meta_value TEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS mailbox_state (
                mailbox VARCHAR(320) PRIMARY KEY,
                history_id VARCHAR(64),
                last_scan_at BIGINT,
                last_error VARCHAR(1000)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                token CHAR(16) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
                mailbox VARCHAR(320) NOT NULL,
                gmail_id VARCHAR(64) NOT NULL,
                thread_id VARCHAR(64),
                sender VARCHAR(1000),
                sender_email VARCHAR(320),
                subject VARCHAR(1000),
                received_at VARCHAR(128),
                gmail_labels JSON,
                mailing_list BOOLEAN NOT NULL DEFAULT FALSE,
                discovered_at BIGINT NOT NULL,
                metadata_attempts INT NOT NULL DEFAULT 0,
                metadata_next_attempt_at BIGINT,
                metadata_dead_at BIGINT,
                category VARCHAR(16),
                confidence DOUBLE,
                why VARCHAR(1000),
                classifier_source VARCHAR(64),
                classified_at BIGINT,
                delivered_at BIGINT,
                delivery_kind VARCHAR(16),
                telegram_message_id BIGINT,
                delivery_attempts INT NOT NULL DEFAULT 0,
                user_category VARCHAR(16),
                feedback_at BIGINT,
                promotion_pending BOOLEAN NOT NULL DEFAULT FALSE,
                last_error VARCHAR(1000),
                UNIQUE KEY uq_mailbox_gmail (mailbox, gmail_id),
                KEY idx_hydrate (sender_email, discovered_at),
                KEY idx_classify (category, discovered_at),
                KEY idx_deliver (delivered_at, category, discovered_at),
                KEY idx_feedback_sender (sender_email, feedback_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                callback_id VARCHAR(128) NOT NULL UNIQUE,
                token CHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                previous_category VARCHAR(16),
                chosen_category VARCHAR(16) NOT NULL,
                created_at BIGINT NOT NULL,
                CONSTRAINT fk_feedback_message FOREIGN KEY(token)
                    REFERENCES messages(token)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS subscriber_deliveries (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                subscriber VARCHAR(64) NOT NULL,
                token CHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                topic VARCHAR(64) NOT NULL,
                queued_at BIGINT NOT NULL,
                delivered_at BIGINT,
                delivery_attempts INT NOT NULL DEFAULT 0,
                last_error VARCHAR(1000),
                UNIQUE KEY uq_subscriber_message (subscriber, token),
                KEY idx_subscriber_pending (subscriber, delivered_at, queued_at),
                CONSTRAINT fk_subscriber_message FOREIGN KEY(token)
                    REFERENCES messages(token)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        ]
        with self._tx() as cursor:
            for statement in statements:
                cursor.execute(statement)
            # Keep upgrades explicit: the production table can predate columns
            # present in CREATE TABLE IF NOT EXISTS.
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'messages'"
            )
            columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
            upgrades = {
                "metadata_next_attempt_at": "BIGINT",
                "metadata_dead_at": "BIGINT",
                "promotion_pending": "BOOLEAN NOT NULL DEFAULT FALSE",
            }
            for name, ddl in upgrades.items():
                if name not in columns:
                    cursor.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
            cursor.execute(
                "INSERT INTO meta(meta_key, meta_value) VALUES(%s, %s) "
                "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def _validate_schema(self) -> None:
        try:
            row = self._fetchone(
                "SELECT meta_value FROM meta WHERE meta_key = 'schema_version'"
            )
        except Exception as exc:
            raise RuntimeError(
                "schema agent_mail не готов; запусти provision_mysql.py"
            ) from exc
        version = int((row or {}).get("meta_value") or 0)
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"schema agent_mail={version}, код требует {SCHEMA_VERSION}; "
                "запусти provision_mysql.py"
            )

    def rename_mailbox(self, old: str, new: str) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "SELECT mailbox FROM mailbox_state WHERE mailbox = %s FOR UPDATE", (new,)
            )
            if cursor.fetchone():
                raise RuntimeError(f"mailbox {new!r} уже есть в watcher DB")
            cursor.execute("UPDATE messages SET mailbox = %s WHERE mailbox = %s", (new, old))
            cursor.execute(
                "UPDATE mailbox_state SET mailbox = %s WHERE mailbox = %s", (new, old)
            )

    def retire_mailbox(self, mailbox: str, *, now: int | None = None) -> None:
        when = int(now or time.time())
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET metadata_dead_at = %s, "
                "last_error = 'mailbox access revoked' "
                "WHERE mailbox = %s AND sender IS NULL",
                (when, mailbox),
            )
            cursor.execute("DELETE FROM mailbox_state WHERE mailbox = %s", (mailbox,))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._fetchone("SELECT meta_value FROM meta WHERE meta_key = %s", (key,))
        return row["meta_value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "INSERT INTO meta(meta_key, meta_value) VALUES(%s, %s) "
                "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                (key, str(value)),
            )

    def migrate_legacy_json(self, legacy_path: Path | str) -> bool:
        """Import chat id and Gmail cursors once, preserving the old file."""
        if self.get_meta("legacy_json_migrated") == "1":
            return False
        path = Path(legacy_path).expanduser()
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            legacy = {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"legacy state is invalid JSON: {path}: {exc}") from exc

        now = int(time.time())
        with self._tx() as cursor:
            chat_id = legacy.get("chat_id")
            if chat_id:
                for key in ("chat_id", "owner_user_id"):
                    cursor.execute(
                        "INSERT INTO meta(meta_key, meta_value) VALUES(%s, %s) "
                        "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                        (key, str(chat_id)),
                    )
            for mailbox, box in (legacy.get("boxes") or {}).items():
                history_id = (box or {}).get("history_id")
                if history_id:
                    cursor.execute(
                        "INSERT INTO mailbox_state(mailbox, history_id, last_scan_at) "
                        "VALUES(%s, %s, %s) ON DUPLICATE KEY UPDATE "
                        "history_id = VALUES(history_id)",
                        (mailbox, str(history_id), now),
                    )
            cursor.execute(
                "INSERT INTO meta(meta_key, meta_value) VALUES(%s, %s) "
                "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                ("legacy_json_migrated", "1"),
            )
        return True

    def mailbox_cursor(self, mailbox: str) -> str | None:
        row = self._fetchone(
            "SELECT history_id FROM mailbox_state WHERE mailbox = %s", (mailbox,)
        )
        return row["history_id"] if row else None

    def stage_discovery(
        self,
        mailbox: str,
        history_id: str | None,
        gmail_ids: Iterable[str],
        *,
        now: int | None = None,
    ) -> int:
        """Atomically save ids and advance this mailbox's history cursor."""
        when = int(now or time.time())
        ids = list(dict.fromkeys(str(mid) for mid in gmail_ids if mid))
        inserted = 0
        with self._tx() as cursor:
            for gmail_id in ids:
                cursor.execute(
                    "INSERT IGNORE INTO messages(" 
                    "token, mailbox, gmail_id, gmail_labels, discovered_at) "
                    "VALUES(%s, %s, %s, JSON_ARRAY(), %s)",
                    (message_token(mailbox, gmail_id), mailbox, gmail_id, when),
                )
                inserted += cursor.rowcount
            cursor.execute(
                "INSERT INTO mailbox_state(mailbox, history_id, last_scan_at, last_error) "
                "VALUES(%s, %s, %s, NULL) ON DUPLICATE KEY UPDATE "
                "history_id = VALUES(history_id), last_scan_at = VALUES(last_scan_at), "
                "last_error = NULL",
                (mailbox, str(history_id) if history_id else None, when),
            )
        return inserted

    def record_mailbox_error(self, mailbox: str, error: str) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "INSERT INTO mailbox_state(mailbox, last_error) VALUES(%s, %s) "
                "ON DUPLICATE KEY UPDATE last_error = VALUES(last_error)",
                (mailbox, error[:1000]),
            )

    def unhydrated(self, limit: int = 100, *, now: int | None = None) -> list[dict]:
        when = int(now or time.time())
        return self._fetchall(
            "SELECT * FROM messages WHERE sender IS NULL "
            "AND metadata_dead_at IS NULL "
            "AND (metadata_next_attempt_at IS NULL OR metadata_next_attempt_at <= %s) "
            "ORDER BY metadata_attempts, discovered_at, mailbox LIMIT %s",
            (when, limit),
        )

    def set_metadata(
        self,
        token: str,
        *,
        sender: str,
        sender_email: str,
        subject: str,
        thread_id: str,
        received_at: str,
        gmail_labels: Iterable[str],
        mailing_list: bool,
    ) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET thread_id = %s, sender = %s, sender_email = %s, "
                "subject = %s, received_at = %s, gmail_labels = %s, mailing_list = %s, "
                "metadata_attempts = metadata_attempts + 1, "
                "metadata_next_attempt_at = NULL, metadata_dead_at = NULL, "
                "last_error = NULL "
                "WHERE token = %s",
                (
                    thread_id,
                    sender,
                    sender_email,
                    subject,
                    received_at,
                    json.dumps(sorted(set(gmail_labels)), ensure_ascii=False),
                    bool(mailing_list),
                    token,
                ),
            )

    def record_metadata_error(
        self,
        token: str,
        error: str,
        *,
        permanent: bool = False,
        now: int | None = None,
    ) -> None:
        """Back off transient failures and dead-letter permanent Gmail misses."""
        when = int(now or time.time())
        with self._tx() as cursor:
            cursor.execute(
                "SELECT metadata_attempts FROM messages WHERE token = %s FOR UPDATE",
                (token,),
            )
            row = cursor.fetchone()
            attempts = int((row or {}).get("metadata_attempts") or 0) + 1
            # 1m, 2m, 4m ... capped at 6h. New rows (attempts=0) are always
            # selected before retries, so a broken mailbox cannot starve six
            # healthy ones.
            next_attempt = when + min(6 * 3600, 60 * (2 ** min(attempts - 1, 9)))
            cursor.execute(
                "UPDATE messages SET metadata_attempts = metadata_attempts + 1, "
                "metadata_next_attempt_at = %s, metadata_dead_at = %s, "
                "last_error = %s WHERE token = %s",
                (
                    None if permanent else next_attempt,
                    when if permanent else None,
                    error[:1000],
                    token,
                ),
            )

    def unclassified(self, limit: int = 20) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM messages WHERE sender IS NOT NULL AND category IS NULL "
            "ORDER BY discovered_at, mailbox LIMIT %s", (limit,)
        )

    def set_classification(
        self,
        token: str,
        category: str,
        confidence: float,
        why: str,
        source: str,
        *,
        now: int | None = None,
    ) -> None:
        self.finalize_classification(
            token,
            category,
            confidence,
            why,
            source,
            now=now,
        )

    def finalize_classification(
        self,
        token: str,
        category: str,
        confidence: float,
        why: str,
        source: str,
        *,
        suppress: bool = False,
        subscriber: str | None = None,
        topic: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Commit classification, suppression and subscriber enqueue atomically."""
        if bool(subscriber) != bool(topic):
            raise ValueError("subscriber and topic must be provided together")
        when = int(now or time.time())
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET category = %s, confidence = %s, why = %s, "
                "classifier_source = %s, classified_at = %s, last_error = NULL "
                "WHERE token = %s",
                (category, confidence, why[:1000], source, when, token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"message token not found: {token}")
            if suppress:
                cursor.execute(
                    "UPDATE messages SET delivered_at = %s, "
                    "delivery_kind = 'suppressed', telegram_message_id = NULL, "
                    "last_error = NULL WHERE token = %s",
                    (when, token),
                )
            subscriber_enqueued = False
            if subscriber and topic:
                cursor.execute(
                    "INSERT IGNORE INTO subscriber_deliveries("
                    "subscriber, token, topic, queued_at) VALUES(%s, %s, %s, %s)",
                    (subscriber[:64], token, topic[:64], when),
                )
                subscriber_enqueued = bool(cursor.rowcount)
            return subscriber_enqueued

    def pending_hot(self, confidence_floor: float, limit: int = 25) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM messages WHERE category IS NOT NULL "
            "AND delivered_at IS NULL AND ("
            "COALESCE(user_category, category) IN ('urgent', 'important') "
            "OR confidence < %s) ORDER BY discovered_at, mailbox LIMIT %s",
            (confidence_floor, limit),
        )

    def pending_low(self, confidence_floor: float, limit: int = 8) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM messages WHERE category IS NOT NULL "
            "AND delivered_at IS NULL "
            "AND COALESCE(user_category, category) IN ('routine', 'noise') "
            "AND confidence >= %s ORDER BY discovered_at, mailbox LIMIT %s",
            (confidence_floor, limit),
        )

    def mark_suppressed(self, token: str, *, now: int | None = None) -> None:
        """Acknowledge a confident low-priority item without sending Telegram."""
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET delivered_at = %s, delivery_kind = 'suppressed', "
                "telegram_message_id = NULL, last_error = NULL WHERE token = %s",
                (int(now or time.time()), token),
            )

    def pending_promotions(
        self, limit: int = 25, *, token: str | None = None
    ) -> list[dict]:
        sql = (
            "SELECT * FROM messages WHERE promotion_pending = TRUE "
            "AND COALESCE(user_category, category) IN ('urgent', 'important')"
        )
        params: tuple = ()
        if token:
            sql += " AND token = %s"
            params = (token,)
        sql += " ORDER BY feedback_at, discovered_at LIMIT %s"
        return self._fetchall(sql, params + (limit,))

    def mark_promotion_delivered(
        self, token: str, telegram_message_id: int, *, now: int | None = None
    ) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET promotion_pending = FALSE, delivered_at = %s, "
                "delivery_kind = 'hot', telegram_message_id = %s, "
                "delivery_attempts = delivery_attempts + 1, last_error = NULL "
                "WHERE token = %s",
                (int(now or time.time()), telegram_message_id, token),
            )

    def mark_delivered(
        self, token: str, kind: str, telegram_message_id: int, *, now: int | None = None
    ) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET delivered_at = %s, delivery_kind = %s, "
                "telegram_message_id = %s, delivery_attempts = delivery_attempts + 1, "
                "last_error = NULL WHERE token = %s",
                (int(now or time.time()), kind, telegram_message_id, token),
            )

    def record_delivery_error(self, token: str, error: str) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE messages SET delivery_attempts = delivery_attempts + 1, "
                "last_error = %s WHERE token = %s", (error[:1000], token)
            )

    def get_message(self, token: str) -> dict | None:
        return self._fetchone("SELECT * FROM messages WHERE token = %s", (token,))

    def apply_feedback(
        self,
        callback_id: str,
        token: str,
        chosen_category: str,
        *,
        now: int | None = None,
    ) -> tuple[bool, dict | None]:
        """Idempotently save one Telegram correction."""
        when = int(now or time.time())
        with self._tx() as cursor:
            cursor.execute("SELECT * FROM messages WHERE token = %s FOR UPDATE", (token,))
            row = cursor.fetchone()
            if not row:
                return False, None
            previous = row["user_category"] or row["category"]
            cursor.execute(
                "INSERT IGNORE INTO feedback(" 
                "callback_id, token, previous_category, chosen_category, created_at) "
                "VALUES(%s, %s, %s, %s, %s)",
                (callback_id, token, previous, chosen_category, when),
            )
            if not cursor.rowcount:
                return False, row
            promotion_pending = bool(
                chosen_category in ("urgent", "important")
                and row.get("delivery_kind") in ("digest", "suppressed")
            )
            cursor.execute(
                "UPDATE messages SET user_category = %s, feedback_at = %s, "
                "promotion_pending = %s WHERE token = %s",
                (chosen_category, when, promotion_pending, token),
            )
            cursor.execute("SELECT * FROM messages WHERE token = %s", (token,))
            updated = cursor.fetchone()
        return True, updated

    def feedback_examples(self, limit: int = 80) -> list[dict]:
        return self._fetchall(
            "SELECT mailbox, sender, sender_email, subject, category, "
            "user_category, feedback_at FROM messages "
            "WHERE user_category IS NOT NULL ORDER BY feedback_at DESC LIMIT %s",
            (limit,),
        )

    def enqueue_subscriber(
        self,
        token: str,
        subscriber: str,
        topic: str,
        *,
        now: int | None = None,
    ) -> bool:
        with self._tx() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO subscriber_deliveries("
                "subscriber, token, topic, queued_at) VALUES(%s, %s, %s, %s)",
                (subscriber[:64], token, topic[:64], int(now or time.time())),
            )
            return bool(cursor.rowcount)

    def pending_subscriber(self, subscriber: str, limit: int = 25) -> list[dict]:
        return self._fetchall(
            "SELECT d.id AS subscriber_delivery_id, d.subscriber, d.topic, "
            "d.queued_at AS subscriber_queued_at, d.delivery_attempts AS subscriber_attempts, "
            "m.* FROM subscriber_deliveries d JOIN messages m ON m.token = d.token "
            "WHERE d.subscriber = %s AND d.delivered_at IS NULL "
            "ORDER BY d.queued_at, d.id LIMIT %s",
            (subscriber, limit),
        )

    def mark_subscriber_delivered(
        self, delivery_id: int, *, now: int | None = None
    ) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE subscriber_deliveries SET delivered_at = %s, "
                "delivery_attempts = delivery_attempts + 1, last_error = NULL "
                "WHERE id = %s",
                (int(now or time.time()), delivery_id),
            )

    def record_subscriber_error(self, delivery_id: int, error: str) -> None:
        with self._tx() as cursor:
            cursor.execute(
                "UPDATE subscriber_deliveries SET delivery_attempts = delivery_attempts + 1, "
                "last_error = %s WHERE id = %s",
                (error[:1000], delivery_id),
            )

    def recent(self, limit: int = 12) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM messages WHERE category IS NOT NULL "
            "ORDER BY discovered_at DESC, token DESC LIMIT %s", (limit,)
        )

    def stats(self) -> dict:
        row = self._fetchone(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN sender IS NULL THEN 1 ELSE 0 END) AS unhydrated, "
            "SUM(CASE WHEN metadata_dead_at IS NOT NULL THEN 1 ELSE 0 END) AS dead_metadata, "
            "SUM(CASE WHEN sender IS NOT NULL AND category IS NULL THEN 1 ELSE 0 END) AS unclassified, "
            "SUM(CASE WHEN category IS NOT NULL AND delivered_at IS NULL THEN 1 ELSE 0 END) AS undelivered, "
            "SUM(CASE WHEN promotion_pending = TRUE THEN 1 ELSE 0 END) AS promotions, "
            "SUM(CASE WHEN user_category IS NOT NULL THEN 1 ELSE 0 END) AS corrected "
            "FROM messages"
        ) or {}
        out = {key: int(row.get(key) or 0) for key in (
            "total", "unhydrated", "dead_metadata", "unclassified", "undelivered",
            "promotions", "corrected"
        )}
        boxes = self._fetchone(
            "SELECT COUNT(*) AS n FROM mailbox_state WHERE history_id IS NOT NULL"
        ) or {"n": 0}
        out["mailboxes"] = int(boxes["n"])
        pending = self._fetchone(
            "SELECT COUNT(*) AS n FROM subscriber_deliveries WHERE delivered_at IS NULL"
        ) or {"n": 0}
        out["subscriber_pending"] = int(pending["n"])
        return out

    def mailbox_status(self) -> list[dict]:
        return self._fetchall("SELECT * FROM mailbox_state ORDER BY mailbox")
