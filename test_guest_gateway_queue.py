import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from guest_gateway import Config, GuestGateway, main


class FakeGateway(GuestGateway):
    def __init__(self):
        cfg = Config(
            bot_token="token",
            owner_id=123456789,
            hermes_url="http://127.0.0.1:1/v1/chat/completions",
            hermes_key="key",
            model="test-model",
            worker_count=2,
            placeholder_enabled=False,
            hermes_use_runs=False,
        )
        self.tmp_path = Path(tempfile.mkdtemp())
        self._tmp_finalizer = weakref.finalize(self, shutil.rmtree, self.tmp_path, ignore_errors=True)
        super().__init__(cfg, state_path=self.tmp_path / "state.json")
        self.calls = []
        self.hermes_calls = []

    def tg(self, method, payload, timeout=60):
        self.calls.append((method, payload))
        if method == "answerGuestQuery":
            return {"ok": True, "result": {"inline_message_id": "sent-final"}}
        return {"ok": True, "result": []}

    def call_hermes(self, message):
        self.hermes_calls.append(message.get("text"))
        time.sleep(0.01)
        return f"final: {message.get('text')}"

class MediaGateway(GuestGateway):
    def __init__(self):
        self.tmp_path = Path(tempfile.mkdtemp())
        self._tmp_finalizer = weakref.finalize(self, shutil.rmtree, self.tmp_path, ignore_errors=True)
        cfg = Config(
            bot_token="secret-token",
            owner_id=123456789,
            hermes_url="http://127.0.0.1:1/v1/chat/completions",
            hermes_key="key",
            model="test-model",
            media_cache_dir=self.tmp_path / "media",
            media_max_bytes=100,
            hermes_use_runs=False,
        )
        super().__init__(cfg, state_path=self.tmp_path / "state.json")
        self.calls = []

    def tg(self, method, payload, timeout=60):
        self.calls.append((method, payload))
        if method == "getFile":
            return {"ok": True, "result": {"file_path": f"photos/{payload['file_id']}.jpg", "file_size": 4}}
        if method == "answerGuestQuery":
            return {"ok": True, "result": {"inline_message_id": "sent-final"}}
        return {"ok": True, "result": []}

    def tg_upload(self, method, fields, file_field, path, timeout=60):
        self.calls.append((method, {"fields": fields, "file_field": file_field, "path": str(path)}))
        result = {"message_id": 777}
        if method == "sendPhoto":
            result["photo"] = [{"file_id": "staged-photo", "width": 100, "height": 100}]
        elif method == "sendAnimation":
            result["animation"] = {"file_id": "staged-animation"}
        elif method == "sendVideo":
            result["video"] = {"file_id": "staged-video"}
        elif method == "sendAudio":
            result["audio"] = {"file_id": "staged-audio"}
        elif method == "sendVoice":
            result["voice"] = {"file_id": "staged-voice"}
        elif method == "sendDocument":
            result["document"] = {"file_id": "staged-document"}
        return {"ok": True, "result": result}

def rich_or_text(content):
    if "message_text" in content:
        return content["message_text"]
    rich = content["rich_message"]
    if rich.get("markdown") or rich.get("html"):
        return rich.get("markdown") or rich.get("html") or ""

    def plain(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(plain(item) for item in value)
        if not isinstance(value, dict):
            return ""
        if value.get("type") == "custom_emoji":
            return value.get("alternative_text") or ""
        if value.get("type") == "mathematical_expression":
            return value.get("expression") or ""
        if value.get("items") is not None:
            return "\n".join(plain(item.get("blocks") or []) for item in value["items"])
        if value.get("blocks") is not None:
            return "\n".join(plain(item) for item in value["blocks"])
        return plain(value.get("text") or value.get("caption") or "")

    return plain(rich.get("blocks") or [])


def update(update_id, query_id, text, caller_id=123456789):
    return {
        "update_id": update_id,
        "guest_message": {
            "message_id": update_id + 1000,
            "guest_query_id": query_id,
            "from": {"id": caller_id, "username": "guest_owner"},
            "chat": {"id": -100123, "type": "supergroup", "title": "test"},
            "text": text,
        },
    }


class GuestQueueTests(unittest.TestCase):
    def test_once_mode_starts_workers_waits_for_jobs_and_stops_cleanly(self):
        class Jobs:
            def __init__(self):
                self.joined = False

            def join(self):
                self.joined = True

        class OnceGateway:
            instance = None

            def __init__(self, _cfg, state_path=None):
                type(self).instance = self
                self.jobs = Jobs()
                self.started = False
                self.stopped = False
                self.handled = []
                self.saved_offset = None
                self.running = True

            def start_worker(self):
                self.started = True

            def stop_worker(self):
                self.stopped = True

            def get_updates(self):
                return [{"update_id": 9, "guest_message": {"guest_query_id": "q9"}}]

            def handle_guest(self, update, dry_run=False):
                self.handled.append((update["update_id"], dry_run))

            def _save_offset(self, offset):
                self.saved_offset = offset

        cfg = Config(
            bot_token="token",
            owner_id=123456789,
            hermes_url="http://127.0.0.1:1",
            hermes_key="key",
            model="test",
        )
        with (
            patch.object(sys, "argv", ["guest_gateway.py", "--once"]),
            patch("guest_gateway.load_dotenv"),
            patch("guest_gateway.Config.from_env", return_value=cfg),
            patch("guest_gateway.GuestGateway", OnceGateway),
            patch("guest_gateway.signal.signal"),
        ):
            self.assertEqual(main(), 0)

        gateway = OnceGateway.instance
        self.assertTrue(gateway.started)
        self.assertEqual(gateway.handled, [(9, False)])
        self.assertEqual(gateway.saved_offset, 10)
        self.assertTrue(gateway.jobs.joined)
        self.assertTrue(gateway.stopped)

    def test_utf16_entity_offsets_handle_emoji_before_bot_mention(self):
        gw = FakeGateway()
        text = "🙂 @guest_bot проверь"
        message = {
            "text": text,
            "entities": [
                {
                    "type": "mention",
                    "offset": 3,
                    "length": len("@guest_bot"),
                }
            ],
        }

        self.assertTrue(gw._has_explicit_mention(message))

    def test_queued_job_and_offset_are_persisted_before_processing(self):
        gw = FakeGateway()
        gw.handle_guest(update(7, "q7", "durable"))

        state = json.loads(gw.state_path.read_text(encoding="utf-8"))
        restored = GuestGateway(gw.cfg, state_path=gw.state_path)

        self.assertEqual(state["offset"], 8)
        self.assertIn("update:7", state["pending_jobs"])
        self.assertEqual(restored.jobs.qsize(), 1)
        self.assertEqual(restored.offset, 8)

    def test_burst_is_queued_without_placeholder_or_hermes_blocking(self):
        gw = FakeGateway()
        gw.handle_guest(update(1, "q1", "first"))
        gw.handle_guest(update(2, "q2", "second"))

        self.assertEqual([c for c in gw.calls if c[0] == "answerGuestQuery"], [])
        self.assertEqual(gw.jobs.qsize(), 2)
        self.assertEqual(gw.hermes_calls, [])

    def test_worker_sends_final_guest_answers(self):
        gw = FakeGateway()
        gw.handle_guest(update(1, "q1", "first"))
        gw.handle_guest(update(2, "q2", "second"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        answers = [payload for method, payload in gw.calls if method == "sendRichMessage"]
        self.assertEqual([a["reply_parameters"]["message_id"] for a in answers], [1001, 1002])
        self.assertEqual(
            [rich_or_text({"rich_message": a["rich_message"]}) for a in answers],
            ["final: first", "final: second"],
        )
        self.assertEqual([c for c in gw.calls if c[0] == "editMessageText"], [])
        self.assertEqual(gw.hermes_calls, ["first", "second"])

    def test_worker_edits_placeholder_when_enabled(self):
        gw = FakeGateway()
        gw.cfg.placeholder_enabled = True
        gw.cfg.placeholder_text = "working"
        gw.handle_guest(update(1, "q1", "first"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        answers = [payload for method, payload in gw.calls if method == "answerGuestQuery"]
        edits = [payload for method, payload in gw.calls if method == "editMessageText"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(rich_or_text(answers[0]["result"]["input_message_content"]), "working")
        self.assertEqual(
            edits,
            [
                {
                    "inline_message_id": "sent-final",
                    "rich_message": {"blocks": [{"type": "paragraph", "text": "final: first"}]},
                }
            ],
        )
        self.assertEqual([method for method, _payload in gw.calls if method in {"sendRichMessage", "sendMessage"}], [])
        self.assertEqual(gw.hermes_calls, ["first"])

    def test_worker_preserves_explicit_new_message_then_edit_mode(self):
        gw = FakeGateway()
        gw.cfg.placeholder_enabled = True
        gw.cfg.final_delivery_mode = "new_message_then_edit"
        gw.handle_guest(update(1, "q1", "first"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        sends = [payload for method, payload in gw.calls if method == "sendRichMessage"]
        edits = [payload for method, payload in gw.calls if method == "editMessageText"]
        self.assertEqual(
            sends[0]["rich_message"],
            {"blocks": [{"type": "paragraph", "text": "final: first"}]},
        )
        self.assertEqual(
            edits,
            [
                {
                    "inline_message_id": "sent-final",
                    "rich_message": {"blocks": [{"type": "paragraph", "text": "Готово."}]},
                }
            ],
        )

    def test_worker_sends_owner_fallback_when_final_delivery_fails(self):
        class FinalDeliveryFailGateway(FakeGateway):
            def tg(self, method, payload, timeout=60):
                self.calls.append((method, payload))
                if method == "answerGuestQuery":
                    return {"ok": True, "result": {"inline_message_id": "sent-final"}}
                if method == "editMessageText":
                    raise RuntimeError("Bad Request: query is too old and response timeout expired or query ID is invalid")
                return {"ok": True, "result": []}

        gw = FinalDeliveryFailGateway()
        gw.cfg.placeholder_enabled = True
        gw.cfg.final_delivery_mode = "edit"
        gw.handle_guest(update(1, "q1", "first"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        owner_messages = [payload for method, payload in gw.calls if method == "sendMessage"]
        self.assertEqual(len(owner_messages), 1)
        self.assertEqual(owner_messages[0]["chat_id"], 123456789)
        self.assertIn("Не смог отправить ответ в guest-чат", owner_messages[0]["text"])
        self.assertIn("final: first", owner_messages[0]["text"])

    def test_worker_falls_back_to_placeholder_edit_when_new_chat_message_fails(self):
        class SendFailGateway(FakeGateway):
            def tg(self, method, payload, timeout=60):
                self.calls.append((method, payload))
                if method == "answerGuestQuery":
                    return {"ok": True, "result": {"inline_message_id": "sent-final"}}
                if method in {"sendRichMessage", "sendMessage"}:
                    raise RuntimeError("Forbidden: bot is not a member of the chat")
                return {"ok": True, "result": []}

        gw = SendFailGateway()
        gw.cfg.placeholder_enabled = True
        gw.cfg.final_delivery_mode = "new_message_then_edit"
        gw.handle_guest(update(1, "q1", "first"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        edits = [payload for method, payload in gw.calls if method == "editMessageText"]
        self.assertEqual(
            edits,
            [
                {
                    "inline_message_id": "sent-final",
                    "rich_message": {"blocks": [{"type": "paragraph", "text": "final: first"}]},
                }
            ],
        )

    def test_reactions_mark_accept_and_success(self):
        gw = FakeGateway()
        gw.handle_guest(update(1, "q1", "first"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        reaction_payloads = [payload for method, payload in gw.calls if method == "setMessageReaction"]
        self.assertEqual([p["reaction"][0]["emoji"] for p in reaction_payloads], ["👀", "👍"])
        self.assertEqual(reaction_payloads[0]["message_id"], 1001)
        self.assertTrue(reaction_payloads[1]["is_big"])

    def test_reactions_mark_failure_when_hermes_fails(self):
        class FailingGateway(FakeGateway):
            def call_hermes(self, message):
                raise RuntimeError("boom")

        gw = FailingGateway()
        gw.handle_guest(update(1, "q1", "first"))
        gw.running = False
        gw.jobs.put(None)
        gw._worker_loop()

        reaction_payloads = [payload for method, payload in gw.calls if method == "setMessageReaction"]
        self.assertEqual([p["reaction"][0]["emoji"] for p in reaction_payloads], ["👀", "👎"])
        answers = [payload for method, payload in gw.calls if method == "sendRichMessage"]
        self.assertIn("Сломалась", rich_or_text({"rich_message": answers[0]["rich_message"]}))

    def test_reaction_failure_does_not_block_queueing(self):
        class ReactionFailGateway(FakeGateway):
            def tg(self, method, payload, timeout=60):
                if method == "setMessageReaction":
                    self.calls.append((method, payload))
                    raise RuntimeError("reactions unavailable")
                return super().tg(method, payload, timeout=timeout)

        gw = ReactionFailGateway()
        gw.handle_guest(update(1, "q1", "first"))

        self.assertEqual(gw.jobs.qsize(), 1)
        self.assertEqual([method for method, _payload in gw.calls if method == "setMessageReaction"], ["setMessageReaction"])

    def test_hermes_timeout_is_configurable(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "HERMES_TIMEOUT": "420",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.hermes_timeout, 420)

    def test_hermes_timeout_has_safe_minimum(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "HERMES_TIMEOUT": "5",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.hermes_timeout, 30)

    def test_owner_id_is_required_and_never_has_a_fallback(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "HERMES_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(SystemExit, "GUEST_OWNER_ID"):
                Config.from_env()

    def test_owner_id_must_be_a_positive_integer(self):
        for value in ("owner", "0", "-1"):
            with self.subTest(value=value):
                env = {
                    "GUEST_BOT_TOKEN": "token",
                    "GUEST_OWNER_ID": value,
                    "HERMES_API_KEY": "key",
                }
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(SystemExit, "positive integer"):
                        Config.from_env()

    def test_hermes_run_env_knobs_are_configurable(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "HERMES_USE_RUNS": "false",
            "HERMES_RUN_START_TIMEOUT": "7",
            "HERMES_POLL_TIMEOUT": "8",
            "HERMES_POLL_INTERVAL": "0.25",
            "GUEST_HERMES_MAX_RUNTIME": "99",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertFalse(cfg.hermes_use_runs)
        self.assertEqual(cfg.hermes_run_start_timeout, 7)
        self.assertEqual(cfg.hermes_poll_timeout, 8)
        self.assertEqual(cfg.hermes_poll_interval, 0.5)
        self.assertEqual(cfg.hermes_max_runtime, 99)

    def test_compose_runtime_limit_allows_long_tool_runs(self):
        compose = (Path(__file__).parent / "compose.yaml").read_text()
        self.assertIn('GUEST_HERMES_MAX_RUNTIME: "${GUEST_HERMES_MAX_RUNTIME:-900}"', compose)
        self.assertNotIn('GUEST_HERMES_MAX_RUNTIME: "300"', compose)

    def test_call_hermes_uses_runs_and_polls_to_completion(self):
        gw = FakeGateway()
        gw.cfg.hermes_use_runs = True
        gw.cfg.hermes_poll_interval = 0.01
        calls = []

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
            if url.endswith("/v1/runs"):
                return {"run_id": "run_1", "status": "started"}
            if url.endswith("/v1/runs/run_1"):
                if len([c for c in calls if c["url"].endswith("/v1/runs/run_1")]) == 1:
                    return {"run_id": "run_1", "status": "running"}
                return {"run_id": "run_1", "status": "completed", "output": "done"}
            raise AssertionError(f"unexpected url {url}")

        message = update(1, "q1", "slow task")["guest_message"]
        with patch("guest_gateway.http_json", fake_http_json):
            self.assertEqual(GuestGateway.call_hermes(gw, message), "done")

        self.assertEqual(calls[0]["url"], "http://127.0.0.1:1/v1/runs")
        self.assertEqual(calls[0]["payload"]["input"].splitlines()[-1], "message: slow task")
        self.assertNotIn("session_id", calls[0]["payload"])
        self.assertEqual(calls[0]["timeout"], 30)
        self.assertEqual(calls[1]["timeout"], 20)

    def test_runs_send_the_stable_session_id_for_a_reply_context(self):
        gw = FakeGateway()
        gw.cfg.hermes_use_runs = True
        gw.cfg.hermes_poll_interval = 0.01
        calls = []

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            calls.append({"url": url, "payload": payload})
            if url.endswith("/v1/runs"):
                return {"run_id": "run_1", "status": "started"}
            if url.endswith("/v1/runs/run_1"):
                return {"run_id": "run_1", "status": "completed", "output": "done"}
            raise AssertionError(f"unexpected url {url}")

        message = update(1, "q1", "remember this")["guest_message"]
        message["_guest_context"] = {"mode": "followup", "thread_id": "thread-a", "uses_prior_context": True}
        with patch("guest_gateway.http_json", fake_http_json):
            self.assertEqual(GuestGateway.call_hermes(gw, message), "done")

        self.assertEqual(calls[0]["payload"]["session_id"], gw._guest_session_id(message))

    def test_chat_completions_keep_reply_history_and_reset_new_sessions(self):
        gw = FakeGateway()
        gw.cfg.hermes_use_runs = False
        calls = []
        responses = iter(["first answer", "followup answer", "fresh answer"])

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            calls.append(payload)
            return {"choices": [{"message": {"content": next(responses)}}]}

        first = update(1, "q1", "first question")["guest_message"]
        first["_guest_context"] = {"mode": "standalone", "thread_id": "thread-a", "uses_prior_context": False}
        followup = update(2, "q2", "followup question")["guest_message"]
        followup["_guest_context"] = {"mode": "followup", "thread_id": "thread-a", "uses_prior_context": True}
        fresh = update(3, "q3", "fresh question")["guest_message"]
        fresh["_guest_context"] = {"mode": "standalone", "thread_id": "thread-b", "uses_prior_context": False}

        with patch("guest_gateway.http_json", fake_http_json):
            self.assertEqual(GuestGateway.call_hermes(gw, first), "first answer")
            self.assertEqual(GuestGateway.call_hermes(gw, followup), "followup answer")
            self.assertEqual(GuestGateway.call_hermes(gw, fresh), "fresh answer")

        first_contents = "\n".join(item["content"] for item in calls[0]["messages"])
        followup_contents = "\n".join(item["content"] for item in calls[1]["messages"])
        fresh_contents = "\n".join(item["content"] for item in calls[2]["messages"])
        self.assertIn("first question", first_contents)
        self.assertIn("first question", followup_contents)
        self.assertIn("first answer", followup_contents)
        self.assertIn("followup question", followup_contents)
        self.assertNotIn("first question", fresh_contents)
        self.assertNotIn("first answer", fresh_contents)
        self.assertNotIn("session_id", calls[1])
        self.assertNotIn("chat_completion_sessions", gw._load_state())

    def test_run_poll_timeout_is_transient_not_final_failure(self):
        gw = FakeGateway()
        gw.cfg.hermes_use_runs = True
        gw.cfg.hermes_poll_interval = 0.01
        poll_count = 0

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            nonlocal poll_count
            if url.endswith("/v1/runs"):
                return {"run_id": "run_1", "status": "started"}
            if url.endswith("/v1/runs/run_1"):
                poll_count += 1
                if poll_count == 1:
                    raise RuntimeError("timed out")
                return {"run_id": "run_1", "status": "completed", "output": "ok"}
            raise AssertionError(f"unexpected url {url}")

        with patch("guest_gateway.http_json", fake_http_json):
            self.assertEqual(GuestGateway.call_hermes(gw, update(1, "q1", "slow")["guest_message"]), "ok")
        self.assertEqual(poll_count, 2)

    def test_run_failed_raises_meaningful_error(self):
        gw = FakeGateway()
        gw.cfg.hermes_use_runs = True
        gw.cfg.hermes_poll_interval = 0.01

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            if url.endswith("/v1/runs"):
                return {"run_id": "run_1", "status": "started"}
            if url.endswith("/v1/runs/run_1"):
                return {"run_id": "run_1", "status": "failed", "error": "model auth failed"}
            raise AssertionError(f"unexpected url {url}")

        with patch("guest_gateway.http_json", fake_http_json):
            with self.assertRaisesRegex(RuntimeError, "model auth failed"):
                GuestGateway.call_hermes(gw, update(1, "q1", "slow")["guest_message"])

    def test_media_env_knobs_are_configurable(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "GUEST_MEDIA_ENABLED": "false",
            "GUEST_MEDIA_CACHE_DIR": "/tmp/guest-media-test",
            "GUEST_MEDIA_HOST_DIR": "/host/guest-media-test",
            "GUEST_MEDIA_MAX_BYTES": "123",
            "GUEST_REACTIONS_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertFalse(cfg.media_enabled)
        self.assertEqual(cfg.media_cache_dir, Path("/tmp/guest-media-test"))
        self.assertEqual(cfg.media_host_dir, Path("/host/guest-media-test"))
        self.assertEqual(cfg.media_max_bytes, 123)
        self.assertFalse(cfg.reactions_enabled)

    def test_rich_and_text_limits_are_configurable_and_capped(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "GUEST_RICH_MAX_REPLY_CHARS": "99999",
            "GUEST_TEXT_MAX_REPLY_CHARS": "99999",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()

        self.assertEqual(cfg.rich_max_reply_chars, 32768)
        self.assertEqual(cfg.max_reply_chars, 4096)

    def test_compose_uses_directory_mounted_atomic_state_path(self):
        compose = (Path(__file__).parent / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("GUEST_STATE_PATH: /app/runtime/state.json", compose)
        self.assertNotIn("./state.json:/app/state.json", compose)

    def test_placeholder_env_knobs_are_configurable(self):
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "GUEST_PLACEHOLDER_ENABLED": "false",
            "GUEST_PLACEHOLDER_TEXT": " ",
            "GUEST_PLACEHOLDER_CUSTOM_EMOJI_ID": "1234567890123456789",
            "GUEST_PLACEHOLDER_CUSTOM_EMOJI_ALT": "🤔",
            "GUEST_FINAL_DELIVERY_MODE": "bogus",
            "GUEST_PLACEHOLDER_DONE_TEXT": " ",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertFalse(cfg.placeholder_enabled)
        self.assertEqual(cfg.placeholder_text, "Думаю…")
        self.assertEqual(cfg.placeholder_custom_emoji_id, "1234567890123456789")
        self.assertEqual(cfg.placeholder_custom_emoji_alt, "🤔")
        self.assertEqual(cfg.final_delivery_mode, "edit")
        self.assertEqual(cfg.placeholder_done_text, "Готово.")

    def test_placeholder_defaults_use_inline_edit_delivery(self):
        cfg = Config(
            bot_token="token",
            owner_id=123456789,
            hermes_url="http://127.0.0.1:1/v1/chat/completions",
            hermes_key="key",
            model="test-model",
        )
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True):
            env_cfg = Config.from_env()

        self.assertEqual(cfg.final_delivery_mode, "edit")
        self.assertEqual(env_cfg.final_delivery_mode, "edit")
        self.assertNotEqual(cfg.placeholder_done_text, "↴")
        self.assertNotEqual(env_cfg.placeholder_done_text, "↴")

    def test_shipped_config_has_no_bare_arrow_done_marker(self):
        root = Path(__file__).parent
        for filename in ("compose.yaml", ".env.example", "README.md"):
            with self.subTest(filename=filename):
                self.assertNotIn("↴", (root / filename).read_text())

    def test_prompt_is_generic_transport_context_without_harness_specific_skills(self):
        gw = FakeGateway()
        captured = {}

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            captured.update({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
            return {"choices": [{"message": {"content": "ok"}}]}

        message = {
            "from": {"id": 123456789, "username": "guest_owner"},
            "chat": {"type": "supergroup", "title": "agents"},
            "text": "найди актуальное место рядом",
            "reply_to_message": {"text": "Зябликово"},
        }
        with patch("guest_gateway.http_json", fake_http_json):
            self.assertEqual(GuestGateway.call_hermes(gw, message), "ok")

        messages = captured["payload"]["messages"]
        combined = "\n".join(m["content"] for m in messages)
        combined_lower = combined.lower()
        self.assertIn("active harness profile", combined_lower)
        self.assertIn("reply_to_message", combined)
        self.assertIn("uses_prior_context", combined)
        self.assertNotIn("browser skill", combined_lower)
        self.assertNotIn("custom profile instruction", combined_lower)
        self.assertIn("Зябликово", combined)

    def test_direct_photo_without_reply_is_sent_to_hermes_as_visible_media_path(self):
        gw = MediaGateway()
        gw.cfg.media_host_dir = gw.tmp_path / "host-media"
        captured = {}

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            captured.update({"payload": payload})
            return {"choices": [{"message": {"content": "ok"}}]}

        def fake_http_bytes(url, timeout=60, max_bytes=100):
            return b"jpeg"

        message = {
            "from": {"id": 123456789, "username": "guest_owner"},
            "chat": {"type": "supergroup", "title": "agents"},
            "caption": "что на фото?",
            "photo": [{"file_id": "direct-large", "width": 1000, "height": 800, "file_size": 4}],
        }
        with patch("guest_gateway.http_json", fake_http_json), patch("guest_gateway.http_bytes", fake_http_bytes):
            self.assertEqual(GuestGateway.call_hermes(gw, message), "ok")

        combined = "\n".join(m["content"] for m in captured["payload"]["messages"])
        self.assertIn('"message": {', combined)
        self.assertIn('"message_kinds": ["photo"]', combined)
        self.assertIn("direct-large", combined)
        self.assertIn(str(gw.cfg.media_host_dir), combined)
        self.assertIn("media_context describes downloaded files", combined)
        self.assertNotIn('"reply_to_message"', combined)

    def test_media_context_downloads_largest_photo_without_leaking_file_url(self):
        gw = MediaGateway()
        gw.cfg.media_host_dir = gw.tmp_path / "host-media"
        captured = {}
        downloaded_urls = []

        def fake_http_json(url, payload=None, headers=None, timeout=60):
            captured.update({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
            return {"choices": [{"message": {"content": "ok"}}]}

        def fake_http_bytes(url, timeout=60, max_bytes=100):
            downloaded_urls.append(url)
            return b"jpeg"

        message = {
            "from": {"id": 123456789, "username": "guest_owner"},
            "chat": {"type": "supergroup", "title": "agents"},
            "caption": "что на фото?",
            "photo": [
                {"file_id": "small", "width": 90, "height": 90, "file_size": 3},
                {"file_id": "large", "width": 900, "height": 900, "file_size": 4},
            ],
            "reply_to_message": {
                "text": "референс",
                "sticker": {
                    "file_id": "sticker-file",
                    "emoji": "ok",
                    "set_name": "set",
                    "width": 512,
                    "height": 512,
                    "file_size": 4,
                    "thumbnail": {"file_id": "sticker-thumb", "width": 128, "height": 128, "file_size": 4},
                },
            },
        }

        with patch("guest_gateway.http_json", fake_http_json), patch("guest_gateway.http_bytes", fake_http_bytes):
            self.assertEqual(GuestGateway.call_hermes(gw, message), "ok")

        combined = "\n".join(m["content"] for m in captured["payload"]["messages"])
        self.assertIn('"message_kinds": ["photo"]', combined)
        self.assertIn('"message_kinds": ["sticker"]', combined)
        self.assertIn('"local_path":', combined)
        self.assertIn('"sandbox_path":', combined)
        self.assertIn(str(gw.cfg.media_host_dir), combined)
        self.assertIn(str(gw.cfg.media_cache_dir), combined)
        self.assertIn("large", combined)
        self.assertNotIn("secret-token", combined)
        self.assertNotIn("api.telegram.org/file", combined)
        self.assertTrue(downloaded_urls)
        self.assertIn("secret-token", downloaded_urls[0])

    def test_media_context_respects_size_cap_and_keeps_metadata(self):
        gw = MediaGateway()
        gw.cfg.media_max_bytes = 2

        message = {
            "text": "проверь файл",
            "document": {
                "file_id": "doc-file",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 10,
            },
            "voice": {
                "file_id": "voice-file",
                "mime_type": "audio/ogg",
                "duration": 7,
                "file_size": 10,
            },
        }

        with patch("guest_gateway.http_bytes") as fake_http_bytes:
            ctx = gw.media_context(message)

        media = ctx["message"]["media"]
        self.assertEqual([m["kind"] for m in media], ["voice", "document"])
        self.assertEqual([m["download"] for m in media], ["skipped", "skipped"])
        self.assertEqual([m["reason"] for m in media], ["file_size over cap", "file_size over cap"])
        fake_http_bytes.assert_not_called()

    def test_media_context_downloads_actual_video_not_only_thumbnail(self):
        gw = MediaGateway()
        message = {
            "video": {
                "file_id": "video-file",
                "mime_type": "video/mp4",
                "duration": 3,
                "width": 640,
                "height": 360,
                "file_size": 4,
            }
        }

        with patch("guest_gateway.http_bytes", return_value=b"mp4") as fake_http_bytes:
            ctx = gw.media_context(message)

        video = ctx["message"]["media"][0]
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["download"], "ok")
        self.assertIn("local_path", video)
        fake_http_bytes.assert_called_once()

    def test_media_context_extracts_and_downloads_rich_media_blocks(self):
        gw = MediaGateway()
        message = {
            "rich_message": {
                "blocks": [
                    {"type": "paragraph", "text": "Смотри"},
                    {
                        "type": "photo",
                        "photo": {
                            "file_id": "rich-photo",
                            "width": 800,
                            "height": 600,
                            "file_size": 4,
                        },
                        "caption": {"text": "Пример"},
                    },
                ]
            }
        }

        with patch("guest_gateway.http_bytes", return_value=b"jpeg"):
            ctx = gw.media_context(message)

        self.assertIn("rich_message", ctx["message"]["message_kinds"])
        photo = ctx["message"]["media"][0]
        self.assertEqual(photo["kind"], "photo")
        self.assertEqual(photo["file_id"], "rich-photo")
        self.assertEqual(photo["caption"], "Пример")
        self.assertEqual(photo["download"], "ok")

    def test_gif_and_ogg_use_animation_and_voice_uploads(self):
        gw = MediaGateway()

        self.assertEqual(gw._owner_media_method(Path("thinking.gif")), ("sendAnimation", "animation"))
        self.assertEqual(gw._owner_media_method(Path("answer.ogg")), ("sendVoice", "voice"))

    def test_public_ogg_url_uses_inline_voice_result(self):
        gw = MediaGateway()

        result = gw._public_media_result("https://example.com/answer.ogg", "id-1")

        self.assertEqual(result["type"], "voice")
        self.assertEqual(result["voice_url"], "https://example.com/answer.ogg")

    def test_answer_guest_builds_cached_media_result_when_reply_text_empty(self):
        gw = MediaGateway()
        gw.answer_guest(
            "q1",
            "",
            {
                "photo": [
                    {"file_id": "small", "width": 1, "height": 1},
                    {"file_id": "large", "width": 10, "height": 10},
                ]
            },
        )

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        self.assertEqual(payload["result"]["type"], "photo")
        self.assertEqual(payload["result"]["photo_file_id"], "large")

    def test_answer_guest_uses_text_article_fallback(self):
        gw = MediaGateway()
        gw.answer_guest("q1", "текст", {"document": {"file_id": "doc-file", "file_name": "x.pdf"}})

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        self.assertEqual(payload["result"]["type"], "article")
        self.assertEqual(rich_or_text(payload["result"]["input_message_content"]), "текст")

    def test_answer_guest_builds_public_media_result_for_plain_url(self):
        gw = MediaGateway()
        gw.answer_guest("q1", "https://example.com/image.jpg")

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        self.assertEqual(payload["result"]["type"], "photo")
        self.assertEqual(payload["result"]["photo_url"], "https://example.com/image.jpg")

    def test_answer_guest_rejects_public_bot_file_urls(self):
        gw = MediaGateway()
        gw.answer_guest("q1", "https://api.telegram.org/file/botsecret-token/photos/x.jpg")

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        self.assertEqual(payload["result"]["type"], "article")

    def test_answer_guest_does_not_leak_local_markdown_image_path(self):
        gw = MediaGateway()
        gw.answer_guest("q1", "![котик](/Users/tester/.guest-agent/cache/images/cat.png)")

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        text = rich_or_text(payload["result"]["input_message_content"])
        self.assertEqual(payload["result"]["type"], "article")
        self.assertIn("Не удалось безопасно загрузить локальный файл", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn(".hermes", text)
        self.assertEqual([m for m, _p in gw.calls if m == "sendPhoto"], [])

    def test_answer_guest_sends_allowed_local_image_to_owner_dm(self):
        gw = MediaGateway()
        local = gw.cfg.media_cache_dir / "cat.jpg"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"jpeg")
        gw.answer_guest("q1", f"![котик]({local})")

        upload = [p for method, p in gw.calls if method == "sendPhoto"][0]
        self.assertEqual(upload["fields"]["chat_id"], "123456789")
        self.assertEqual(upload["file_field"], "photo")
        self.assertEqual(upload["path"], str(local))
        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        text = rich_or_text(payload["result"]["input_message_content"])
        self.assertIn("котик", text)
        self.assertNotIn(str(local), text)
        rich = payload["result"]["input_message_content"]["rich_message"]
        self.assertEqual(rich["blocks"][-1]["type"], "photo")
        self.assertEqual(rich["blocks"][-1]["photo"]["media"], "staged-photo")

    def test_answer_guest_maps_allowed_host_harness_media_path_into_container_cache(self):
        gw = MediaGateway()
        host_media_dir = gw.tmp_path / "host-harness-media"
        gw.cfg.media_host_dir = host_media_dir
        local = gw.cfg.media_cache_dir / "cat.jpg"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"jpeg")

        gw.answer_guest("q1", f"MEDIA:{host_media_dir / local.name}")

        upload = [p for method, p in gw.calls if method == "sendPhoto"][0]
        self.assertEqual(upload["path"], str(local))

    def test_answer_guest_does_not_map_host_path_outside_configured_media_directory(self):
        gw = MediaGateway()
        gw.cfg.media_host_dir = gw.tmp_path / "host-harness-media"
        outside = gw.tmp_path / "outside.jpg"
        outside.write_bytes(b"jpeg")

        gw.answer_guest("q1", f"MEDIA:{outside}")

        self.assertEqual([method for method, _payload in gw.calls if method == "sendPhoto"], [])

    def test_answer_guest_refuses_local_file_outside_allowed_dirs(self):
        gw = MediaGateway()
        outside = gw.tmp_path / "outside.jpg"
        outside.write_bytes(b"jpeg")
        gw.answer_guest("q1", f"MEDIA:{outside}")

        self.assertEqual([m for m, _p in gw.calls if m == "sendPhoto"], [])
        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        text = rich_or_text(payload["result"]["input_message_content"])
        self.assertIn("Не удалось безопасно загрузить локальный файл", text)
        self.assertNotIn(str(outside), text)

    def test_answer_guest_redacts_local_paths_inside_text(self):
        gw = MediaGateway()
        gw.answer_guest("q1", "готово: MEDIA:/tmp/private-cat.png и /Users/tester/secret.png")

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        text = rich_or_text(payload["result"]["input_message_content"])
        self.assertIn("[локальный файл скрыт]", text)
        self.assertNotIn("/tmp/private-cat.png", text)
        self.assertNotIn("/Users/tester", text)

    def test_outbound_sanitizer_covers_file_uris_and_sensitive_system_roots(self):
        gw = MediaGateway()
        for local_path in (
            "file:///etc/hosts",
            "MEDIA:/opt/guest-agent/secret.txt",
            "/usr/local/share/private.json",
            "/Applications/Private.app/config.json",
            r"C:\\Users\\owner\\secret.txt",
        ):
            with self.subTest(local_path=local_path):
                sanitized = gw._sanitize_outbound_text(f"result: {local_path}")
                self.assertIn("[локальный файл скрыт]", sanitized)
                self.assertNotIn(local_path, sanitized)

    def test_docker_compose_sandbox_defaults_exist(self):
        root = Path(__file__).resolve().parent
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("read_only: true", compose)
        self.assertIn("/sandbox/inbound", compose)
        self.assertIn('user: "${GUEST_RUNTIME_UID:-10001}:${GUEST_RUNTIME_GID:-10001}"', compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("USER guest", dockerfile)


if __name__ == "__main__":
    unittest.main()
