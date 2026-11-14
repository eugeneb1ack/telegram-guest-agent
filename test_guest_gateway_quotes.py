import json
import unittest
import time
from unittest.mock import patch

from guest_gateway import GuestJob, GuestGateway
from test_guest_gateway_context import gateway, owner_message


class QuoteContextTests(unittest.TestCase):
    def setUp(self):
        self.gw = gateway()
        self.addCleanup(self.gw._test_temp.cleanup)

    def context(self, message):
        with patch.object(self.gw, "media_context", return_value={}):
            prompt = self.gw._build_hermes_prompt(message)
        line = next(line for line in prompt.splitlines() if line.startswith("telegram_context: "))
        return json.loads(line.removeprefix("telegram_context: ")), prompt

    def test_selected_plain_quote_survives_filter_queue_restart_and_prompt(self):
        message = owner_message(reply={"message_id": 9, "text": "🌿 Full article: use a buffer.",
                                       "reply_to_message": {"text": "nested private"},
                                       "quote": {"text": "nested quote"}})["guest_message"]
        quote = {"text": "use a buffer", "position": 18, "is_manual": True,
                 "entities": [{"type": "bold", "offset": 0, "length": 3}]}
        message["entities"] = [{"type": "mention", "offset": 0, "length": 10}]
        message["quote"] = {**quote, "reference_messages": [{"text": "private"}]}
        invocation = self.gw._invocation_message(message)
        self.assertEqual(invocation["quote"], quote)
        self.assertNotIn("reply_to_message", invocation["reply_to_message"])
        self.assertNotIn("quote", invocation["reply_to_message"])
        self.gw._persist_and_queue_job(GuestJob(100, "gq-10", invocation, time.time(), "quote-test", "anchored_new"))
        restored = GuestGateway(self.gw.cfg, state_path=self.gw.state_path)
        queued = next(iter(restored.pending_jobs.values())).message
        context, prompt = self.context(queued)
        self.assertEqual(context["quote"]["selected_text"], "use a buffer")
        self.assertEqual(context["quote"]["position"], 18)
        self.assertIn("Full article", context["reply_to_message"]["text"])
        self.assertNotIn("nested private", prompt)
        self.assertIn("exact selected fragment", prompt)

    def test_empty_text_rich_quote_keeps_text_boundaries_and_inline_styles(self):
        message = owner_message()["guest_message"]
        message["quote"] = {"text": "", "rich_texts": [
            ["🌿 ", {"type": "bold", "text": "Keep a buffer"}, "."],
            {"type": "italic", "text": "Reduce delay."}], "position": 2}
        context, _ = self.context(self.gw._invocation_message(message))
        self.assertEqual(context["quote"]["selected_text"], "🌿 Keep a buffer.\nReduce delay.")
        self.assertEqual(context["quote"]["rich_texts"], message["quote"]["rich_texts"])

    def test_plain_text_is_authoritative_when_both_forms_are_present(self):
        context, _ = self.context({"quote": {"text": "chosen", "rich_texts": ["other"]}})
        self.assertEqual(context["quote"]["selected_text"], "chosen")

    def test_external_quote_without_reply_never_imports_history(self):
        message = owner_message(text="@guest_bot Explain this")["guest_message"]
        message["quote"] = {"text": "quoted fragment", "position": 100}
        message["external_reply"] = {"chat": {"id": -10077}, "message_id": 5,
                                     "origin": {"type": "hidden_user", "sender_user_name": "Source"},
                                     "reference_messages": [{"text": "unrelated"}]}
        invocation = self.gw._invocation_message(message)
        context, prompt = self.context(invocation)
        self.assertNotIn("reply_to_message", context)
        self.assertEqual(context["external_reply"]["message_id"], 5)
        self.assertNotIn("unrelated", prompt)
        self.assertEqual(self.gw._context_info(invocation, 100)["mode"], "anchored_new")
        self.assertFalse(self.gw._context_info(invocation, 100)["uses_prior_context"])

    def test_caption_reply_and_no_quote_are_preserved(self):
        message = owner_message(reply={"message_id": 9, "caption": "caption source"})["guest_message"]
        context, _ = self.context(self.gw._invocation_message(message))
        self.assertNotIn("quote", context)
        self.assertEqual(context["reply_to_message"]["text"], "caption source")

    def test_quoted_greeting_does_not_use_lightweight_reasoning(self):
        message = owner_message(text="@guest_bot привет")["guest_message"]
        message["_guest_context"] = {"mode": "standalone"}
        message["quote"] = {"text": "complex source", "position": 0}
        self.assertFalse(self.gw._is_standalone_greeting(message))


if __name__ == "__main__":
    unittest.main()
