"""Foreign Play reviews stay out of both automatic notification paths."""

import unittest
from unittest import mock

import mail_watch as mw


def review(app="Wallz Game: Quoridor Online", **overrides):
    row = {
        "token": "0123456789abcdef",
        "gmail_id": "test-review",
        "mailbox": "business@ddinsights.org",
        "sender_email": "noreply-play-developer-console@google.com",
        "subject": f"A user has written a new review for {app} on September 5, 2026",
        "gmail_labels": '["INBOX"]',
        "mailing_list": True,
        "category": "urgent",
        "confidence": 0.99,
        "user_category": None,
        "why": "old model verdict",
        "subscriber_delivery_id": 8,
        "topic": "platform-notice",
    }
    row.update(overrides)
    return row


class ForeignReviewIdentityTests(unittest.TestCase):
    def test_observed_wallz_envelope_and_known_exact_packages(self):
        identities = [
            ("Wallz Game: Quoridor Online", "com.ddinsights.wallz"),
            ("com.ddinsights.wallz", "com.ddinsights.wallz"),
            ("com.ddinsights.wallkade", "com.ddinsights.wallkade"),
            ("com.ddlnsights.sudokuv20", "com.ddlnsights.sudokuv20"),
        ]
        for app, package in identities:
            with self.subTest(app=app):
                self.assertEqual(mw._foreign_play_review_app(review(app)), package)

    def test_case_and_header_whitespace_do_not_change_identity(self):
        row = review("WALLZ\u00a0Game: Quoridor Online",
                     sender_email=" NOREPLY-PLAY-DEVELOPER-CONSOLE@GOOGLE.COM ")
        self.assertEqual(mw._foreign_play_review_app(row), "com.ddinsights.wallz")

    def test_our_and_unmatched_apps_stay_visible(self):
        apps = ["Arrow Solitaire: Crosswise", "Mahjong (Dragon's Garden)",
                "Petart", "New Puzzle Game", "org.ddinsights.arrowclash"]
        for app in apps:
            with self.subTest(app=app):
                row = review(app)
                self.assertIsNone(mw._foreign_play_review_app(row))
                self.assertEqual(mw._claude_subscription_topic(row), "platform-notice")

    def test_requested_substrings_match_anywhere_in_app_name(self):
        names = [
            ("Sudoku", "sudoku"), ("Daily SuDoKu Challenge", "sudoku"),
            ("SuperSudokuPro", "sudoku"), ("Wallkade", "wallkade"),
            ("Online WALLKADE Game", "wallkade"), ("Wallz", "wallz"),
            ("Wallz Game: Quoridor Online 2", "wallz"),
            ("New Game featuring Wallz Game: Quoridor Online", "wallz"),
            ("com.ddinsights.wallz2", "wallz"),
            ("Arrow Solitaire: Crosswise (com.ddinsights.wallz)", "wallz"),
        ]
        for app, part in names:
            with self.subTest(app=app):
                row = review(app)
                self.assertEqual(mw._foreign_play_review_app(row), f"title:{part}")
                self.assertIsNone(mw._claude_subscription_topic(row))

    def test_same_sender_does_not_hide_other_notices_or_changed_templates(self):
        subjects = [
            "Action required: Wallz Game: Quoridor Online policy violation",
            "Review rejected for Wallz Game: Quoridor Online",
            "Your production release for Wallz Game: Quoridor Online is live",
            "New rating for Wallz Game: Quoridor Online",
            "A user has written a new review for Wallz Game: Quoridor Online",
            "Re: " + review()["subject"],
            review()["subject"] + "; account suspended",
            "Google Play review: " + review()["subject"],
            review()["subject"].replace("September 5, 2026", "05.09.2026"),
        ]
        for subject in subjects:
            with self.subTest(subject=subject):
                row = review(subject=subject)
                self.assertIsNone(mw._foreign_play_review_app(row))
                self.assertEqual(mw._claude_subscription_topic(row), "platform-notice")

    def test_sender_identity_is_exact(self):
        for sender in ["person@example.com", "googleplay-noreply@google.com",
                       "noreply-play-developer-console@google.com.example",
                       "noreply-play-developer-console@notgoogle.com", ""]:
            with self.subTest(sender=sender):
                self.assertIsNone(mw._foreign_play_review_app(review(sender_email=sender)))

    def test_body_or_incidental_mentions_cannot_trigger_exclusion(self):
        row = review("Arrow Solitaire: Crosswise")
        row["body"] = review()["subject"]
        row["snippet"] = "Wallkade Sudoku Wallz Game: Quoridor Online com.ddinsights.wallz"
        self.assertIsNone(mw._foreign_play_review_app(row))

    def test_foreign_review_excluded_before_google_platform_route(self):
        self.assertIsNone(mw._claude_subscription_topic(review()))

    def test_safety_notice_stays_important(self):
        row = review(subject="Action required: review rejected for Wallz Game: Quoridor Online")
        verdict = mw._apply_owner_and_safety_policy(row, {
            **row, "category": "noise", "confidence": 1.0, "source": "opus",
        })
        self.assertEqual(verdict["category"], "important")
        self.assertEqual(verdict["source"], "safety-floor")


class ForeignReviewDeliveryTests(unittest.TestCase):
    @mock.patch("mail_watch._peer_tell")
    @mock.patch("mail_watch._tg")
    @mock.patch("mail_watch._classify_batch")
    def test_substring_rule_bypasses_model_and_both_stale_queues(self, classify, tg, tell):
        for app in ["Online WALLKADE Game", "Daily SuDoKu Challenge", "Play Wallz Now"]:
            with self.subTest(app=app):
                store = mock.Mock()
                store.feedback_examples.return_value = []
                store.unclassified.return_value = [review(app, category=None)]
                store.pending_hot.return_value = [review(app)]
                store.pending_subscriber.return_value = [review(app)]
                self.assertEqual(mw._classify_pending(store, {}), 1)
                self.assertTrue(store.finalize_classification.call_args.kwargs["suppress"])
                self.assertEqual(mw._deliver_hot(store), 0)
                self.assertEqual(mw._deliver_subscriber(store, "claude"), 0)
                store.mark_suppressed.assert_called_once()
                store.mark_subscriber_delivered.assert_called_once()
        classify.assert_not_called()
        tg.assert_not_called()
        tell.assert_not_called()

    @mock.patch("mail_watch._classify_batch")
    def test_explicit_foreign_exclusion_never_calls_model(self, classify):
        store = mock.Mock()
        store.feedback_examples.return_value = []
        store.unclassified.return_value = [review(category=None)]
        self.assertEqual(mw._classify_pending(store, {}), 1)
        classify.assert_not_called()
        call = store.finalize_classification.call_args
        self.assertEqual(call.args[1:3], ("noise", 1.0))
        self.assertEqual(call.args[4], "owner-rule")
        self.assertEqual(call.kwargs, {"suppress": True, "subscriber": None, "topic": None})

    @mock.patch("mail_watch._classify_batch")
    def test_mixed_batch_keeps_our_review_in_classifier(self, classify):
        ours = review("Arrow Solitaire: Crosswise", token="ours", category=None)
        store = mock.Mock()
        store.feedback_examples.return_value = []
        store.unclassified.return_value = [review(category=None), ours]
        classify.return_value = [{**ours, "category": "important", "confidence": .9,
                                  "source": "opus", "why": "our review"}]
        self.assertEqual(mw._classify_pending(store, {}), 2)
        self.assertEqual(classify.call_args.args[0], [ours])
        self.assertEqual(store.finalize_classification.call_args.kwargs["subscriber"], "claude")
        self.assertFalse(store.finalize_classification.call_args.kwargs["suppress"])

    @mock.patch("mail_watch._tg")
    def test_old_urgent_owner_queue_is_drained_without_sending(self, tg):
        store = mock.Mock()
        store.pending_hot.return_value = [review()]
        self.assertEqual(mw._deliver_hot(store), 0)
        store.mark_suppressed.assert_called_once_with(review()["token"])
        tg.assert_not_called()
        store.record_delivery_error.assert_not_called()

    @mock.patch("mail_watch._peer_tell")
    def test_old_subscriber_queue_is_drained_without_sending(self, tell):
        store = mock.Mock()
        store.pending_subscriber.return_value = [review()]
        self.assertEqual(mw._deliver_subscriber(store, "claude"), 0)
        store.mark_subscriber_delivered.assert_called_once_with(8)
        tell.assert_not_called()
        store.record_subscriber_error.assert_not_called()

    @mock.patch("mail_watch._tg", return_value={"message_id": 123})
    def test_our_review_still_notifies_owner(self, tg):
        store = mock.Mock()
        store.get_meta.return_value = "42"
        store.pending_hot.return_value = [review("Arrow Solitaire: Crosswise")]
        self.assertEqual(mw._deliver_hot(store), 1)
        tg.assert_called_once()
        store.mark_suppressed.assert_not_called()

    @mock.patch("mail_watch._tg", return_value={"message_id": 123})
    def test_explicit_per_message_owner_override_wins(self, tg):
        store = mock.Mock()
        store.get_meta.return_value = "42"
        store.pending_hot.return_value = [review(user_category="important")]
        self.assertEqual(mw._deliver_hot(store), 1)
        tg.assert_called_once()
        store.mark_suppressed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
