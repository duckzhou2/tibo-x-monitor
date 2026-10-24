import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor import MonitorError, load_state
from persist_state import StateRepository


class PersistenceTests(unittest.TestCase):
    """Real Git plumbing against disposable file-system remotes, never GitHub."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.remote = self.directory / "remote.git"
        self.seed = self.directory / "seed"
        self.work = self.directory / "runner"
        self.git(self.directory, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Fixture")
        self.git(self.seed, "config", "user.email", "fixture@example.com")
        self.initial = {"initialized": True, "last_seen_id": "100"}
        (self.seed / "state.json").write_text(json.dumps(self.initial) + "\n", encoding="utf-8")
        (self.seed / "code.txt").write_text("first\n", encoding="utf-8")
        self.git(self.seed, "add", "."); self.git(self.seed, "commit", "-m", "fixture baseline")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "-u", "origin", "main")
        self.git(self.directory, "clone", str(self.remote), str(self.work))
        self.repository = StateRepository(self.work)

    def git(self, root, *args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
        if result.returncode:
            self.fail(result.stderr)
        return result.stdout.strip()

    def remote_state(self):
        return json.loads(self.git(self.remote, "show", "main:state.json"))

    def candidate(self, cursor="101"):
        value = dict(self.initial, last_seen_id=cursor)
        (self.work / "state.json").write_text(json.dumps(value) + "\n", encoding="utf-8")
        return value

    def seed_commit(self, state=None, code=None):
        if state is not None:
            (self.seed / "state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
        if code is not None:
            (self.seed / "code.txt").write_text(code, encoding="utf-8")
        self.git(self.seed, "add", "."); self.git(self.seed, "commit", "-m", "concurrent fixture")
        self.git(self.seed, "push", "origin", "main")

    def test_prepare_ignores_stale_event_sha_and_loads_latest_main(self):
        event_sha = self.git(self.work, "rev-parse", "HEAD")
        self.seed_commit(state={"initialized": True, "last_seen_id": "200"})
        self.repository.prepare()
        self.assertNotEqual(event_sha, self.git(self.work, "rev-parse", "HEAD"))
        self.assertEqual(load_state(self.work / "state.json")["last_seen_id"], "200")
        self.assertEqual(load_state(self.repository.snapshot)["last_seen_id"], "200")

    def test_persist_changes_only_state_on_latest_tree(self):
        self.repository.prepare(); expected = self.candidate()
        self.seed_commit(code="concurrent implementation\n")
        parent = self.git(self.remote, "rev-parse", "main")
        self.assertEqual(self.repository.persist(), "PERSISTED")
        self.assertEqual(self.remote_state(), expected)
        self.assertEqual(self.git(self.remote, "show", "main:code.txt"), "concurrent implementation")
        self.assertEqual(self.git(self.remote, "diff", "--name-only", parent, "main"), "state.json")

    def test_non_fast_forward_push_retries_after_rechecking_state(self):
        self.repository.prepare(); expected = self.candidate(); original = self.repository.git; calls = 0
        def race(*args, **kwargs):
            nonlocal calls
            if args[0] == "push":
                calls += 1
                if calls == 1: self.seed_commit(code="changed during push\n")
            return original(*args, **kwargs)
        with patch.object(self.repository, "git", side_effect=race), patch("persist_state.time.sleep"):
            self.assertEqual(self.repository.persist(), "PERSISTED")
        self.assertEqual(calls, 2); self.assertEqual(self.remote_state(), expected)
        self.assertEqual(self.git(self.remote, "show", "main:code.txt"), "changed during push")

    def test_remote_advanced_does_not_get_overwritten_or_silently_discarded(self):
        self.repository.prepare(); local = self.candidate()
        remote = {"initialized": True, "last_seen_id": "200"}; self.seed_commit(state=remote)
        with self.assertRaisesRegex(MonitorError, "REMOTE_STATE_CHANGED"):
            self.repository.persist()
        self.assertEqual(self.remote_state(), remote)
        self.assertEqual(load_state(self.work / "state.json"), local)

    def test_remote_regression_is_not_overwritten(self):
        self.repository.prepare(); self.candidate()
        self.seed_commit(state={"initialized": True, "last_seen_id": "99"})
        with self.assertRaisesRegex(MonitorError, "REMOTE_STATE_CHANGED"):
            self.repository.persist()
        self.assertEqual(self.remote_state()["last_seen_id"], "99")

    def test_identical_remote_state_is_explicitly_already_persisted(self):
        self.repository.prepare(); expected = self.candidate()
        self.seed_commit(state=expected)
        self.assertEqual(self.repository.persist(), "ALREADY_PERSISTED_IDENTICAL_STATE")

    def test_candidate_regression_rejected(self):
        self.repository.prepare(); self.candidate("99")
        with self.assertRaisesRegex(MonitorError, "REGRESSION"):
            self.repository.persist()
        self.assertEqual(self.remote_state(), self.initial)

    def test_unchanged_state_does_not_create_commit(self):
        self.repository.prepare(); before = self.git(self.remote, "rev-parse", "main")
        self.assertEqual(self.repository.persist(), "UNCHANGED")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)

    def test_push_failure_is_bounded_and_candidate_survives(self):
        self.repository.prepare(); expected = self.candidate(); original = self.repository.git; calls = 0
        def fail_push(*args, **kwargs):
            nonlocal calls
            if args[0] == "push":
                calls += 1
                raise MonitorError("GIT_COMMAND_FAILED")
            return original(*args, **kwargs)
        with patch.object(self.repository, "git", side_effect=fail_push), patch("persist_state.time.sleep"):
            with self.assertRaises(MonitorError): self.repository.persist()
        self.assertEqual(calls, 3); self.assertEqual(self.remote_state(), self.initial)
        self.assertEqual(load_state(self.work / "state.json"), expected)

    def test_dirty_checkout_is_not_overwritten_by_prepare(self):
        expected = self.candidate()
        with self.assertRaisesRegex(MonitorError, "CLEAN_CHECKOUT"):
            self.repository.prepare()
        self.assertEqual(load_state(self.work / "state.json"), expected)


if __name__ == "__main__":
    unittest.main()
