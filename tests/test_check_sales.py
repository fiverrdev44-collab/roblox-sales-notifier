import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from scripts import check_sales as notifier


GROUP_ID = "852118640"
CONFIG = {"id": int(GROUP_ID), "webhook": "https://discord.invalid/test"}


def transaction(token="purchase-1", **updates):
    value = {
        "purchaseToken": token,
        "created": "2026-11-11T20:50:00Z",
        "agent": {"id": 1513288327, "name": "ExampleBuyer"},
        "details": {"id": 1234, "name": "TF2 Dustbowl Background", "type": "Asset"},
        "currency": {"amount": 60},
        "isPending": True,
    }
    value.update(updates)
    return value


def ledger(tokens=()):
    return {GROUP_ID: {"tokens": list(tokens), "seeded": True}}


def response(status=200, **body):
    return Mock(status_code=status, json=Mock(return_value=body))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "last_seen.json"
        self.addCleanup(patch.stopall)
        patch.object(notifier, "STATE_FILE", str(self.path)).start()
        patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "PERSIST_STATE_TO_GIT": "0"}).start()
        patch.object(notifier.time, "sleep").start()
        self.post = patch.object(notifier.requests, "post", return_value=response()).start()

    def process(self, state, transactions):
        return notifier.process_transactions(state, "/Background Check", CONFIG, transactions)

    def test_repeated_rows_and_next_run_post_once(self):
        state = ledger()
        self.assertEqual(self.process(state, [transaction(), transaction()]), 1)
        self.assertEqual(self.process(notifier.load_state(), [transaction()]), 0)
        self.post.assert_called_once()

    def test_settlement_change_does_not_repeat_sale(self):
        state = ledger()
        self.process(state, [transaction()])
        self.process(notifier.load_state(), [transaction(isPending=False)])
        self.post.assert_called_once()

    def test_distinct_purchases_same_buyer_item_and_amount_both_post(self):
        self.assertEqual(self.process(ledger(), [transaction("one"), transaction("two")]), 2)
        self.assertEqual(self.post.call_count, 2)

    def test_group_ledgers_are_independent(self):
        state = ledger()
        state["42"] = {"tokens": [], "seeded": True}
        self.process(state, [transaction()])
        notifier.process_transactions(state, "Other", dict(CONFIG, id=42), [transaction()])
        self.assertEqual(self.post.call_count, 2)

    def test_legacy_ledger_is_preserved(self):
        state = ledger(["already-sent"])
        notifier.save_state(state)
        self.assertEqual(notifier.load_state(), state)
        self.process(notifier.load_state(), [transaction("already-sent")])
        self.post.assert_not_called()

    def test_first_run_seeds_without_sending(self):
        state = {}
        self.process(state, [transaction(), transaction()])
        self.assertEqual(notifier.load_state()[GROUP_ID]["tokens"], ["purchase-1"])
        self.post.assert_not_called()

    def test_pre_send_checkpoint_exists_at_http_call(self):
        def send(*args, **kwargs):
            saved = notifier.load_state()[GROUP_ID]
            self.assertIn("purchase-1", saved["tokens"])
            self.assertEqual(saved["delivery_issues"]["purchase-1"]["status"], "unconfirmed")
            self.assertEqual(kwargs["params"], {"wait": "true"})
            self.assertFalse(kwargs["allow_redirects"])
            return response()
        self.post.side_effect = send
        self.process(ledger(), [transaction()])
        self.assertEqual(notifier.load_state()[GROUP_ID]["delivery_issues"], {})

    def test_timeout_is_not_retried_after_restart(self):
        self.post.side_effect = requests.ReadTimeout("private webhook URL must not leak")
        with self.assertRaises(notifier.DiscordUncertain) as raised:
            self.process(ledger(), [transaction()])
        self.assertNotIn("private", str(raised.exception))
        self.process(notifier.load_state(), [transaction()])
        self.post.assert_called_once()

    def test_server_error_is_not_retried(self):
        self.post.return_value = response(500)
        with self.assertRaises(notifier.DiscordUncertain):
            self.process(ledger(), [transaction()])
        self.process(notifier.load_state(), [transaction()])
        self.post.assert_called_once()

    def test_runner_death_after_reservation_does_not_resend(self):
        self.post.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.process(ledger(), [transaction()])
        self.process(notifier.load_state(), [transaction()])
        self.post.assert_called_once()

    def test_failure_saving_before_send_blocks_http(self):
        with patch.object(notifier.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.process(ledger(), [transaction()])
        self.post.assert_not_called()

    def test_failure_saving_after_send_keeps_durable_reservation(self):
        real_persist = notifier.persist_state
        count = 0
        def persist(state):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("disk full")
            real_persist(state)
        with patch.object(notifier, "persist_state", side_effect=persist):
            with self.assertRaises(OSError):
                self.process(ledger(), [transaction()])
        self.process(notifier.load_state(), [transaction()])
        self.post.assert_called_once()

    def test_429_retries_only_definitive_rejections(self):
        self.post.side_effect = [response(429, retry_after=0), response()]
        self.assertEqual(self.process(ledger(), [transaction()]), 1)
        self.assertEqual(self.post.call_count, 2)

    def test_exhausted_429_is_not_marked_sent_and_unattempted_sales_remain_new(self):
        self.post.return_value = response(429, retry_after=0)
        with self.assertRaises(notifier.DiscordRejected):
            self.process(ledger(), [transaction("newer"), transaction("older")])
        self.assertEqual(notifier.load_state()[GROUP_ID]["tokens"], [])
        self.assertEqual(self.post.call_count, 3)
        self.post.return_value = response()
        self.assertEqual(self.process(notifier.load_state(), [transaction("newer"), transaction("older")]), 2)

    def test_definitive_rejection_can_retry_next_run(self):
        self.post.return_value = response(403)
        with self.assertRaises(notifier.DiscordRejected):
            self.process(ledger(), [transaction()])
        self.assertEqual(notifier.load_state()[GROUP_ID]["tokens"], [])
        self.post.return_value = response()
        self.assertEqual(self.process(notifier.load_state(), [transaction()]), 1)

    def test_old_tokens_are_not_evicted(self):
        state = ledger([f"old-{i}" for i in range(600)])
        self.process(state, [transaction()])
        self.process(notifier.load_state(), [transaction("old-599")])
        self.post.assert_called_once()

    def test_corrupt_missing_and_invalid_state_fail_closed(self):
        with self.assertRaises(FileNotFoundError):
            notifier.load_state()
        for text in ("broken", "[]", '{"852118640": {"tokens": "invalid", "seeded": true}}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(ValueError):
                    notifier.load_state()
        self.post.assert_not_called()


class GitCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.first))
        self.configure(self.first)
        (self.first / "scripts").mkdir()
        (self.first / "scripts/last_seen.json").write_text(json.dumps(ledger()))
        self.git(self.first, "add", ".")
        self.git(self.first, "commit", "-m", "Initial ledger")
        self.git(self.first, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.second))
        self.configure(self.second)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PERSIST_STATE_TO_GIT": "1"}).start()
        patch.object(notifier.time, "sleep").start()
        self.post = patch.object(notifier.requests, "post", return_value=response()).start()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    def configure(self, repo):
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")

    def process(self, repo, state, txns):
        with patch.object(notifier, "STATE_FILE", str(repo / "scripts/last_seen.json")):
            return notifier.process_transactions(state, "Test", CONFIG, txns)

    def test_remote_reservation_is_visible_before_discord(self):
        def send(*args, **kwargs):
            remote_state = json.loads(self.git(self.remote, "show", "main:scripts/last_seen.json"))
            self.assertIn("purchase-1", remote_state[GROUP_ID]["tokens"])
            self.assertIn("purchase-1", remote_state[GROUP_ID]["delivery_issues"])
            return response()
        self.post.side_effect = send
        self.process(self.first, ledger(), [transaction()])
        self.post.assert_called_once()

    def test_stale_runner_cannot_send_already_claimed_sale(self):
        # Reproduces two queued runs starting from the same pre-sale snapshot.
        self.process(self.first, ledger(), [transaction()])
        with self.assertRaises(subprocess.CalledProcessError):
            self.process(self.second, ledger(), [transaction()])
        self.post.assert_called_once()

    def test_competing_claim_while_first_delivery_is_in_flight_is_rejected(self):
        def send(*args, **kwargs):
            with self.assertRaises(subprocess.CalledProcessError):
                self.process(self.second, ledger(), [transaction()])
            return response()
        self.post.side_effect = send
        self.process(self.first, ledger(), [transaction()])
        self.post.assert_called_once()

    def test_remote_push_failure_blocks_discord(self):
        self.git(self.first, "remote", "set-url", "origin", str(self.root / "missing.git"))
        with self.assertRaises(subprocess.CalledProcessError):
            self.process(self.first, ledger(), [transaction()])
        self.post.assert_not_called()

    def test_restart_from_remote_skips_ambiguous_delivery(self):
        self.post.side_effect = requests.ReadTimeout()
        with self.assertRaises(notifier.DiscordUncertain):
            self.process(self.first, ledger(), [transaction()])
        self.git(self.second, "pull", "--ff-only", "origin", "main")
        state = json.loads((self.second / "scripts/last_seen.json").read_text())
        self.process(self.second, state, [transaction()])
        self.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
