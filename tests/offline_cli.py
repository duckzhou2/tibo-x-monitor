"""Subprocess boundary fixture. Only run inside a disposable local Git clone."""

import io
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch


root = Path.cwd()
sys.path.insert(0, str(root))
origin = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()
assert Path(origin).is_absolute() and Path(origin).is_dir(), "Fixture requires a local file-system remote"
scenario = json.loads((root / ".git/scenario.json").read_text(encoding="utf-8"))
calls = []
outbox = []


class Response(io.BytesIO):
    pass


def http(request, timeout):
    url = urllib.parse.urlparse(request.full_url)
    calls.append({"host": url.hostname, "query": urllib.parse.parse_qs(url.query), "timeout": timeout})
    if url.hostname == "api.x.com":
        query = calls[-1]["query"]
        assert request.get_header("Authorization") == "Bearer fixture-x"
        index = int(query.get("next_token", ["0"])[0])
        if scenario.get("x_fail_page") == index:
            raise urllib.error.HTTPError(request.full_url, 503, "fixture-private", {}, io.BytesIO(b"fixture-private"))
        return Response(json.dumps(scenario["pages"][index]).encode("utf-8"))
    assert url.hostname == "api.deepseek.com", "Unexpected outbound host"
    payload = json.loads(request.data)
    assert payload["model"] == "deepseek-flash"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    mode = scenario.get("translation", "ok")
    if mode == "timeout":
        raise TimeoutError("fixture-private")
    if mode == "http":
        raise urllib.error.HTTPError(request.full_url, 503, "fixture-private", {}, io.BytesIO(b"fixture-private"))
    entries = json.loads(payload["messages"][1]["content"])["sources"]
    rows = [dict(entry, text="中文译文 " + entry["source"]) for entry in entries if entry["source"] not in scenario.get("untranslated", [])]
    content = json.dumps({"translations": rows}, ensure_ascii=False)
    if mode == "invalid": content = '{"translations": ['
    if mode == "empty": content = ""
    completion = {"choices": [{"finish_reason": "length" if mode == "truncated" else "stop", "message": {"content": content}}]}
    return Response(json.dumps(completion, ensure_ascii=False).encode("utf-8"))


class SMTP:
    def __init__(self, host, port, **kwargs):
        assert (host, port) == ("smtp.example.invalid", 465)

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def login(self, username, password):
        assert (username, password) == ("sender@example.invalid", "fixture-smtp")

    def send_message(self, message):
        outbox.append(message.as_string())
        (root / ".git/outbox.json").write_text(json.dumps(outbox), encoding="utf-8")
        if scenario.get("interrupt_after_acceptance"):
            os._exit(75)
        return {"second@example.invalid": (550, b"fixture-private")} if scenario.get("partial_refusal") else {}


def no_network(*args, **kwargs):
    raise AssertionError("Real network forbidden")


original_run = subprocess.run
def git_boundary(args, **kwargs):
    if scenario.get("push_failure") and args[:2] == ["git", "push"]:
        return subprocess.CompletedProcess(args, 1, "", "fixture-private")
    return original_run(args, **kwargs)


with patch("socket.create_connection", no_network), patch.object(socket.socket, "connect", no_network), patch("urllib.request.urlopen", http), patch("smtplib.SMTP_SSL", SMTP), patch("time.sleep"), patch("subprocess.run", git_boundary):
    try:
        phase, *flags = sys.argv[1:]
        sys.argv = [str(root / ("monitor.py" if phase == "monitor" else "persist_state.py")), *flags]
        if scenario.get("save_failure"):
            with patch("os.replace", side_effect=OSError("fixture-private")):
                runpy.run_path(sys.argv[0], run_name="__main__")
        else:
            runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as exc:
        code = exc.code
    finally:
        (root / ".git/http-calls.json").write_text(json.dumps(calls), encoding="utf-8")
sys.exit(code)
