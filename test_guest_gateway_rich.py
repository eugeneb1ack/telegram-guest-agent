import copy
import time
import unittest
from pathlib import Path

from guest_gateway import Config, GuestGateway, GuestJob, UploadedMedia


class FakeGateway(GuestGateway):
    def __init__(self, fail_once=None):
        cfg = Config(
            bot_token="token",
            owner_id=123456789,
            hermes_url="http://127.0.0.1:1/v1/chat/completions",
            hermes_key="key",
            model="test-model",
            placeholder_enabled=False,
            rich_messages_enabled=True,
        )
        super().__init__(cfg, state_path=Path("/tmp/guest-gateway-rich-test-state.json"))
        self.calls = []
        self.fail_once = list(fail_once or [])

    def tg(self, method, payload, timeout=60):
        self.calls.append((method, copy.deepcopy(payload)))
        if self.fail_once and self.fail_once[0] == method:
            self.fail_once.pop(0)
            raise RuntimeError(f"{method} failed")
        if method == "answerGuestQuery":
            return {"ok": True, "result": {"inline_message_id": "inline-1"}}
        return {"ok": True, "result": True}


def message():
    return {
        "message_id": 1001,
        "guest_query_id": "q1",
        "from": {"id": 123456789},
        "chat": {"id": -100123, "type": "group", "title": "test"},
        "text": "@guest_bot расскажи анекдот",
    }


class RichGatewayTests(unittest.TestCase):

    def test_plain_multiline_answer_gets_visible_rich_structure(self):
        gw = FakeGateway()
        text = (
            "Первый абзац отвечает на вопрос без Markdown-разметки и выглядит как обычный текст. "
            "Он достаточно длинный, чтобы быть реальным ответом, а не короткой репликой.\n\n"
            "Второй абзац добавляет контекст и доводит пример примерно до трехсот символов, "
            "чтобы Rich Message не проходил как визуально обычное сообщение."
        )

        rich = gw._input_rich_message(text)

        self.assertEqual(
            rich,
            {
                "blocks": [
                    {"type": "paragraph", "text": text.split("\n\n")[0]},
                    {"type": "paragraph", "text": text.split("\n\n")[1]},
                ]
            },
        )
        self.assertNotIn("## Ответ", gw._rich_message_debug_text(rich))

    def test_plain_short_one_liner_stays_quote_not_report(self):
        gw = FakeGateway()

        self.assertEqual(
            gw._input_rich_message("Короткий факт."),
            {"blocks": [{"type": "paragraph", "text": "Короткий факт."}]},
        )

    def test_inline_renderer_handles_italic_html_and_currency_without_false_math(self):
        gw = FakeGateway()

        italic = gw._input_rich_message("*курсив*")["blocks"][0]["text"]
        bold = gw._input_rich_message("<b>важно</b>")["blocks"][0]["text"]
        currency = gw._input_rich_message("Цена $5 и $10")["blocks"][0]["text"]

        self.assertEqual(italic, {"type": "italic", "text": "курсив"})
        self.assertEqual(bold, {"type": "bold", "text": "важно"})
        self.assertEqual(currency, "Цена $5 и $10")

    def test_rich_and_text_fallback_have_separate_safe_limits(self):
        gw = FakeGateway()
        gw.cfg.max_reply_chars = 40
        gw.cfg.rich_max_reply_chars = 120
        text = ("Первый достаточно длинный абзац для проверки границы. " * 5).strip()

        rich = gw._fit_reply(text, rich=True)
        fallback = gw._fit_reply(text, rich=False)
        blocks = gw._input_rich_message(text)["blocks"]

        self.assertLessEqual(len(rich), 120)
        self.assertLessEqual(len(fallback), 40)
        self.assertTrue(rich.endswith("…"))
        self.assertEqual(blocks[0]["type"], "paragraph")

    def test_structured_answers_are_not_double_wrapped(self):
        gw = FakeGateway()
        structured = {
            "## Итог\n\nТекст": ["heading", "paragraph"],
            "- пункт\n- пункт": ["list"],
            "| A | B |\n| - | - |\n| 1 | 2 |": ["table"],
        }

        for text, expected_types in structured.items():
            with self.subTest(text=text):
                blocks = gw._input_rich_message(text)["blocks"]
                self.assertEqual([block["type"] for block in blocks], expected_types)

    def test_answer_guest_uses_input_rich_message_content(self):
        gw = FakeGateway()
        gw.answer_guest("q1", "# Отчёт\n\n- пункт", message())

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        content = payload["result"]["input_message_content"]
        self.assertEqual(
            [block["type"] for block in content["rich_message"]["blocks"]],
            ["heading", "list"],
        )
        self.assertNotIn("message_text", content)

    def test_plain_short_answer_gets_visible_rich_block(self):
        gw = FakeGateway()
        gw.answer_guest("q1", "короткий анекдот", message())

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        rich = payload["result"]["input_message_content"]["rich_message"]
        self.assertEqual(rich, {"blocks": [{"type": "paragraph", "text": "короткий анекдот"}]})

    def test_placeholder_uses_configured_animated_custom_emoji(self):
        gw = FakeGateway()
        gw.cfg.placeholder_text = "Думаю…"
        gw.cfg.placeholder_custom_emoji_id = "1234567890123456789"
        gw.cfg.placeholder_custom_emoji_alt = "🤔"

        gw.answer_guest("q1", gw.cfg.placeholder_text, message(), purpose="placeholder")

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        rich = payload["result"]["input_message_content"]["rich_message"]
        self.assertEqual(
            rich,
            {
                "blocks": [
                    {
                        "type": "paragraph",
                        "text": [
                            {
                                "type": "custom_emoji",
                                "custom_emoji_id": "1234567890123456789",
                                "alternative_text": "🤔",
                            },
                            " ",
                            "Думаю…",
                        ],
                    }
                ]
            },
        )

    def test_invalid_custom_emoji_id_keeps_plain_rich_placeholder(self):
        gw = FakeGateway()
        gw.cfg.placeholder_custom_emoji_id = '"><b>unsafe</b>'

        gw.answer_guest("q1", "Думаю…", message(), purpose="placeholder")

        payload = [p for method, p in gw.calls if method == "answerGuestQuery"][0]
        rich = payload["result"]["input_message_content"]["rich_message"]
        self.assertEqual(rich, {"blocks": [{"type": "paragraph", "text": "Думаю…"}]})

    def test_custom_emoji_placeholder_retries_as_plain_text_when_rejected(self):
        gw = FakeGateway(fail_once=["answerGuestQuery"])
        gw.cfg.placeholder_custom_emoji_id = "1234567890123456789"

        gw.answer_guest("q1", "Думаю…", message(), purpose="placeholder")

        calls = [p for method, p in gw.calls if method == "answerGuestQuery"]
        custom = calls[0]["result"]["input_message_content"]["rich_message"]["blocks"][0]["text"][0]
        self.assertEqual(custom["type"], "custom_emoji")
        self.assertEqual(custom["custom_emoji_id"], "1234567890123456789")
        self.assertEqual(calls[1]["result"]["input_message_content"], {"message_text": "Думаю…"})

    def test_send_chat_message_uses_send_rich_message(self):
        gw = FakeGateway()
        msg = message()
        msg["message_thread_id"] = 42
        gw.send_chat_message(msg, "| A | B |\n|---|---|\n| 1 | 2 |")

        method, payload = gw.calls[-1]
        self.assertEqual(method, "sendRichMessage")
        self.assertEqual(payload["chat_id"], -100123)
        self.assertEqual(payload["message_thread_id"], 42)
        self.assertIn("rich_message", payload)
        self.assertEqual(payload["rich_message"]["blocks"][0]["type"], "table")
        self.assertEqual(payload["rich_message"]["blocks"][0]["cells"][0][0]["text"], "A")

    def test_send_chat_message_falls_back_to_text_when_rich_fails(self):
        gw = FakeGateway(fail_once=["sendRichMessage"])
        msg = message()
        msg["message_thread_id"] = 42
        gw.send_chat_message(msg, "# Done\n- ok")

        self.assertEqual(gw.calls[0][0], "sendRichMessage")
        self.assertEqual(
            [block["type"] for block in gw.calls[0][1]["rich_message"]["blocks"]],
            ["heading", "list"],
        )
        self.assertEqual(gw.calls[1][0], "sendMessage")
        self.assertEqual(gw.calls[1][1]["text"], "# Done\n- ok")
        self.assertEqual(gw.calls[1][1]["message_thread_id"], 42)

    def test_edit_guest_answer_falls_back_to_text_when_rich_fails(self):
        gw = FakeGateway(fail_once=["editMessageText"])
        gw.edit_guest_answer("inline-1", "# Done\n- ok")

        self.assertEqual(gw.calls[0][0], "editMessageText")
        self.assertEqual(gw.calls[0][1]["inline_message_id"], "inline-1")
        self.assertEqual(
            [block["type"] for block in gw.calls[0][1]["rich_message"]["blocks"]],
            ["heading", "list"],
        )
        self.assertEqual(gw.calls[1][0], "editMessageText")
        self.assertEqual(gw.calls[1][1], {"inline_message_id": "inline-1", "text": "# Done\n- ok"})

    def test_preserves_new_rich_markdown_blocks(self):
        gw = FakeGateway()
        samples = {
            "- item\n  - nested item\n- [x] done": ["list"],
            "Text with a note[^n].\n[^n]: Footnote body": ["paragraph", "footer"],
            '![](https://telegram.org/example/photo.jpg "Photo caption")': ["photo"],
            '<tg-map lat="41.9" long="12.5" zoom="14"/>': ["map"],
            "<tg-collage>\n![](https://telegram.org/example/photo.jpg)\n![](https://telegram.org/example/video.mp4)\n</tg-collage>": ["collage"],
            "<details><summary>Подробности</summary>Скрытый текст</details>": ["details"],
            "<blockquote expandable>\nКоротко\nскрываемые подробности\n</blockquote>": ["expandable_blockquote"],
            "<u>underlined</u> and <sup>2</sup>": ["paragraph"],
        }

        for sample, expected_types in samples.items():
            with self.subTest(sample=sample):
                blocks = gw._input_rich_message(sample)["blocks"]
                self.assertEqual([block["type"] for block in blocks], expected_types)

    def test_bot_api_10_3_tables_are_compact(self):
        gw = FakeGateway()

        table = gw._input_rich_message("| A | B |\n|---|---|\n| 1 | 2 |")["blocks"][0]

        self.assertIs(table["is_compact"], True)

    def test_bot_api_10_3_document_is_embedded_in_rich_answer(self):
        gw = FakeGateway()
        document = UploadedMedia(kind="document", file_id="doc-file", caption="Отчёт")

        rich = gw._input_rich_message("Готово.", uploaded_media=[document])

        self.assertEqual(rich["blocks"][-1]["type"], "document")
        self.assertEqual(
            rich["blocks"][-1]["document"],
            {"type": "document", "media": "doc-file"},
        )
        self.assertEqual(rich["blocks"][-1]["caption"]["text"], "Отчёт")

    def test_table_links_stay_native_rich_markdown(self):
        gw = FakeGateway()
        text = "| Место | Карта |\n|---|---|\n| Duo Asia | [Яндекс](https://yandex.ru/maps/?text=Duo%20Asia) |"

        table = gw._input_rich_message(text)["blocks"][0]

        self.assertEqual(table["type"], "table")
        link = table["cells"][1][1]["text"]
        self.assertEqual(link["type"], "url")
        self.assertEqual(link["text"], "Яндекс")
        self.assertEqual(link["url"], "https://yandex.ru/maps/?text=Duo%20Asia")

    def test_reply_context_extracts_rich_message_text(self):
        gw = FakeGateway()
        msg = message()
        msg["text"] = "а второй вариант?"
        msg["reply_to_message"] = {
            "rich_message": {
                "blocks": [
                    {"type": "heading", "text": "Топ мест", "size": 2},
                    {
                        "type": "table",
                        "cells": [
                            [{"text": "Место", "is_header": True}, {"text": "Карта", "is_header": True}],
                            [
                                {"text": "Duo Asia"},
                                {"text": {"type": "url", "text": "Яндекс", "url": "https://yandex.ru/maps/?text=Duo%20Asia"}},
                            ],
                        ],
                    },
                ]
            }
        }

        prompt = gw._build_hermes_prompt(msg)

        self.assertIn("reply_context: Топ мест", prompt)
        self.assertIn("Duo Asia | Яндекс (https://yandex.ru/maps/?text=Duo%20Asia)", prompt)
        self.assertIn("message: а второй вариант?", prompt)

    def test_reply_context_marks_other_user_as_target_context(self):
        gw = FakeGateway()
        msg = message()
        msg["text"] = "объясни ему коротко"
        msg["reply_to_message"] = {
            "message_id": 77,
            "from": {"id": 4399809, "first_name": "Alice", "username": "alice"},
            "text": "почему это вообще работает?",
        }

        prompt = gw._build_hermes_prompt(msg)

        self.assertIn('"reply_to_message": {', prompt)
        self.assertIn('"author_role": "other_user"', prompt)
        self.assertIn('"id": 4399809', prompt)
        self.assertIn('"username": "alice"', prompt)
        self.assertIn('"text": "почему это вообще работает?"', prompt)
        self.assertIn("message is the owner's command", prompt)
        self.assertIn("rather than an additional owner instruction", prompt)
        self.assertIn("message: объясни ему коротко", prompt)

    def test_reply_context_marks_owner_and_guest_bot_messages(self):
        gw = FakeGateway()
        owner_reply = message()
        owner_reply["text"] = "сделай из этого"
        owner_reply["reply_to_message"] = {
            "from": {"id": 123456789, "first_name": "Guest", "username": "guest_owner"},
            "text": "мой черновик",
        }
        owner_prompt = gw._build_hermes_prompt(owner_reply)
        self.assertIn('"author_role": "owner"', owner_prompt)

        bot_reply = message()
        bot_reply["text"] = "продолжи"
        bot_reply["reply_to_message"] = {
            "from": {"id": 8824886468, "is_bot": True, "username": "guest_bot"},
            "guest_bot_caller_user": {"id": 123456789, "first_name": "Guest"},
            "rich_message": {"blocks": [{"type": "paragraph", "text": "прошлый ответ бота"}]},
        }
        bot_prompt = gw._build_hermes_prompt(bot_reply)
        self.assertIn('"author_role": "guest_bot"', bot_prompt)
        self.assertIn('"text": "прошлый ответ бота"', bot_prompt)

    def test_no_draft_methods_exist_or_run(self):
        gw = FakeGateway()
        self.assertFalse(hasattr(gw, "send_rich_draft"))
        self.assertFalse(hasattr(gw, "send_rich_draft_async"))
        self.assertFalse(hasattr(gw.cfg, "rich_drafts_enabled"))
        GuestJob(update_id=1, guest_query_id="q", message=message(), queued_at=time.time())

    def test_prompt_guides_visible_rich_markdown_without_drafts(self):
        gw = FakeGateway()
        msg = message()
        msg["text"] = "сравни варианты"

        prompt = gw._build_hermes_prompt(msg)

        self.assertIn("Standard Markdown is supported", prompt)
        self.assertIn("Do not emit Telegram API JSON", prompt)
        self.assertIn("telegram_context", prompt)
        self.assertIn("media_context", prompt)
        self.assertNotIn("draft", prompt.lower())


if __name__ == "__main__":
    unittest.main()
