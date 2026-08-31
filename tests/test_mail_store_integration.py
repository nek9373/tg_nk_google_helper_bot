import os
import time
import unittest

from mail_store import MailStore


@unittest.skipUnless(os.environ.get("MAIL_WATCH_INTEGRATION") == "1", "live MySQL test")
class MailStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = MailStore()
        self.mailbox = f"__test_{os.getpid()}@example.invalid"

    def tearDown(self):
        with self.store._tx() as cursor:
            cursor.execute(
                "DELETE f FROM feedback f JOIN messages m ON f.token=m.token "
                "WHERE m.mailbox=%s", (self.mailbox,)
            )
            cursor.execute("DELETE FROM messages WHERE mailbox=%s", (self.mailbox,))
            cursor.execute("DELETE FROM mailbox_state WHERE mailbox=%s", (self.mailbox,))
        self.store.close()

    def test_cursor_outbox_dedup_delivery_and_feedback(self):
        ids = [f"m{index}" for index in range(60)]
        inserted = self.store.stage_discovery(self.mailbox, "h100", ids, now=100)
        self.assertEqual(inserted, 60)
        self.assertEqual(self.store.mailbox_cursor(self.mailbox), "h100")
        self.assertEqual(
            self.store.stage_discovery(self.mailbox, "h101", ids, now=101), 0
        )
        self.assertEqual(self.store.mailbox_cursor(self.mailbox), "h101")
        rows = [row for row in self.store.unhydrated(1000) if row["mailbox"] == self.mailbox]
        self.assertEqual(len(rows), 60)

        target = rows[0]
        self.store.set_metadata(
            target["token"],
            sender="Tester <test@example.invalid>",
            sender_email="test@example.invalid",
            subject="Action required",
            thread_id="thread",
            received_at="now",
            gmail_labels=["INBOX", "IMPORTANT"],
            mailing_list=False,
        )
        self.store.set_classification(
            target["token"], "important", 0.8, "test", "unit", now=102
        )
        hot = [row for row in self.store.pending_hot(0.72, 100) if row["mailbox"] == self.mailbox]
        self.assertEqual([row["token"] for row in hot], [target["token"]])

        self.store.mark_suppressed(target["token"], now=103)
        changed, _ = self.store.apply_feedback("callback-1", target["token"], "important", now=104)
        repeated, _ = self.store.apply_feedback("callback-1", target["token"], "important", now=105)
        self.assertTrue(changed)
        self.assertFalse(repeated)
        self.assertEqual(self.store.get_message(target["token"])["user_category"], "important")
        self.assertEqual(
            [row["token"] for row in self.store.pending_promotions(10)],
            [target["token"]],
        )
        self.store.mark_promotion_delivered(target["token"], 88, now=106)
        self.assertEqual(self.store.pending_promotions(10), [])

    def test_dead_letter_backoff_rename_and_global_lease(self):
        self.store.stage_discovery(self.mailbox, "h200", ["dead", "retry"], now=200)
        rows = {
            row["gmail_id"]: row
            for row in self.store.unhydrated(1000, now=200)
            if row["mailbox"] == self.mailbox
        }
        self.store.record_metadata_error(rows["dead"]["token"], "404", permanent=True, now=201)
        self.store.record_metadata_error(rows["retry"]["token"], "timeout", now=201)
        self.assertEqual(
            [row["gmail_id"] for row in self.store.unhydrated(1000, now=201)
             if row["mailbox"] == self.mailbox],
            [],
        )
        self.assertEqual(
            [row["gmail_id"] for row in self.store.unhydrated(1000, now=262)
             if row["mailbox"] == self.mailbox],
            ["retry"],
        )

        renamed = self.mailbox.replace("__test_", "__renamed_")
        self.store.rename_mailbox(self.mailbox, renamed)
        self.assertEqual(self.store.mailbox_cursor(renamed), "h200")
        self.store.rename_mailbox(renamed, self.mailbox)

        second = MailStore()
        try:
            self.store.acquire_worker_lease()
            with self.assertRaises(RuntimeError):
                second.acquire_worker_lease()
            self.store.release_worker_lease()
            second.acquire_worker_lease()
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
