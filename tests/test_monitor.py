import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor import Config, XClient, build_notification, classify_post, run_monitor


class FakeXClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def fetch_posts(self, username, since_id):
        self.calls.append((username, since_id))
        return self.response


class FakeEmailClient:
    def __init__(self):
        self.messages = []

    def send(self, **message):
        self.messages.append(message)


def config():
    return Config(
        x_bearer_token="test-x-token",
        resend_api_key="test-email-token",
        alert_email="owner@example.com",
    )


class MonitorTests(unittest.TestCase):
    def test_classifies_original_reply_and_quote(self):
        self.assertEqual(classify_post({"id": "1"}), "原创")
        self.assertEqual(
            classify_post({"id": "2", "in_reply_to_user_id": "9"}), "回复"
        )
        self.assertEqual(
            classify_post(
                {"id": "3", "referenced_tweets": [{"type": "quoted", "id": "8"}]}
            ),
            "引用",
        )

    def test_first_run_sets_baseline_without_historical_alerts(self):
        response = {
            "data": [
                {"id": "101", "text": "older"},
                {"id": "102", "text": "newer"},
            ]
        }
        email = FakeEmailClient()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            run_monitor(config(), FakeXClient(response), email, state_path)
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state, {"initialized": True, "last_seen_id": "102"})
        self.assertEqual(len(email.messages), 1)
        self.assertIn("监控已启用", email.messages[0]["subject"])

    def test_new_posts_are_emailed_oldest_first_and_state_advances(self):
        response = {
            "data": [
                {
                    "id": "102",
                    "text": "reply text",
                    "created_at": "2026-07-24T06:00:00.000Z",
                    "in_reply_to_user_id": "9",
                    "referenced_tweets": [{"type": "replied_to", "id": "90"}],
                },
                {
                    "id": "101",
                    "text": "original text",
                    "created_at": "2026-07-24T05:00:00.000Z",
                },
            ],
            "includes": {
                "users": [{"id": "9", "username": "someone", "name": "Someone"}],
                "tweets": [{"id": "90", "text": "parent post"}],
            },
        }
        email = FakeEmailClient()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                '{"initialized": true, "last_seen_id": "100"}\n',
                encoding="utf-8",
            )
            x_client = FakeXClient(response)
            sent = run_monitor(config(), x_client, email, state_path)
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(x_client.calls, [("thsottiaux", "100")])
        self.assertEqual(sent, 2)
        self.assertIn("original text", email.messages[0]["subject"])
        self.assertIn("[Tibo 回复]", email.messages[1]["subject"])
        self.assertIn("回复对象：@someone", email.messages[1]["text"])
        self.assertIn("被回复原帖：parent post", email.messages[1]["text"])
        self.assertEqual(state["last_seen_id"], "102")

    def test_notification_uses_note_tweet_text_and_beijing_time(self):
        subject, text, html_body = build_notification(
            {
                "id": "123",
                "text": "truncated",
                "note_tweet": {"text": "complete long post"},
                "created_at": "2026-07-24T06:30:00.000Z",
            },
            {},
            "thsottiaux",
        )

        self.assertIn("complete long post", subject)
        self.assertIn("2026-07-24 14:30:00", text)
        self.assertIn("https://x.com/thsottiaux/status/123", html_body)

    @patch("monitor.request_json")
    def test_initial_x_lookup_reads_only_one_small_page(self, request_json):
        request_json.return_value = {
            "data": [{"id": "123", "text": "latest"}],
            "meta": {"next_token": "unused-token"},
        }

        response = XClient("token").fetch_posts("thsottiaux", None)

        self.assertEqual([post["id"] for post in response["data"]], ["123"])
        self.assertEqual(request_json.call_count, 1)
        requested_url = request_json.call_args.args[0]
        self.assertIn("max_results=10", requested_url)


if __name__ == "__main__":
    unittest.main()
