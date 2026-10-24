"""A-J qualification with real CLI, provider boundary mocks and real local Git."""

import json
import os
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import Parser
from email.utils import parsedate_to_datetime
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import test_persist_state as git_fixtures


SOURCE = Path(__file__).resolve().parents[1]
BOUNDARY = Path(__file__).with_name("offline_cli.py")


def post(number, text=None, **extra):
    return {"id": str(number), "text": text or f"English {number}", "created_at": "2026-10-24T01:00:00Z", **extra}


def page(posts=(), includes=None, token=None):
    result = {"data": list(posts), "meta": {"result_count": len(posts)}, "includes": includes or {}}
    if token is not None: result["meta"]["next_token"] = str(token)
    return result


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = git_fixtures.PersistenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.git = self.fixture.git
        self.seed, self.work, self.remote = self.fixture.seed, self.fixture.work, self.fixture.remote
        for name in ("monitor.py", "persist_state.py"):
            shutil.copyfile(SOURCE / name, self.seed / name)
        self.initial = {
            "initialized": True, "last_seen_id": "100",
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        }
        self.fixture.seed_commit(state=self.initial)
        self.git(self.work, "pull", "--ff-only", "origin", "main")
        self.prepare(self.work)

    def env(self, root, scenario):
        # Inherit only OS/runtime necessities, never the host's API/mail credentials.
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE") if key in os.environ}
        env.update({
            "PYTHONIOENCODING": "utf-8", "X_BEARER_TOKEN": "fixture-x",
            "SMTP_USERNAME": "sender@example.invalid", "SMTP_APP_PASSWORD": "fixture-smtp",
            "SMTP_HOST": "smtp.example.invalid", "SMTP_PORT": "465",
            "ALERT_EMAIL": "owner@example.invalid", "ADDITIONAL_ALERT_EMAIL": "second@example.invalid",
            "THIRD_ALERT_EMAIL": "third@example.invalid", "DEEPSEEK_API_KEY": "fixture-deepseek",
            "GITHUB_STEP_SUMMARY": str(root / ".git/step-summary.md"),
            "MONITOR_REPORT_PATH": str(root / ".git/report.json"),
        })
        if scenario.get("unconfigured"): env.pop("DEEPSEEK_API_KEY")
        if scenario.get("summary_failure"):
            directory = root / ".git/unwritable-summary"
            directory.mkdir(exist_ok=True)
            env["GITHUB_STEP_SUMMARY"] = str(directory)
        return env

    def prepare(self, root):
        result = subprocess.run([sys.executable, "-B", "persist_state.py", "prepare"], cwd=root,
                                env=self.env(root, {}), capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_cli(self, scenario, *flags, root=None, phase="monitor", expected=0):
        root = root or self.work
        (root / ".git/scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(BOUNDARY), phase, *flags], cwd=root,
                                env=self.env(root, scenario), capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("fixture-private", result.stdout + result.stderr)
        self.assertNotIn("fixture-deepseek", result.stdout + result.stderr)
        self.assertNotIn("fixture-smtp", result.stdout + result.stderr)
        report_path = root / ".git/report.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        outbox_path = root / ".git/outbox.json"
        messages = [Parser(policy=policy.default).parsestr(raw) for raw in json.loads(outbox_path.read_text())] if outbox_path.exists() else []
        return result, report, messages

    def fresh_runner(self, name):
        root = self.fixture.directory / name
        self.git(self.fixture.directory, "clone", str(self.remote), str(root))
        self.prepare(root)
        return root

    def state(self, root=None):
        return json.loads(((root or self.work) / "state.json").read_text())

    def text(self, message, kind="plain"):
        return message.get_body(preferencelist=(kind,)).get_content()

    def test_A_no_posts_no_mail_no_heartbeat_commit(self):
        before = self.git(self.remote, "rev-parse", "main")
        _, report, mail = self.run_cli({"pages": [page()]})
        self.assertEqual((report["status"], report["state_written"], mail), ("NO_NEW_POSTS", False, []))
        self.assertEqual(self.state(), self.initial)
        result, _, _ = self.run_cli({}, "persist", phase="persist")
        self.assertIn("UNCHANGED", result.stdout)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)

    def representative(self):
        original = "Original <script>alert('x')</script> & special\n```py\nprint('x')\n```\nSee https://example.com @person #tag"
        rows = [
            post(103, "Quote mixed 中文 & English", referenced_tweets=[{"type": "quoted", "id": "9"}]),
            post(102, "Reply text", in_reply_to_user_id="8", referenced_tweets=[{"type": "replied_to", "id": "7"}]),
            post(101, "truncated", note_tweet={"text": original + "\n" + "long " * 200}),
        ]
        includes = {"tweets": [post(9, "Quoted parent"), post(7, "Reply parent")], "users": [{"id": "8", "username": "someone"}]}
        return rows, includes, original

    def test_B_original_reply_quote_mail_and_persistence(self):
        rows, includes, original = self.representative()
        _, report, mail = self.run_cli({"pages": [page(rows, includes)]})
        self.assertEqual((report["unique_posts"], report["translation_success"], report["translation_failed"]), (3, 5, 0))
        self.assertEqual(len(mail), 1)
        text, html = self.text(mail[0]), self.text(mail[0], "html")
        self.assertLess(text.index("--- 第 1 条 ---"), text.index("--- 第 3 条 ---"))
        self.assertLess(text.index(original), text.index("Reply text"))
        for value in [original, "类型：原创", "类型：回复", "类型：引用", "回复对象：@someone", "被回复原帖：Reply parent", "被引用原帖：Quoted parent", "2026-10-24 09:00:00", "中文译文"]:
            self.assertIn(value, text)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;", html)
        self.assertIn("white-space:pre-wrap", html)
        self.assertIsNotNone(parsedate_to_datetime(mail[0]["Date"]).tzinfo)
        self.assertEqual(self.state()["last_seen_id"], "103")
        self.run_cli({}, "persist", phase="persist")
        self.assertEqual(self.fixture.remote_state()["last_seen_id"], "103")

    def test_C_total_translation_failure_still_notifies(self):
        for mode in ["timeout", "http", "invalid", "empty", "truncated"]:
            with self.subTest(mode=mode):
                root = self.fresh_runner("failure-" + mode)
                _, report, mail = self.run_cli({"pages": [page([post(101)])], "translation": mode}, root=root)
                self.assertIn("English 101", self.text(mail[0]))
                self.assertIn("中文翻译失败，已保留英文原文", self.text(mail[0]))
                self.assertEqual((report["smtp"], report["translation_failed"]), ("ALL_ACCEPTED", 1))
                self.assertEqual(self.state(root)["last_seen_id"], "101")

    def test_D_partial_main_and_context_are_independent(self):
        for missing in ["English 101", "Parent English"]:
            with self.subTest(missing=missing):
                root = self.fresh_runner("partial-" + missing.replace(" ", "-"))
                row = post(101, referenced_tweets=[{"type": "quoted", "id": "9"}])
                _, report, mail = self.run_cli({"pages": [page([row], {"tweets": [post(9, "Parent English")]})], "untranslated": [missing]}, root=root)
                self.assertEqual((report["translation_success"], report["translation_failed"]), (1, 1))
                self.assertEqual(self.text(mail[0]).count("中文翻译失败，已保留英文原文"), 1)
                self.assertIn("中文译文", self.text(mail[0]))
                self.assertEqual(self.state(root)["last_seen_id"], "101")

    def test_E_over_100_posts_boundary_duplicate_and_context(self):
        rows = [post(i) for i in range(202, 100, -1)]
        rows[-1]["referenced_tweets"] = [{"type": "quoted", "id": "9"}]
        rows[-2]["referenced_tweets"] = [{"type": "replied_to", "id": "8"}]
        first, second = rows[:100], rows[99:]
        _, report, mail = self.run_cli({"pages": [page(first, token=1), page(second, {"tweets": [post(9), post(8)]})]})
        self.assertEqual((report["pages"], report["fetched"], report["unique_posts"]), (2, 103, 102))
        text = self.text(mail[0])
        self.assertEqual(text.count("--- 第 "), 102)
        self.assertLess(text.index("status/101"), text.index("status/202"))
        self.assertIn("类型：回复", text); self.assertIn("类型：引用", text)
        self.assertEqual(self.state()["last_seen_id"], "202")

    def test_F_page_two_failure_cannot_confirm_partial_batch(self):
        _, report, mail = self.run_cli({"pages": [page([post(102)], token=1)], "x_fail_page": 1}, expected=1)
        self.assertEqual((report["failure_type"], report["smtp"], mail), ("HTTP_503", "NOT_ATTEMPTED", []))
        self.assertEqual(self.state(), self.initial)

    def test_G_partial_refusal_retains_retry_opportunity(self):
        _, report, mail = self.run_cli({"pages": [page([post(101)])], "partial_refusal": True}, expected=1)
        self.assertEqual((report["accepted"], report["refused"], report["smtp"]), (2, 1, "PARTIAL_REFUSAL"))
        self.assertEqual(len(mail), 1)
        self.assertEqual(self.state(), self.initial)

    def test_H_acceptance_then_save_push_or_process_failure_can_repeat(self):
        for failure in ["save_failure", "push_failure", "interrupt_after_acceptance"]:
            with self.subTest(failure=failure):
                root = self.fresh_runner("accepted-" + failure)
                scenario = {"pages": [page([post(101)])], failure: True}
                _, report, mail = self.run_cli(scenario, root=root, expected=75 if failure == "interrupt_after_acceptance" else 1 if failure == "save_failure" else 0)
                self.assertEqual(len(mail), 1)
                if failure == "push_failure":
                    result, _, _ = self.run_cli(scenario, "persist", phase="persist", root=root, expected=1)
                    self.assertIn("FAILED / GIT_PUSH_FAILED", result.stdout)
                elif failure == "save_failure":
                    self.assertEqual((report["status"], report["smtp"], report["state_written"]), ("FAILED", "ALL_ACCEPTED", False))
                self.assertEqual(self.fixture.remote_state(), self.initial)
                next_root = self.fresh_runner("retry-" + failure)
                _, _, retry = self.run_cli({"pages": [page([post(101)])]}, root=next_root)
                self.assertEqual(mail[0]["Message-ID"], retry[0]["Message-ID"])

    def test_I_invalid_states_fail_before_network(self):
        for raw in [None, "{bad", "[]", '{"initialized":true,"last_seen_id":""}', '{"initialized":"true","last_seen_id":"100"}', '{"initialized":true,"last_seen_id":"-1"}']:
            with self.subTest(raw=raw):
                root = self.fresh_runner("bad-" + str(len(str(raw)))) if raw is None else self.work
                path = root / "state.json"
                if raw is None: path.unlink()
                else: path.write_text(raw, encoding="utf-8")
                _, report, mail = self.run_cli({"pages": []}, root=root, expected=1)
                self.assertEqual((report["smtp"], mail), ("NOT_ATTEMPTED", []))
                self.assertEqual(json.loads((root / ".git/http-calls.json").read_text()), [])
        self.assertEqual(self.fixture.remote_state(), self.initial)

    def test_J_long_gap_warns_without_claiming_historical_recovery(self):
        state = dict(self.initial, last_checked_at=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
        (self.work / "state.json").write_text(json.dumps(state))
        result, report, mail = self.run_cli({"pages": [page()]})
        self.assertTrue(report["coverage_warning"])
        self.assertIn("cannot prove complete historical coverage", result.stdout)
        self.assertEqual((mail, self.state()["last_seen_id"]), ([], "100"))

    def test_manual_modes_never_change_or_persist_formal_state(self):
        for mode in [["--sample"], ["--resend-since-id", "100", "--resend-through-id", "101"], ["--test-email"]]:
            with self.subTest(mode=mode):
                root = self.fresh_runner("manual-" + mode[0][2:])
                _, report, mail = self.run_cli({"pages": [page([post(101)])], "unconfigured": True}, *mode, root=root)
                self.assertEqual(len(mail), 1)
                self.assertFalse(report["state_written"])
                self.assertEqual(self.state(root), self.initial)
                result, _, _ = self.run_cli({}, "persist", phase="persist", root=root)
                self.assertIn("UNCHANGED", result.stdout)
                if mode[0] != "--test-email":
                    self.assertIn("翻译服务未配置", self.text(mail[0]))

    def test_summary_failure_must_not_block_state_persistence(self):
        _, report, mail = self.run_cli({"pages": [page([post(101)])], "summary_failure": True})
        self.assertEqual((len(mail), report["state_written"]), (1, True))
        result, _, _ = self.run_cli({"summary_failure": True}, "persist", phase="persist")
        self.assertIn("PERSISTED", result.stdout)
        self.assertEqual(self.fixture.remote_state()["last_seen_id"], "101")
        root = self.fresh_runner("failed-with-broken-summary")
        _, report, _ = self.run_cli({
            "pages": [page([post(102)])], "summary_failure": True, "partial_refusal": True,
        }, root=root, expected=1)
        self.assertEqual(report["failure_type"], "SMTP_PARTIAL_REFUSAL")
        self.assertEqual(self.state(root)["last_seen_id"], "101")

    def test_missing_context_is_visible_in_both_mail_alternatives(self):
        row = post(101, referenced_tweets=[{"type": "quoted", "id": "9"}])
        response = page([row])
        response["errors"] = [{"type": "resource-not-found", "resource_type": "tweet", "resource_id": "9"}]
        _, report, mail = self.run_cli({"pages": [response]})
        self.assertEqual(report["context_missing"], 1)
        for kind in ("plain", "html"):
            self.assertIn("上下文不可用", self.text(mail[0], kind))
            self.assertNotIn("被引用原帖中文翻译", self.text(mail[0], kind))

    def test_empty_baseline_cursor_is_not_silently_reinitialized(self):
        state = dict(self.initial, last_seen_id=None)
        (self.work / "state.json").write_text(json.dumps(state))
        _, report, mail = self.run_cli({"pages": [page()]})
        self.assertEqual((report["status"], mail), ("NO_NEW_POSTS", []))
        self.assertIsNone(self.state()["last_seen_id"])

    def test_cutover_merge_preserves_live_cursor_and_second_run_does_not_repeat(self):
        # Build a legacy main, then add the upgrade on a feature branch without
        # changing state. Production advances state while that branch is reviewed.
        self.git(self.seed, "rm", "monitor.py", "persist_state.py")
        legacy = {"initialized": True, "last_seen_id": "105"}
        (self.seed / "state.json").write_text(json.dumps(legacy))
        self.git(self.seed, "add", "state.json")
        self.git(self.seed, "commit", "-m", "legacy schema fixture")
        self.git(self.seed, "checkout", "-b", "upgrade-fixture")
        base = self.git(self.seed, "rev-parse", "HEAD")
        for name in ("monitor.py", "persist_state.py"):
            shutil.copyfile(SOURCE / name, self.seed / name)
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "new code only")
        self.assertEqual(self.git(self.seed, "diff", base, "HEAD", "--", "state.json"), "")
        self.git(self.seed, "checkout", "main")
        self.fixture.seed_commit(state=dict(legacy, last_seen_id="110"), code="unrelated production code\n")
        self.git(self.seed, "merge", "--no-ff", "upgrade-fixture", "-m", "qualification merge")
        self.git(self.seed, "push", "origin", "main")
        # Our existing runner still has stale code/state from its earlier event.
        self.prepare(self.work)
        self.assertEqual(self.state(), {"initialized": True, "last_seen_id": "110"})
        _, report, mail = self.run_cli({"pages": [page([post(111)])]})
        self.assertEqual(len(mail), 1)
        self.assertEqual(report["batch_id"], "tibo-digest-111-111")
        self.assertIn("last_checked_at", self.state())
        calls = json.loads((self.work / ".git/http-calls.json").read_text())
        self.assertEqual(calls[0]["query"]["since_id"], ["110"])
        self.run_cli({}, "persist", phase="persist")
        self.assertEqual(self.fixture.remote_state()["last_seen_id"], "111")
        self.assertEqual(self.git(self.remote, "show", "main:code.txt"), "unrelated production code")
        before = self.git(self.remote, "rev-parse", "main")
        next_root = self.fresh_runner("after-cutover")
        _, report, mail = self.run_cli({"pages": [page()]}, root=next_root)
        self.assertEqual((report["status"], mail), ("NO_NEW_POSTS", []))
        calls = json.loads((next_root / ".git/http-calls.json").read_text())
        self.assertEqual(calls[0]["query"]["since_id"], ["111"])
        self.run_cli({}, "persist", phase="persist", root=next_root)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)


if __name__ == "__main__":
    unittest.main()
