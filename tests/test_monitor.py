import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor import (
    Config,
    DeepSeekTranslator,
    SMTPEmailClient,
    XClient,
    build_notification,
    build_digest,
    classify_post,
    resolve_smtp_host,
    run_monitor,
)


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


class FakeTranslator:
    def __init__(self, translation):
        self.translation = translation
        self.contents = []

    def translate(self, content):
        self.contents.append(content)
        return self.translation


def config():
    return Config(
        x_bearer_token="test-x-token",
        smtp_username="sender@gmail.com",
        smtp_app_password="test-app-password",
        alert_emails=("owner@example.com",),
        smtp_host="smtp.gmail.com",
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

    def test_new_posts_are_combined_in_one_email_and_state_advances(self):
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
        self.assertEqual(len(email.messages), 1)
        self.assertEqual(email.messages[0]["subject"], "[Tibo 更新] 2 条新内容")
        text = email.messages[0]["text"]
        self.assertLess(text.find("original text"), text.find("reply text"))
        self.assertIn("回复对象：@someone", text)
        self.assertIn("被回复原帖：parent post", text)
        self.assertEqual(state["last_seen_id"], "102")

    def test_digest_contains_each_post(self):
        subject, text, html_body = build_digest(
            [
                {"id": "101", "text": "first", "created_at": "2026-07-24T05:00:00Z"},
                {"id": "102", "text": "second", "created_at": "2026-07-24T06:00:00Z"},
            ],
            {},
            "thsottiaux",
        )

        self.assertEqual(subject, "[Tibo 更新] 2 条新内容")
        self.assertIn("--- 第 1 条 ---", text)
        self.assertIn("--- 第 2 条 ---", text)
        self.assertIn("first", text)
        self.assertIn("second", html_body)

    def test_notification_uses_note_tweet_text_and_beijing_time(self):
        translator = FakeTranslator("完整的中文帖子")
        subject, text, html_body = build_notification(
            {
                "id": "123",
                "text": "truncated",
                "note_tweet": {"text": "complete long post"},
                "created_at": "2026-07-24T06:30:00.000Z",
            },
            {},
            "thsottiaux",
            translator,
        )

        self.assertIn("complete long post", subject)
        self.assertIn("2026-07-24 14:30:00", text)
        self.assertIn("中文翻译：\n完整的中文帖子", text)
        self.assertIn("https://x.com/thsottiaux/status/123", html_body)
        self.assertEqual(translator.contents, ["complete long post"])

    @patch("monitor.request_json")
    def test_deepseek_translator_uses_chat_completions(self, request_json):
        request_json.return_value = {
            "choices": [{"message": {"content": "你好，世界"}}]
        }

        translation = DeepSeekTranslator("test-key").translate("Hello, world")

        self.assertEqual(translation, "你好，世界")
        self.assertEqual(request_json.call_args.args[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(request_json.call_args.kwargs["method"], "POST")
        self.assertEqual(
            request_json.call_args.kwargs["headers"],
            {"Authorization": "Bearer test-key"},
        )

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

    def test_resolves_gmail_and_qq_smtp_hosts(self):
        self.assertEqual(resolve_smtp_host("sender@gmail.com"), "smtp.gmail.com")
        self.assertEqual(resolve_smtp_host("123456@qq.com"), "smtp.qq.com")
        self.assertEqual(
            resolve_smtp_host("sender@example.com", "mail.example.com"),
            "mail.example.com",
        )

    @patch("monitor.smtplib.SMTP_SSL")
    def test_smtp_client_uses_app_password_and_ssl(self, smtp_ssl):
        smtp = smtp_ssl.return_value.__enter__.return_value
        client = SMTPEmailClient(
            "smtp.gmail.com",
            465,
            "sender@gmail.com",
            "app-password",
            ("recipient@example.com", "second@example.com"),
        )

        client.send(
            subject="subject",
            text="plain",
            html_body="<p>html</p>",
            idempotency_key="post-123",
        )

        smtp_ssl.assert_called_once()
        smtp.login.assert_called_once_with("sender@gmail.com", "app-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.com, second@example.com")
        self.assertEqual(message["Message-ID"], "<post-123@tibo-monitor.local>")


if __name__ == "__main__":
    unittest.main()
