import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from guest_gateway import (
    Config,
    GuestGateway,
    MAX_SSE_EVENT_BYTES,
    ProgressReporter,
    anonymized_tool_status,
    iter_sse_json_events,
    progress_status_for_event,
)


def update(update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "guest_message": {
            "message_id": update_id + 1000,
            "guest_query_id": f"q{update_id}",
            "from": {"id": 123456789},
            "chat": {"id": -100123, "type": "group", "title": "test"},
            "text": "проверь задачу",
        },
    }


class ProgressGatewayTests(unittest.TestCase):
    def test_tool_names_map_to_fixed_anonymous_categories(self) -> None:
        cases = {
            "browser_control": "🌐 Использую браузер…",
            "web_search": "🔎 Ищу в интернете…",
            "apply_patch": "✍️ Пишу код…",
            "pytest": "🧪 Проверяю результат…",
            "exec_command": "⌨️ Использую командную строку…",
            "read_file": "📚 Изучаю материалы…",
            "generate_image": "🎨 Работаю с изображением…",
            "transcribe_audio": "🎧 Работаю с аудио…",
            "query_database": "📊 Анализирую данные…",
            "memory_recall": "🧠 Проверяю контекст…",
            "spawn_agent": "🧩 Подключаю дополнительного агента…",
            "private_plugin_name": "🛠 Использую инструменты…",
        }
        for tool_name, expected in cases.items():
            with self.subTest(tool_name=tool_name):
                self.assertEqual(anonymized_tool_status(tool_name), expected)
                self.assertNotIn(tool_name, expected)

    def test_progress_event_never_uses_preview_or_arguments(self) -> None:
        event = {
            "event": "tool.started",
            "tool": "terminal",
            "preview": "cd /Users/private/project && cat .env",
            "args": {"token": "secret"},
        }

        status = progress_status_for_event(event)

        self.assertEqual(status, "⌨️ Использую командную строку…")
        self.assertNotIn("/Users/private", status)
        self.assertNotIn(".env", status)
        self.assertNotIn("secret", status)

    def test_sse_parser_is_bounded_and_ignores_non_json_frames(self) -> None:
        lines = [
            b": keepalive\n",
            b"\n",
            b"data: " + (b"x" * (MAX_SSE_EVENT_BYTES + 1)) + b"\n",
            b'data: {"event": "tool.started", "tool": "terminal"}\n',
            b"\n",
            b"data: {\"event\": \"tool.started\", \"tool\": \"web_search\"}\n",
            b"\n",
            b"data: not-json\n",
            b"\n",
            b"event: ignored\n",
            b"data: {\"event\": \"reasoning.available\"}\n",
            b"\n",
        ]

        events = list(iter_sse_json_events(lines))

        self.assertEqual(
            events,
            [
                {"event": "tool.started", "tool": "web_search"},
                {"event": "reasoning.available"},
            ],
        )

    def test_reporter_cancels_pending_status_before_final_delivery(self) -> None:
        sent: list[str] = []
        reporter = ProgressReporter(sent.append, min_interval=0.05)

        reporter.report("первый")
        reporter.report("второй")
        reporter.close()
        time.sleep(0.08)

        self.assertEqual(sent, ["первый"])

    def test_sse_consumer_emits_only_anonymized_statuses(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(
                    [
                        b'data: {"event":"tool.started","tool":"browser_control","preview":"/private/path"}\n',
                        b"\n",
                        b'data: {"event":"tool.started","tool":"unknown_secret_tool"}\n',
                        b"\n",
                        b'data: {"event":"run.completed","output":"private answer"}\n',
                        b"\n",
                    ]
                )

        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                bot_token="token",
                owner_id=123456789,
                hermes_url="http://127.0.0.1:1/v1/chat/completions",
                hermes_key="key",
                model="test",
            )
            gateway = GuestGateway(cfg, state_path=Path(directory, "state.json"))
            statuses: list[str] = []
            with patch("guest_gateway.urllib.request.urlopen", return_value=Response()):
                gateway._consume_hermes_run_events(
                    "http://127.0.0.1:1/v1",
                    "run_1",
                    {"Authorization": "Bearer key"},
                    statuses.append,
                    threading.Event(),
                )

        self.assertEqual(
            statuses,
            ["🌐 Использую браузер…", "🛠 Использую инструменты…"],
        )
        self.assertNotIn("/private/path", " ".join(statuses))
        self.assertNotIn("unknown_secret_tool", " ".join(statuses))
        self.assertNotIn("private answer", " ".join(statuses))

    def test_worker_edits_placeholder_with_live_status_then_final_answer(self) -> None:
        class Gateway(GuestGateway):
            def __init__(self, state_path: Path):
                cfg = Config(
                    bot_token="token",
                    owner_id=123456789,
                    hermes_url="http://127.0.0.1:1/v1/chat/completions",
                    hermes_key="key",
                    model="test",
                    reactions_enabled=False,
                    placeholder_enabled=True,
                    progress_enabled=True,
                    progress_min_interval=0,
                )
                super().__init__(cfg, state_path=state_path)
                self.calls: list[tuple[str, dict]] = []

            def tg(self, method, payload, timeout=60):
                self.calls.append((method, copy.deepcopy(payload)))
                if method == "answerGuestQuery":
                    return {"ok": True, "result": {"inline_message_id": "inline-1"}}
                return {"ok": True, "result": True}

            def call_hermes(self, message, progress_callback=None):
                self.assert_progress_callback(progress_callback)
                progress_callback(
                    progress_status_for_event(
                        {
                            "event": "tool.started",
                            "tool": "web_search",
                            "preview": "/Users/private/repository",
                        }
                    )
                )
                return "Финальный ответ"

            @staticmethod
            def assert_progress_callback(callback):
                if callback is None:
                    raise AssertionError("worker did not provide progress callback")

        with tempfile.TemporaryDirectory() as directory:
            gateway = Gateway(Path(directory, "state.json"))
            gateway.handle_guest(update())
            gateway.running = False
            gateway.jobs.put(None)
            gateway._worker_loop()

        edits = [payload for method, payload in gateway.calls if method == "editMessageText"]
        texts = [payload["rich_message"]["blocks"][0]["text"] for payload in edits]
        self.assertEqual(texts, ["🔎 Ищу в интернете…", "Финальный ответ"])
        serialized = str(gateway.calls)
        self.assertNotIn("web_search", serialized)
        self.assertNotIn("/Users/private", serialized)

    def test_progress_environment_knobs_are_bounded(self) -> None:
        env = {
            "GUEST_BOT_TOKEN": "token",
            "GUEST_OWNER_ID": "123456789",
            "HERMES_API_KEY": "key",
            "GUEST_PROGRESS_ENABLED": "false",
            "GUEST_PROGRESS_MIN_INTERVAL": "0.1",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()

        self.assertFalse(cfg.progress_enabled)
        self.assertEqual(cfg.progress_min_interval, 0.5)


if __name__ == "__main__":
    unittest.main()
