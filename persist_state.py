"""Actions-only Git state persistence. Tests use disposable local repositories."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from monitor import MonitorError, check_state_progress, load_state, validate_state


class StateRepository:
    def __init__(self, root: Path):
        self.root = root
        self.end = time.monotonic() + 45

    def git(self, *args: str, input: str | None = None, env: dict | None = None) -> str:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise MonitorError("PERSIST_BUDGET_EXHAUSTED")
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, input=input, env=env,
                text=True, encoding="utf-8", capture_output=True,
                timeout=min(10, remaining),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise MonitorError("GIT_UNAVAILABLE_OR_TIMEOUT") from None
        if result.returncode:
            operation = args[0].upper() if args[0] in {"fetch", "push", "checkout"} else "COMMAND"
            raise MonitorError(f"GIT_{operation}_FAILED")
        return result.stdout.strip()

    @property
    def snapshot(self) -> Path:
        path = Path(self.git("rev-parse", "--git-path", "tibo-state-base.json"))
        return path if path.is_absolute() else self.root / path

    def prepare(self) -> None:
        if self.git("status", "--porcelain"):
            raise MonitorError("PREPARE_REQUIRES_CLEAN_CHECKOUT")
        self.git("fetch", "--no-tags", "origin", "main")
        # Runs after Actions concurrency admission, independent of event SHA.
        self.git("checkout", "-B", "main", "origin/main")
        state = load_state(self.root / "state.json")
        self.snapshot.write_text(json.dumps(state) + "\n", encoding="utf-8")

    def persist(self) -> str:
        baseline = load_state(self.snapshot)
        candidate = load_state(self.root / "state.json")
        check_state_progress(baseline, candidate)
        if candidate == baseline:
            return "UNCHANGED"
        for attempt in range(3):
            self.git("fetch", "--no-tags", "origin", "main")
            parent = self.git("rev-parse", "origin/main")
            try:
                remote = validate_state(json.loads(self.git("show", f"{parent}:state.json")))
            except ValueError:
                raise MonitorError("REMOTE_STATE_INVALID_JSON") from None
            if remote == candidate:
                return "ALREADY_PERSISTED_IDENTICAL_STATE"
            if remote != baseline:
                # Keep candidate and snapshot for diagnosis; never reset or take max(id).
                raise MonitorError("REMOTE_STATE_CHANGED_RECONCILIATION_REQUIRED")
            check_state_progress(remote, candidate)
            # Build on the latest remote tree so unrelated concurrent changes survive.
            # A private index avoids resetting or rebasing the running business checkout.
            with tempfile.TemporaryDirectory(dir=self.snapshot.parent, prefix="tibo-index-") as directory:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
                self.git("read-tree", parent, env=env)
                blob = self.git("hash-object", "-w", "--stdin", input=json.dumps(candidate, ensure_ascii=False, indent=2) + "\n")
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},state.json", env=env)
                tree = self.git("write-tree", env=env)
                commit = self.git(
                    "-c", "user.name=github-actions[bot]", "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit-tree", tree, "-p", parent, "-m", "Update monitor state",
                )
            try:
                self.git("push", "origin", f"{commit}:refs/heads/main")
                return "PERSISTED"
            except MonitorError:
                if attempt == 2:
                    raise
                print(f"State push retry {attempt + 1}; refetching and checking remote state.")
                time.sleep(1)
        raise AssertionError("Unreachable")


def publish(status: str) -> None:
    print(f"STATE_PERSISTENCE: {status}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"\nState persistence: **{status}**\n")
            if status.startswith("FAILED"):
                stream.write("SMTP may already have accepted the batch. State is not confirmed; a later attempt can duplicate email. Inspect the monitor batch_id and SMTP counters.\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "persist"))
    args = parser.parse_args()
    try:
        repository = StateRepository(Path.cwd())
        if args.phase == "prepare":
            repository.prepare()
            publish("READY_FROM_LATEST_MAIN")
        else:
            publish(repository.persist())
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, MonitorError) else type(exc).__name__
        publish(f"FAILED / {code}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
