from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


X_API_BASE = "https://api.x.com/2"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_STATE_PATH = Path(__file__).with_name("state.json")


@dataclass(frozen=True)
class Config:
    x_bearer_token: str
    smtp_username: str
    smtp_app_password: str
    alert_emails: tuple[str, ...]
    smtp_host: str
    deepseek_api_key: str = ""
    smtp_port: int = 465
    target_username: str = "thsottiaux"

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "X_BEARER_TOKEN": os.environ.get("X_BEARER_TOKEN", "").strip(),
            "SMTP_USERNAME": os.environ.get("SMTP_USERNAME", "").strip(),
            "SMTP_APP_PASSWORD": os.environ.get("SMTP_APP_PASSWORD", "").strip(),
            "ALERT_EMAIL": os.environ.get("ALERT_EMAIL", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            x_bearer_token=required["X_BEARER_TOKEN"],
            smtp_username=required["SMTP_USERNAME"],
            smtp_app_password=required["SMTP_APP_PASSWORD"],
            alert_emails=tuple(
                email
                for email in (
                    required["ALERT_EMAIL"],
                    os.environ.get("ADDITIONAL_ALERT_EMAIL", "").strip(),
                )
                if email
            ),
            smtp_host=resolve_smtp_host(
                required["SMTP_USERNAME"], os.environ.get("SMTP_HOST", "").strip()
            ),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            smtp_port=int(os.environ.get("SMTP_PORT", "465")),
            target_username=os.environ.get("TARGET_USERNAME", "thsottiaux").strip().lstrip("@"),
        )


def resolve_smtp_host(username: str, configured_host: str = "") -> str:
    if configured_host:
        return configured_host
    domain = username.rsplit("@", 1)[-1].lower()
    hosts = {
        "gmail.com": "smtp.gmail.com",
        "googlemail.com": "smtp.gmail.com",
        "qq.com": "smtp.qq.com",
    }
    if domain not in hosts:
        raise ValueError(
            "Cannot infer SMTP server. Set SMTP_HOST for this email provider."
        )
    return hosts[domain]


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc.reason}") from exc

    if not content:
        return {}
    return json.loads(content.decode("utf-8"))


class XClient:
    def __init__(self, bearer_token: str):
        self._headers = {"Authorization": f"Bearer {bearer_token}"}

    def fetch_posts(
        self, username: str, since_id: str | None
    ) -> dict[str, Any]:
        all_posts: list[dict[str, Any]] = []
        all_users: dict[str, dict[str, Any]] = {}
        all_included_posts: dict[str, dict[str, Any]] = {}
        next_token: str | None = None

        while True:
            params = {
                "query": f"from:{username} -is:retweet",
                "max_results": "100" if since_id else "10",
                "tweet.fields": (
                    "author_id,conversation_id,created_at,in_reply_to_user_id,"
                    "note_tweet,referenced_tweets"
                ),
                "expansions": (
                    "author_id,in_reply_to_user_id,referenced_tweets.id,"
                    "referenced_tweets.id.author_id"
                ),
                "user.fields": "id,name,username",
            }
            if since_id:
                params["since_id"] = since_id
            if next_token:
                params["next_token"] = next_token

            url = (
                f"{X_API_BASE}/tweets/search/recent?"
                f"{urllib.parse.urlencode(params)}"
            )
            page = request_json(url, headers=self._headers)
            all_posts.extend(page.get("data", []))

            includes = page.get("includes", {})
            for user in includes.get("users", []):
                all_users[user["id"]] = user
            for post in includes.get("tweets", []):
                all_included_posts[post["id"]] = post

            next_token = page.get("meta", {}).get("next_token")
            if not since_id or not next_token:
                break

        result: dict[str, Any] = {"data": all_posts, "includes": {}}
        if all_users:
            result["includes"]["users"] = list(all_users.values())
        if all_included_posts:
            result["includes"]["tweets"] = list(all_included_posts.values())
        return result


class SMTPEmailClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        app_password: str,
        to_emails: tuple[str, ...],
    ):
        self._host = host
        self._port = port
        self._username = username
        self._app_password = app_password
        self._to_emails = to_emails

    def send(
        self,
        *,
        subject: str,
        text: str,
        html_body: str,
        idempotency_key: str,
    ) -> None:
        message = EmailMessage()
        message["From"] = formataddr(("Tibo Monitor", self._username))
        message["To"] = ", ".join(self._to_emails)
        message["Subject"] = subject
        message["Message-ID"] = f"<{idempotency_key}@tibo-monitor.local>"
        message.set_content(text)
        message.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            self._host, self._port, timeout=30, context=context
        ) as smtp:
            smtp.login(self._username, self._app_password)
            smtp.send_message(message)


class DeepSeekTranslator:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def translate(self, content: str) -> str:
        return self.translate_many([content])[content]

    def translate_many(self, contents: list[str]) -> dict[str, str]:
        unique_contents = list(dict.fromkeys(contents))
        if not unique_contents:
            return {}

        for attempt in range(2):
            try:
                response = request_json(
                    DEEPSEEK_API_URL,
                    method="POST",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    payload={
                        "model": "deepseek-chat",
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Translate each X post into Simplified Chinese. Preserve "
                                    "@mentions, hashtags, URLs, code, and line breaks. Return "
                                    "only a JSON object with a translations array in the same "
                                    "order as the input texts."
                                ),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"texts": unique_contents}, ensure_ascii=False
                                ),
                            },
                        ],
                        "temperature": 0,
                    },
                    timeout=15,
                )
                raw_translation = str(
                    response["choices"][0]["message"]["content"]
                ).strip()
                if raw_translation.startswith("```"):
                    raw_translation = "\n".join(raw_translation.splitlines()[1:-1])
                translations = json.loads(raw_translation)["translations"]
                if not isinstance(translations, list) or len(translations) != len(unique_contents):
                    raise ValueError("DeepSeek returned the wrong number of translations.")
                return {
                    content: str(translation).strip()
                    for content, translation in zip(unique_contents, translations)
                }
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                error = RuntimeError("DeepSeek returned an unexpected translation response.")
                error.__cause__ = exc
            except (RuntimeError, TimeoutError, OSError) as exc:
                error = exc

            if attempt == 1:
                raise error
            print(f"Translation attempt failed; retrying once: {error}", file=sys.stderr)
            time.sleep(1)

        raise AssertionError("Unreachable")


class CachedTranslator:
    def __init__(self, translations: dict[str, str]):
        self._translations = translations

    def translate(self, content: str) -> str:
        return self._translations.get(content, "")
        try:
            return str(response["choices"][0]["message"]["content"]).strip()
        except (IndexError, KeyError, TypeError) as exc:
            raise RuntimeError("DeepSeek returned an unexpected translation response.") from exc


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"initialized": False, "last_seen_id": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    return {
        "initialized": bool(state.get("initialized", False)),
        "last_seen_id": state.get("last_seen_id"),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def post_text(post: dict[str, Any]) -> str:
    note_tweet = post.get("note_tweet")
    if isinstance(note_tweet, dict) and note_tweet.get("text"):
        return str(note_tweet["text"])
    return str(post.get("text", ""))


def classify_post(post: dict[str, Any]) -> str:
    references = post.get("referenced_tweets") or []
    reference_types = {item.get("type") for item in references}
    if post.get("in_reply_to_user_id") or "replied_to" in reference_types:
        return "回复"
    if "quoted" in reference_types:
        return "引用"
    return "原创"


def format_beijing_time(value: str | None) -> str:
    if not value:
        return "未知时间"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def translate_content(content: str, translator: Any | None) -> str | None:
    if translator is None or not any(char.isascii() and char.isalpha() for char in content):
        return None
    try:
        translation = str(translator.translate(content)).strip()
    except Exception as exc:
        print(f"Translation failed; sending original only: {exc}", file=sys.stderr)
        return None
    return translation if translation and translation != content.strip() else None


def collect_translation_sources(
    posts: list[dict[str, Any]], includes: dict[str, Any]
) -> list[str]:
    included_posts = {
        included_post["id"]: included_post
        for included_post in includes.get("tweets", [])
    }
    sources: list[str] = []
    for post in posts:
        sources.append(post_text(post))
        for reference in post.get("referenced_tweets") or []:
            if reference.get("type") not in {"replied_to", "quoted"}:
                continue
            parent = included_posts.get(reference.get("id"))
            if parent:
                sources.append(post_text(parent))
    return list(
        dict.fromkeys(
            source
            for source in sources
            if any(char.isascii() and char.isalpha() for char in source)
        )
    )


def prepare_translator(
    posts: list[dict[str, Any]], includes: dict[str, Any], translator: Any | None
) -> Any | None:
    translate_many = getattr(translator, "translate_many", None)
    if not callable(translate_many):
        return translator
    try:
        return CachedTranslator(translate_many(collect_translation_sources(posts, includes)))
    except Exception as exc:
        print(f"Translation failed; sending original only: {exc}", file=sys.stderr)
        return None


def build_notification(
    post: dict[str, Any],
    includes: dict[str, Any],
    username: str,
    translator: Any | None = None,
) -> tuple[str, str, str]:
    kind = classify_post(post)
    created_at = format_beijing_time(post.get("created_at"))
    content = post_text(post)
    translation = translate_content(content, translator)
    post_url = f"https://x.com/{username}/status/{post['id']}"

    users = {user["id"]: user for user in includes.get("users", [])}
    included_posts = {
        included_post["id"]: included_post
        for included_post in includes.get("tweets", [])
    }

    context_lines: list[str] = []
    reply_user_id = post.get("in_reply_to_user_id")
    if reply_user_id:
        reply_user = users.get(reply_user_id, {})
        reply_username = reply_user.get("username")
        if reply_username:
            context_lines.append(f"回复对象：@{reply_username}")

    for reference in post.get("referenced_tweets") or []:
        if reference.get("type") not in {"replied_to", "quoted"}:
            continue
        parent = included_posts.get(reference.get("id"))
        if parent:
            label = "被回复原帖" if reference.get("type") == "replied_to" else "被引用原帖"
            parent_content = post_text(parent)
            context_lines.append(f"{label}：{parent_content}")
            parent_translation = translate_content(parent_content, translator)
            if parent_translation:
                context_lines.append(
                    f"{label}中文翻译：\n{parent_translation}"
                )

    context = "\n".join(context_lines)
    subject_preview = " ".join(content.split())
    if len(subject_preview) > 60:
        subject_preview = subject_preview[:57] + "..."
    subject = f"[Tibo {kind}] {subject_preview or post['id']}"

    text_parts = [
        f"Tibo @{username}",
        f"时间：{created_at}（北京时间）",
        f"类型：{kind}",
        "",
        content,
    ]
    if translation:
        text_parts.extend(["", f"中文翻译：\n{translation}"])
    if context:
        text_parts.extend(["", context])
    text_parts.extend(["", f"原帖：{post_url}"])
    text_body = "\n".join(text_parts)

    html_parts = [
        f"<p><strong>Tibo @{html.escape(username)}</strong><br>",
        f"时间：{html.escape(created_at)}（北京时间）<br>",
        f"类型：{html.escape(kind)}</p>",
        f"<blockquote>{html.escape(content).replace(chr(10), '<br>')}</blockquote>",
    ]
    if translation:
        html_parts.append(
            "<p><strong>中文翻译：</strong><br>"
            f"{html.escape(translation).replace(chr(10), '<br>')}</p>"
        )
    if context:
        html_parts.append(
            f"<p>{html.escape(context).replace(chr(10), '<br>')}</p>"
        )
    html_parts.append(
        f'<p><a href="{html.escape(post_url)}">在 X 上打开原帖</a></p>'
    )
    return subject, text_body, "".join(html_parts)


def build_digest(
    posts: list[dict[str, Any]],
    includes: dict[str, Any],
    username: str,
    translator: Any | None = None,
) -> tuple[str, str, str]:
    count = len(posts)
    prepared_translator = prepare_translator(posts, includes, translator)
    text_parts = [f"本次检测到 {count} 条 Tibo @{username} 的新内容。"]
    html_parts = [
        f"<p>本次检测到 <strong>{count}</strong> 条 Tibo "
        f"@{html.escape(username)} 的新内容。</p>"
    ]

    for index, post in enumerate(posts, start=1):
        _, text_body, html_body = build_notification(
            post, includes, username, prepared_translator
        )
        text_parts.extend([f"\n--- 第 {index} 条 ---", text_body])
        html_parts.append(f"<hr><h2>第 {index} 条</h2>{html_body}")

    return (
        f"[Tibo 更新] {count} 条新内容",
        "\n".join(text_parts),
        "".join(html_parts),
    )


def run_monitor(
    config: Config,
    x_client: Any,
    email_client: Any,
    state_path: Path,
    translator: Any | None = None,
) -> int:
    state = load_state(state_path)
    response = x_client.fetch_posts(config.target_username, state["last_seen_id"])
    posts = response.get("data", [])
    posts.sort(key=lambda post: int(post["id"]))

    if not state["initialized"]:
        newest_id = posts[-1]["id"] if posts else None
        email_client.send(
            subject=f"[Tibo Monitor] @{config.target_username} 监控已启用",
            text=(
                f"已开始监控 @{config.target_username} 的原创帖、回复和引用帖。\n"
                "检查频率：每 30 分钟。\n"
                "首次运行只建立基线，不发送历史帖子。"
            ),
            html_body=(
                f"<p>已开始监控 <strong>@{html.escape(config.target_username)}</strong>"
                " 的原创帖、回复和引用帖。</p>"
                "<p>检查频率：每 30 分钟。首次运行只建立基线，不发送历史帖子。</p>"
            ),
            idempotency_key=f"tibo-monitor-init-{config.target_username}",
        )
        state = {"initialized": True, "last_seen_id": newest_id}
        save_state(state_path, state)
        print(f"Initialized monitor at post ID {newest_id or 'none'}.")
        return 0

    if posts:
        subject, text_body, html_body = build_digest(
            posts, response.get("includes", {}), config.target_username, translator
        )
        email_client.send(
            subject=subject,
            text=text_body,
            html_body=html_body,
            idempotency_key=f"tibo-digest-{posts[-1]['id']}",
        )
        state["last_seen_id"] = posts[-1]["id"]
        save_state(state_path, state)

    print(f"Sent one digest for {len(posts)} post(s).")
    return len(posts)


def send_sample(
    config: Config,
    x_client: Any,
    email_client: Any,
    translator: Any | None,
) -> None:
    response = x_client.fetch_posts(config.target_username, None)
    posts = response.get("data", [])
    if not posts:
        raise RuntimeError("No recent Tibo posts are available for a sample email.")
    posts.sort(key=lambda post: int(post["id"]))
    post = posts[-1]
    subject, text_body, html_body = build_digest(
        [post], response.get("includes", {}), config.target_username, translator
    )
    if collect_translation_sources([post], response.get("includes", {})) and "中文翻译：" not in text_body:
        raise RuntimeError("Translation is unavailable, so no sample email was sent.")
    email_client.send(
        subject=f"[翻译示例] {subject}",
        text=("这是翻译功能示例，不代表检测到新帖。\n\n" + text_body),
        html_body=("<p>这是翻译功能示例，不代表检测到新帖。</p>" + html_body),
        idempotency_key=f"tibo-sample-{post['id']}-{int(time.time())}",
    )
    print(f"Sent translated sample for post ID {post['id']}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor one X account and email new posts.")
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to the persistent state JSON file.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Send one recent post as a translated sample without changing monitor state.",
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
        x_client = XClient(config.x_bearer_token)
        email_client = SMTPEmailClient(
            config.smtp_host,
            config.smtp_port,
            config.smtp_username,
            config.smtp_app_password,
            config.alert_emails,
        )
        translator = (
            DeepSeekTranslator(config.deepseek_api_key)
            if config.deepseek_api_key
            else None
        )
        if args.sample:
            send_sample(config, x_client, email_client, translator)
            return 0
        return_code = run_monitor(config, x_client, email_client, args.state, translator)
        return 0 if return_code >= 0 else 1
    except Exception as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
