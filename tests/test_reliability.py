import contextlib
import io
import http.client
import json
import os
import smtplib
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import monitor as m
from test_monitor import FakeBatchTranslator, FakeEmailClient, FakeXClient, config


def page(ids=(), **extra):
    return {
        "data": [{"id": str(i), "text": f"English {i}"} for i in ids],
        "meta": {"result_count": len(ids)},
        **extra,
    }


def completion(items, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps({"translations": items})}}]}


def translated(source, text="中文译文"):
    return {"id": m.DeepSeekTranslator.source_id(source), "source": source, "text": text}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.create_connection", side_effect=AssertionError("Real network forbidden in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)


class XTests(OfflineTests):
    def fetch(self, *pages, since="100", **kwargs):
        with patch("monitor.request_json", side_effect=pages) as request:
            result = m.XClient("fixture").fetch_posts("thsottiaux", since, **kwargs)
        return result, request

    def test_101_posts_across_pages(self):
        first = page(list(range(201, 101, -1)))
        first["meta"]["next_token"] = "second"
        result, request = self.fetch(first, page([101]))
        self.assertEqual(len(result["data"]), 101)
        self.assertEqual(result["stats"], {"pages": 2, "fetched": 101})
        self.assertIn("next_token=second", request.call_args.args[0])
        self.assertIn("from%3Athsottiaux+-is%3Aretweet", request.call_args.args[0])
        self.assertIn("sort_order=recency", request.call_args.args[0])

    def test_overlap_is_deduplicated(self):
        first = page([103, 102]); first["meta"]["next_token"] = "second"
        result, _ = self.fetch(first, page([102, 101]))
        self.assertEqual([p["id"] for p in result["data"]], ["103", "102", "101"])
        self.assertEqual(result["stats"]["fetched"], 4)

    def test_repeated_token_is_fatal(self):
        first = page([103]); first["meta"]["next_token"] = "same"
        second = page([102]); second["meta"]["next_token"] = "same"
        with self.assertRaisesRegex(m.MonitorError, "PAGINATION_STALLED"):
            self.fetch(first, second)

    def test_no_progress_with_new_token_is_fatal(self):
        first = page([103]); first["meta"]["next_token"] = "a"
        second = page([103]); second["meta"]["next_token"] = "b"
        with self.assertRaisesRegex(m.MonitorError, "PAGINATION_STALLED"):
            self.fetch(first, second)

    def test_abnormal_order_and_cursor_are_rejected(self):
        for response in [page([101, 102]), page([100]), page([99])]:
            with self.subTest(response=response), self.assertRaises(m.MonitorError):
                self.fetch(response)
        first = page([105]); first["meta"]["next_token"] = "a"
        with self.assertRaisesRegex(m.MonitorError, "PAGINATION_ORDER"):
            self.fetch(first, page([106]))

    def test_conflicting_duplicate_rejected(self):
        first = page([102]); first["meta"]["next_token"] = "a"
        second = page([102]); second["data"][0]["text"] = "changed"
        with self.assertRaisesRegex(m.MonitorError, "CONFLICTING_DUPLICATE"):
            self.fetch(first, second)

    def test_normal_empty_is_accepted(self):
        self.assertEqual(self.fetch({"meta": {"result_count": 0}})[0]["data"], [])

    def test_invalid_empty_and_incomplete_responses_fail(self):
        for response in [{}, {"errors": []}, {"meta": {"result_count": 1}}, {"data": None, "meta": {"result_count": 0}}, {"meta": {"result_count": True}}, {"meta": {"result_count": 0, "next_token": "a"}}]:
            with self.subTest(response=response), self.assertRaises(m.MonitorError):
                self.fetch(response)

    def test_reference_missing_is_nonfatal_and_visible(self):
        response = page([101])
        response["data"][0]["referenced_tweets"] = [{"type": "quoted", "id": "9"}]
        response["errors"] = [{"type": "https://api.x.com/2/problems/resource-not-found", "resource_type": "tweet", "resource_id": "9"}]
        result, _ = self.fetch(response)
        report = m.RunReport()
        _, text, _ = m.build_digest(result["data"], result["includes"], "thsottiaux", report=report)
        self.assertIn("上下文不可用", text)
        self.assertEqual(report.context_missing, 1)

    def test_primary_unknown_and_rate_partial_errors_fail(self):
        for error in [
            {"type": "resource-not-found", "resource_type": "tweet", "resource_id": "101"},
            {"type": "resource-not-found", "resource_type": "tweet", "resource_id": "999"},
            {"type": "rate-limit-exceeded"},
            {"title": "Unavailable"},
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(m.MonitorError, "PARTIAL_PRIMARY"):
                self.fetch(page([101], errors=[error]))

    def test_partial_error_without_data_is_not_normal_empty(self):
        with self.assertRaises(m.MonitorError):
            self.fetch(page(errors=[{"type": "resource-not-found"}]))

    def test_initialized_empty_cursor_paginates(self):
        first = page([102]); first["meta"]["next_token"] = "second"
        result, request = self.fetch(first, page([101]), since=None)
        self.assertEqual(len(result["data"]), 2)
        self.assertIn("max_results=100", request.call_args.args[0])

    def test_missing_body_and_invalid_post_fields_fail(self):
        for field, value in [("text", None), ("id", 101), ("note_tweet", {}), ("created_at", "bad"), ("referenced_tweets", {})]:
            response = page([101]); response["data"][0][field] = value
            with self.subTest(field=field), self.assertRaises(m.MonitorError):
                self.fetch(response)

    def test_retweet_response_is_rejected(self):
        response = page([101]); response["data"][0]["referenced_tweets"] = [{"type": "retweeted", "id": "9"}]
        with self.assertRaisesRegex(m.MonitorError, "RETWEET"):
            self.fetch(response)

    def test_inconsistent_meta_ids_are_rejected(self):
        response = page([102, 101]); response["meta"]["newest_id"] = "103"
        with self.assertRaisesRegex(m.MonitorError, "META_IDS"):
            self.fetch(response)

    def test_unidentified_user_error_is_fatal(self):
        error = {"type": "resource-not-found", "resource_type": "user"}
        with self.assertRaisesRegex(m.MonitorError, "PARTIAL_PRIMARY"):
            self.fetch(page([101], errors=[error]))

    def test_identified_unavailable_user_expansion_is_nonfatal(self):
        response = page([101]); response["data"][0]["author_id"] = "9"
        response["errors"] = [{"type": "resource-not-found", "resource_type": "user", "resource_id": "9"}]
        self.assertEqual(len(self.fetch(response)[0]["data"]), 1)


class RequestTests(OfflineTests):
    def response(self, body):
        result = Mock(); result.__enter__ = Mock(return_value=result); result.__exit__ = Mock(return_value=False)
        result.read.return_value = body
        return result

    def http_error(self, code, headers=None):
        return urllib.error.HTTPError("https://fixture.invalid", code, "sensitive detail", headers or {}, io.BytesIO(b"sensitive body"))

    def test_401_403_fail_without_retry_or_body(self):
        for code in [401, 403]:
            with patch("monitor.urllib.request.urlopen", side_effect=self.http_error(code)) as request, patch("monitor.time.sleep") as sleep:
                with self.assertRaisesRegex(m.MonitorError, f"^HTTP_{code}$"):
                    m.request_json("https://fixture.invalid")
                self.assertEqual(request.call_count, 1); sleep.assert_not_called()

    def test_429_honors_retry_after(self):
        with patch("monitor.urllib.request.urlopen", side_effect=[self.http_error(429, {"Retry-After": "3"}), self.response(b'{"ok": true}')]) as request, patch("monitor.time.sleep") as sleep:
            self.assertTrue(m.request_json("https://fixture.invalid")["ok"])
            sleep.assert_called_once_with(3); self.assertEqual(request.call_count, 2)

    def test_long_retry_after_does_not_retry_early(self):
        with patch("monitor.urllib.request.urlopen", side_effect=self.http_error(429, {"Retry-After": "120"})) as request, patch("monitor.time.sleep") as sleep:
            with self.assertRaisesRegex(m.MonitorError, "BUDGET"):
                m.request_json("https://fixture.invalid")
            sleep.assert_not_called(); self.assertEqual(request.call_count, 1)

    def test_retry_after_http_date_and_reset_time(self):
        headers = {"Retry-After": "Thu, 01 Jan 1970 00:00:05 GMT", "x-rate-limit-reset": "7"}
        with patch("monitor.time.time", return_value=0), patch("monitor.urllib.request.urlopen", side_effect=[self.http_error(429, headers), self.response(b"{}")]), patch("monitor.time.sleep") as sleep:
            m.request_json("https://fixture.invalid")
            sleep.assert_called_once_with(7)

    def test_5xx_and_network_retries_are_bounded(self):
        for error in [self.http_error(503), urllib.error.URLError("sensitive proxy"), TimeoutError("sensitive"), http.client.IncompleteRead(b"partial")]:
            with patch("monitor.urllib.request.urlopen", side_effect=error) as request, patch("monitor.time.sleep"):
                with self.assertRaises(m.MonitorError): m.request_json("https://fixture.invalid")
                self.assertEqual(request.call_count, 3)

    def test_empty_nonobject_and_invalid_json_fail(self):
        for body in [b"", b"[]", b"null", b"invalid"]:
            with patch("monitor.urllib.request.urlopen", return_value=self.response(body)):
                with self.assertRaisesRegex(m.MonitorError, "INVALID_JSON"):
                    m.request_json("https://fixture.invalid")

    def test_expired_budget_never_calls_network(self):
        with patch("monitor.urllib.request.urlopen") as request:
            with self.assertRaisesRegex(m.MonitorError, "BUDGET"):
                m.request_json("https://fixture.invalid", deadline=m.Deadline(-1))
            request.assert_not_called()


class TranslationTests(OfflineTests):
    def translate(self, responses, sources):
        with patch("monitor.request_json", side_effect=responses) as request, patch("monitor.time.sleep"):
            result = m.DeepSeekTranslator("fixture").translate_many(sources)
        return result, request

    def test_two_timeouts_fall_back_and_keep_original(self):
        translator = m.DeepSeekTranslator("fixture")
        with patch("monitor.request_json", side_effect=TimeoutError), patch("monitor.time.sleep"):
            report = m.RunReport()
            _, text, html = m.build_digest([{"id": "101", "text": "Full original English"}], {}, "thsottiaux", translator, report)
        self.assertIn("Full original English", text)
        self.assertIn("中文翻译失败，已保留英文原文", text)
        self.assertIn("TRANSLATION_FALLBACK", html)
        self.assertEqual((report.translation_needed, report.translation_success, report.translation_failed), (1, 0, 1))

    def test_partial_success_retained_and_only_missing_retried(self):
        result, request = self.translate([completion([translated("first")]), completion([translated("second", "第二译文")])], ["first", "second"])
        self.assertEqual(result, {"first": "中文译文", "second": "第二译文"})
        payload = json.loads(request.call_args.kwargs["payload"]["messages"][1]["content"])
        self.assertEqual([x["source"] for x in payload["sources"]], ["second"])

    def test_partial_success_survives_second_failure(self):
        result, _ = self.translate([completion([translated("first")]), TimeoutError()], ["first", "second"])
        self.assertEqual(result, {"first": "中文译文"})

    def test_empty_null_nonstring_and_untranslated_results_rejected(self):
        for text in ["", " ", None, 123, {}, [], "English", "other English"]:
            with self.subTest(text=text):
                result, request = self.translate([completion([translated("English", text)])] * 2, ["English"])
                self.assertEqual(result, {}); self.assertEqual(request.call_count, 2)

    def test_wrong_source_unknown_id_duplicate_id_rejected(self):
        good = translated("English")
        for items in [[dict(good, source="different")], [dict(good, id="unknown")], [good, good], ["译文"], []]:
            with self.subTest(items=items):
                self.assertEqual(self.translate([completion(items)] * 2, ["English"])[0], {})

    def test_shuffled_response_maps_by_id(self):
        result, _ = self.translate([completion([translated("second", "第二"), translated("first", "第一")])], ["first", "second"])
        self.assertEqual(result, {"first": "第一", "second": "第二"})

    def test_missing_array_or_invalid_content_rejected(self):
        for response in [{}, {"choices": []}, {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}, {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}]:
            self.assertEqual(self.translate([response] * 2, ["English"])[0], {})

    def test_nonstop_or_missing_finish_reason_rejects_even_valid_json(self):
        for reason in ["length", "content_filter", "tool_calls", "insufficient_system_resource", None]:
            response = completion([translated("English")], finish_reason=reason)
            if reason is None:
                del response["choices"][0]["finish_reason"]
            with self.subTest(reason=reason):
                result, request = self.translate([response] * 2, ["English"])
                self.assertEqual(result, {})
                self.assertEqual(request.call_count, 2)

    def test_truncated_completion_can_recover_on_retry(self):
        result, request = self.translate([
            completion([translated("English", "不能采用的截断结果")], finish_reason="length"),
            completion([translated("English", "完整译文")]),
        ], ["English"])
        self.assertEqual(result, {"English": "完整译文"})
        self.assertEqual(request.call_count, 2)

    def test_prior_success_survives_truncated_retry_for_other_source(self):
        result, _ = self.translate([
            completion([translated("first", "第一译文")]),
            completion([translated("second", "不能采用的截断结果")], finish_reason="length"),
        ], ["first", "second"])
        self.assertEqual(result, {"first": "第一译文"})

    def test_translation_timeout_is_clamped_to_remaining_shared_budget(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(completion([translated("English")])).encode("utf-8")
        with patch("monitor.time.monotonic", return_value=100), patch("monitor.urllib.request.urlopen", return_value=response) as request:
            result = m.DeepSeekTranslator("fixture", m.Deadline(5)).translate_many(["English"])
        self.assertEqual(result, {"English": "中文译文"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], 5)

    def test_invalid_truncated_and_empty_json_fall_back_without_losing_notification(self):
        invalid_bodies = ["", "   ", "not json", '{"translations": [', "{}", "null"]
        responses = [
            {"choices": [{"finish_reason": "stop", "message": {"content": body}}]}
            for body in invalid_bodies
        ]
        responses.append(completion([translated("English 101")], finish_reason="length"))
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                path.write_text('{"initialized":true,"last_seen_id":"100"}', encoding="utf-8")
                report = m.RunReport()
                email = FakeEmailClient()
                with patch("monitor.request_json", return_value=response) as request, patch("monitor.time.sleep"):
                    m.run_monitor(config(), FakeXClient(page([101])), email, path, m.DeepSeekTranslator("fixture"), report)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(len(email.messages), 1)
                self.assertIn("English 101", email.messages[0]["text"])
                self.assertIn("中文翻译失败，已保留英文原文", email.messages[0]["text"])
                self.assertEqual(m.load_state(path)["last_seen_id"], "101")
                self.assertEqual((report.translation_success, report.translation_failed), (0, 1))
                self.assertIn("TRANSLATION_FALLBACK", report.status)

    def test_upper_bound_batch_retains_sources_and_has_explicit_output_budget(self):
        sources = [f"English {i}: " + "a" * (500 - len(f"English {i}: ")) for i in range(12)]
        self.assertEqual(sum(map(len, sources)), 6000)
        result, request = self.translate([completion([translated(source) for source in sources])], sources)
        self.assertEqual(len(result), 12)
        self.assertEqual(request.call_count, 1)
        payload = request.call_args.kwargs["payload"]
        entries = json.loads(payload["messages"][1]["content"])["sources"]
        self.assertEqual([entry["source"] for entry in entries], sources)
        self.assertEqual(payload["max_tokens"], 16384)
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_key_missing_is_explicit(self):
        _, text, html = m.build_digest([{"id": "101", "text": "English"}], {}, "thsottiaux")
        self.assertIn("翻译服务未配置", text); self.assertIn("翻译服务未配置", html)

    def test_main_and_context_independent_fallback_both_directions(self):
        post = {"id": "101", "text": "main", "referenced_tweets": [{"type": "quoted", "id": "9"}]}
        includes = {"tweets": [{"id": "9", "text": "parent"}]}
        for results in [{"main": "", "parent": "引用中文"}, {"main": "正文中文", "parent": ""}]:
            report = m.RunReport()
            _, text, _ = m.build_digest([post], includes, "thsottiaux", FakeBatchTranslator(results), report)
            self.assertIn("main", text); self.assertIn("parent", text)
            self.assertIn("引用中文" if results["parent"] else "正文中文", text)
            self.assertEqual(text.count("中文翻译失败，已保留英文原文"), 1)
            self.assertEqual((report.translation_success, report.translation_failed), (1, 1))

    def test_sample_and_resend_send_explicit_fallback(self):
        for operation in ["sample", "resend"]:
            email = FakeEmailClient(); report = m.RunReport(); xc = FakeXClient({"data": [{"id": "101", "text": "English"}]})
            if operation == "sample": m.send_sample(config(), xc, email, None, report)
            else: m.send_resend(config(), xc, email, None, "100", "102", report)
            self.assertEqual(len(email.messages), 1)
            self.assertIn("TRANSLATION_FALLBACK", email.messages[0]["text"])
            self.assertIn("翻译服务未配置", email.messages[0]["text"])
            self.assertFalse(report.state_written)

    def test_long_input_chunks_and_oversized_original_is_intact(self):
        sources = ["a" * 4000, "b" * 4000, "c" * 7000]
        result, request = self.translate([completion([translated(sources[0])]), completion([translated(sources[1])])], sources)
        self.assertEqual(request.call_count, 2); self.assertNotIn(sources[2], result)
        _, text, _ = m.build_digest([{"id": "101", "text": sources[2]}], {}, "thsottiaux")
        self.assertIn(sources[2], text)

    def test_mentions_urls_code_and_mixed_content_are_preserved(self):
        source = "使用 API @person https://example.com/x `run()` #tag"
        translation = "使用接口 API @person https://example.com/x `run()` #tag"
        self.assertTrue(m.valid_translation(source, translation))
        self.assertFalse(m.valid_translation(source, "使用接口"))
        self.assertFalse(m.needs_translation("中文 https://example.com @person"))

    def test_url_sentence_punctuation_and_mentions_are_not_part_of_identity(self):
        for source, translation in [
            ("Read https://example.com.", "请看 https://example.com。"),
            ("Read (https://example.com)", "请看（https://example.com）"),
            ("Read https://example.com/A_(B)", "请看 https://example.com/A_(B)"),
            ("Ask @person的话 please", "请询问 @person"),
        ]:
            with self.subTest(source=source):
                self.assertTrue(m.valid_translation(source, translation))
        self.assertFalse(m.valid_translation("Read https://example.com/A_(B)", "请看 https://example.com/A_"))

    def test_chinese_with_preserved_terms_can_be_valid_unchanged_translation(self):
        content = "中文支持 API 和 GPU"
        translator = FakeBatchTranslator({content: content})
        report = m.RunReport()
        _, text, _ = m.build_digest([{"id": "101", "text": content}], {}, "thsottiaux", translator, report)
        self.assertEqual((report.translation_success, report.translation_failed), (1, 0))
        self.assertNotIn("中文翻译失败", text)
        self.assertFalse(m.valid_translation("English only", "English only"))
        self.assertTrue(m.needs_translation("中文 and an English sentence"))

    def test_permanent_auth_failure_stops_later_batches(self):
        with patch("monitor.request_json", side_effect=m.MonitorError("HTTP_401")) as request:
            result = m.DeepSeekTranslator("fixture").translate_many(["a" * 4000, "b" * 4000])
        self.assertEqual(result, {})
        self.assertEqual(request.call_count, 1)

    def test_deepseek_retry_after_is_honored(self):
        error = m.MonitorError("HTTP_429"); error.retry_after = 5
        with patch("monitor.request_json", side_effect=[error, completion([translated("English")])]), patch("monitor.time.sleep") as sleep:
            self.assertIn("English", m.DeepSeekTranslator("fixture").translate_many(["English"]))
            sleep.assert_called_once_with(5)

    def test_deepseek_long_retry_after_falls_back_without_early_retry(self):
        error = m.MonitorError("HTTP_429"); error.retry_after = 120
        with patch("monitor.request_json", side_effect=error) as request, patch("monitor.time.sleep") as sleep:
            self.assertEqual(m.DeepSeekTranslator("fixture").translate_many(["English"]), {})
            self.assertEqual(request.call_count, 1); sleep.assert_not_called()

    def test_item_count_limit_splits_batches(self):
        sources = [f"source {i}" for i in range(13)]
        result, request = self.translate([completion([translated(s) for s in sources[:12]]), completion([translated(sources[12])])], sources)
        self.assertEqual(len(result), 13); self.assertEqual(request.call_count, 2)


class StateAndDeliveryTests(OfflineTests):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.initial = {"initialized": True, "last_seen_id": "100", "last_checked_at": datetime.now(timezone.utc).isoformat()}
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")

    def test_missing_state_requires_recovery_not_initialization(self):
        self.path.unlink()
        email = FakeEmailClient()
        with self.assertRaisesRegex(m.MonitorError, "STATE_MISSING"):
            m.run_monitor(config(), FakeXClient(page([101])), email, self.path)
        self.assertEqual(email.messages, []); self.assertFalse(self.path.exists())

    def test_corrupt_and_bad_field_types_fail(self):
        for value in ["{bad", "[]", '{"initialized":"false","last_seen_id":"100"}', '{"initialized":true,"last_seen_id":100}', '{"initialized":true,"last_seen_id":"0"}', '{"initialized":false,"last_seen_id":"100"}', '{"initialized":true,"last_seen_id":"100;cmd"}']:
            self.path.write_text(value, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(m.MonitorError): m.load_state(self.path)

    def test_no_cursor_regression_or_reinitialization(self):
        with self.assertRaisesRegex(m.MonitorError, "REGRESSION"):
            m.save_state(self.path, dict(self.initial, last_seen_id="99"))
        with self.assertRaisesRegex(m.MonitorError, "ALREADY_INITIALIZED"):
            m.run_monitor(config(), FakeXClient(page()), FakeEmailClient(), self.path, initialize=True)
        self.assertEqual(m.load_state(self.path), self.initial)

    def test_atomic_replace_failure_preserves_original(self):
        with patch("monitor.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError): m.save_state(self.path, dict(self.initial, last_seen_id="101"))
        self.assertEqual(m.load_state(self.path), self.initial)
        self.assertEqual(list(self.path.parent.glob(".tibo-state-*.tmp")), [])

    def test_atomic_fsync_failure_preserves_original(self):
        with patch("monitor.os.fsync", side_effect=OSError("fixture")):
            with self.assertRaises(OSError): m.save_state(self.path, dict(self.initial, last_seen_id="101"))
        self.assertEqual(m.load_state(self.path), self.initial)

    def test_no_new_posts_does_not_send_or_commit_half_hour_heartbeat(self):
        report = m.RunReport(); email = FakeEmailClient()
        m.run_monitor(config(), FakeXClient(page()), email, self.path, report=report)
        self.assertEqual(report.status, "NO_NEW_POSTS"); self.assertFalse(report.state_written)
        self.assertEqual(email.messages, []); self.assertEqual(m.load_state(self.path), self.initial)

    def test_daily_heartbeat_and_coverage_warning(self):
        self.initial["last_checked_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")
        report = m.RunReport()
        m.run_monitor(config(), FakeXClient(page()), FakeEmailClient(), self.path, report=report)
        self.assertTrue(report.coverage_warning); self.assertTrue(report.state_written)
        self.assertEqual(m.load_state(self.path)["last_seen_id"], "100")

    def test_fallback_success_advances_and_does_not_replay(self):
        email = FakeEmailClient(); report = m.RunReport()
        xc = FakeXClient(page([101]))
        m.run_monitor(config(), xc, email, self.path, report=report)
        self.assertEqual(m.load_state(self.path)["last_seen_id"], "101")
        self.assertTrue(report.translation_fallback)
        m.run_monitor(config(), FakeXClient(page()), email, self.path)
        self.assertEqual(len(email.messages), 1)

    def test_fallback_smtp_failure_keeps_cursor(self):
        email = Mock(); email.send.side_effect = m.MonitorError("SMTP_ERROR_OR_UNCERTAIN")
        with self.assertRaises(m.MonitorError):
            m.run_monitor(config(), FakeXClient(page([101])), email, self.path)
        self.assertEqual(m.load_state(self.path), self.initial)

    def test_two_deepseek_failures_still_send_and_advance(self):
        email = FakeEmailClient(); report = m.RunReport()
        with patch("monitor.request_json", side_effect=TimeoutError) as request, patch("monitor.time.sleep"):
            m.run_monitor(config(), FakeXClient(page([101])), email, self.path, m.DeepSeekTranslator("fixture"), report)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(m.load_state(self.path)["last_seen_id"], "101")
        self.assertEqual(len(email.messages), 1)
        self.assertIn("中文翻译失败，已保留英文原文", email.messages[0]["text"])
        self.assertIn("DEGRADED", report.status)

    def test_deepseek_and_smtp_failures_do_not_advance(self):
        email = Mock(); email.send.side_effect = m.MonitorError("SMTP_ERROR_OR_UNCERTAIN")
        with patch("monitor.request_json", side_effect=TimeoutError) as request, patch("monitor.time.sleep"):
            with self.assertRaises(m.MonitorError):
                m.run_monitor(config(), FakeXClient(page([101])), email, self.path, m.DeepSeekTranslator("fixture"))
        self.assertEqual(request.call_count, 2); self.assertEqual(m.load_state(self.path), self.initial)

    def test_page_two_failure_never_sends_or_saves(self):
        first = page([102]); first["meta"]["next_token"] = "second"
        email = FakeEmailClient()
        with patch("monitor.request_json", side_effect=[first, m.MonitorError("HTTP_503")]):
            with self.assertRaises(m.MonitorError): m.run_monitor(config(), m.XClient("fixture"), email, self.path)
        self.assertEqual(email.messages, []); self.assertEqual(m.load_state(self.path), self.initial)

    def test_accepted_then_save_failure_can_duplicate_same_batch(self):
        email = FakeEmailClient(); response = page([101])
        with patch("monitor.save_state", side_effect=OSError("fixture")):
            with self.assertRaises(OSError): m.run_monitor(config(), FakeXClient(response), email, self.path)
        m.run_monitor(config(), FakeXClient(response), email, self.path)
        self.assertEqual(len(email.messages), 2)
        self.assertEqual(email.messages[0]["idempotency_key"], email.messages[1]["idempotency_key"])

    def test_resend_rejects_invalid_ids_before_fetch(self):
        xc = Mock()
        for since, through in [("100", "100"), ("102", "101"), ("$(cmd)", "102"), ("0", "102")]:
            with self.assertRaises(m.MonitorError): m.send_resend(config(), xc, Mock(), None, since, through)
        xc.fetch_posts.assert_not_called()

    def test_missing_check_time_migrates_without_moving_cursor(self):
        self.path.write_text('{"initialized":true,"last_seen_id":"100"}', encoding="utf-8")
        m.run_monitor(config(), FakeXClient(page()), FakeEmailClient(), self.path)
        self.assertEqual(m.load_state(self.path)["last_seen_id"], "100")
        self.assertIn("last_checked_at", m.load_state(self.path))


class SMTPTests(OfflineTests):
    def send(self, smtp, report):
        client = m.SMTPEmailClient("fixture.invalid", 465, "sender@example.com", "fixture", ("a@example.com", "b@example.com"), report)
        client.send(subject="fixture", text="fixture", html_body="fixture", idempotency_key="fixture")

    def test_all_accepted(self):
        with patch("monitor.smtplib.SMTP_SSL") as factory:
            smtp = factory.return_value.__enter__.return_value; smtp.send_message.return_value = {}
            report = m.RunReport(); self.send(smtp, report)
            self.assertEqual((report.smtp, report.accepted, report.refused), ("ALL_ACCEPTED", 2, 0))

    def test_partial_refusal_keeps_monitor_state(self):
        with tempfile.TemporaryDirectory() as directory, patch("monitor.smtplib.SMTP_SSL") as factory:
            path = Path(directory) / "state.json"; path.write_text('{"initialized":true,"last_seen_id":"100"}', encoding="utf-8")
            smtp = factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {"b@example.com": (550, b"sensitive upstream text")}
            report = m.RunReport()
            client = m.SMTPEmailClient("fixture.invalid", 465, "sender@example.com", "fixture", ("a@example.com", "b@example.com"), report)
            with self.assertRaisesRegex(m.MonitorError, "^SMTP_PARTIAL_REFUSAL$"):
                m.run_monitor(config(), FakeXClient(page([101])), client, path, report=report)
            self.assertEqual(m.load_state(path)["last_seen_id"], "100")
            self.assertEqual((report.accepted, report.refused), (1, 1))

    def test_all_refused_and_disconnect_are_sanitized(self):
        for error, expected in [(smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"private")}), "SMTP_ALL_REFUSED"), (smtplib.SMTPServerDisconnected("private"), "SMTP_ERROR_OR_UNCERTAIN")]:
            with patch("monitor.smtplib.SMTP_SSL") as factory:
                smtp = factory.return_value.__enter__.return_value; smtp.send_message.side_effect = error
                with self.assertRaisesRegex(m.MonitorError, "^" + expected + "$"): self.send(smtp, m.RunReport())

    def test_quit_failure_after_acceptance_is_uncertain(self):
        with patch("monitor.smtplib.SMTP_SSL") as factory:
            smtp = factory.return_value.__enter__.return_value; smtp.send_message.return_value = {}
            factory.return_value.__exit__.side_effect = smtplib.SMTPServerDisconnected("private")
            report = m.RunReport()
            with self.assertRaisesRegex(m.MonitorError, "UNCERTAIN"): self.send(smtp, report)
            self.assertEqual(report.accepted, 2); self.assertEqual(report.smtp, "ERROR_OR_UNCERTAIN")


class CLITests(OfflineTests):
    def test_report_file_failure_does_not_change_successful_business_result(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"MONITOR_REPORT_PATH": directory}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
                report = m.RunReport(status="SENT", smtp="ALL_ACCEPTED", state_written=True)
                report.publish()
            self.assertEqual(report.status, "SENT")
            self.assertIn("OBSERVABILITY_WRITE_FAILED", output.getvalue())

    def test_real_control_flow_partial_translation_smtp_and_state(self):
        response = page([101])
        response["data"][0]["referenced_tweets"] = [{"type": "quoted", "id": "9"}]
        response["includes"] = {"tweets": [{"id": "9", "text": "parent English"}]}
        partial = completion([translated("English 101", "主帖译文"), translated("parent English", None)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"initialized":true,"last_seen_id":"100"}', encoding="utf-8")
            report = Path(directory) / "report.json"
            environment = {"MONITOR_REPORT_PATH": str(report)}
            # Windows' certificate loader needs SystemRoot even with all service secrets cleared.
            if os.name == "nt":
                environment["SystemRoot"] = os.environ["SystemRoot"]
            with patch("sys.argv", ["monitor.py", "--state", str(path)]), patch("monitor.Config.from_env", return_value=replace(config(), deepseek_api_key="fixture")), patch("monitor.request_json", side_effect=[response, partial, TimeoutError()]) as request, patch("monitor.smtplib.SMTP_SSL") as factory, patch("monitor.time.sleep"), patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
                smtp = factory.return_value.__enter__.return_value
                smtp.send_message.return_value = {}
                self.assertEqual(m.main(), 0)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(request.call_count, 3)
            self.assertEqual((data["pages"], data["fetched"], data["unique_posts"]), (1, 1, 1))
            self.assertEqual((data["translation_needed"], data["translation_success"], data["translation_failed"]), (2, 1, 1))
            self.assertEqual(data["smtp"], "ALL_ACCEPTED")
            self.assertEqual(m.load_state(path)["last_seen_id"], "101")
            message = smtp.send_message.call_args.args[0]
            text = message.get_body(preferencelist=("plain",)).get_content()
            for expected in ["主帖译文", "English 101", "parent English", "中文翻译失败，已保留英文原文"]:
                self.assertIn(expected, text)
            self.assertNotIn("parent English", output.getvalue())

    def test_real_control_flow_page_failure_is_failed_with_unchanged_state(self):
        first = page([102]); first["meta"]["next_token"] = "second"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"initialized":true,"last_seen_id":"100"}', encoding="utf-8")
            with patch("sys.argv", ["monitor.py", "--state", str(path)]), patch("monitor.Config.from_env", return_value=config()), patch("monitor.request_json", side_effect=[first, m.MonitorError("HTTP_503")]), patch("monitor.smtplib.SMTP_SSL") as smtp, patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(), 1)
            smtp.assert_not_called()
            self.assertEqual(m.load_state(path)["last_seen_id"], "100")
            self.assertIn('"failure_type": "HTTP_503"', output.getvalue())
            self.assertIn('"pages": 1', output.getvalue())

    def test_public_summary_excludes_exception_bodies(self):
        with patch("sys.argv", ["monitor.py"]), patch("monitor.Config.from_env", side_effect=ValueError("private credential")), patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(m.main(), 1)
        self.assertNotIn("private credential", output.getvalue() + error.getvalue())
        self.assertIn('"failure_type": "ValueError"', output.getvalue())

    def test_invalid_input_never_reads_config_or_calls_service(self):
        for since, through in [("$(cmd)", "102"), ("", ""), ("100", "")]:
            with self.subTest(since=since, through=through), patch("sys.argv", ["monitor.py", "--resend-since-id", since, "--resend-through-id", through]), patch("monitor.Config.from_env") as cfg, patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(), 1)
                cfg.assert_not_called()

    def test_cli_fallback_is_successful_and_summary_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"initialized":true,"last_seen_id":"100"}', encoding="utf-8")
            summary = Path(directory) / "summary.md"; report = Path(directory) / "report.json"
            with patch("sys.argv", ["monitor.py", "--state", str(path)]), patch("monitor.Config.from_env", return_value=config()), patch("monitor.XClient", return_value=FakeXClient(page([101]))), patch("monitor.SMTPEmailClient", return_value=FakeEmailClient()), patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary), "MONITOR_REPORT_PATH": str(report)}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(), 0)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["translation_failed"], 1)
            self.assertTrue(data["state_written"])
            self.assertIn("TRANSLATION_FALLBACK", summary.read_text(encoding="utf-8"))

    def test_test_email_does_not_fetch_or_translate_or_load_state(self):
        with patch("sys.argv", ["monitor.py", "--test-email"]), patch("monitor.Config.from_env", return_value=config()), patch("monitor.XClient") as xc, patch("monitor.DeepSeekTranslator") as translator, patch("monitor.SMTPEmailClient") as email, patch("monitor.load_state") as state, patch.dict(os.environ, {"THIRD_ALERT_EMAIL": "fixture@example.com"}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(), 0)
            xc.return_value.fetch_posts.assert_not_called()
            translator.assert_not_called(); state.assert_not_called()
            email.return_value.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
