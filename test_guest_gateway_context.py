import base64
import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from guest_gateway import Config, GuestGateway, GuestJob, SESSION_HISTORY_CHARS

OWNER_ID = 123456789
CHAT_ID = -1003600387625
BOT_ID = 8824886468


def gateway():
    cfg = Config(
        bot_token=f"{BOT_ID}:test",
        owner_id=OWNER_ID,
        hermes_url="http://127.0.0.1:8644/v1",
        hermes_key="k",
        model="gpt-5.5",
        bot_username="guest_bot",
    )
    tmp = tempfile.TemporaryDirectory()
    gw = GuestGateway(cfg, state_path=Path(tmp.name) / "state.json")
    gw._test_temp = tmp
    return gw


def owner_message(*, text="@guest_bot ping", message_id=10, reply=None, entities=None, update_id=100, chat_id=CHAT_ID, caller_id=OWNER_ID):
    msg = {
        "guest_query_id": f"gq-{message_id}",
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": caller_id, "is_bot": False, "username": "guest_owner"},
        "text": text,
    }
    if reply is not None:
        msg["reply_to_message"] = reply
    if entities is not None:
        msg["entities"] = entities
    return {"update_id": update_id, "guest_message": msg}


def guest_bot_reply(message_id=50, text="old bot answer"):
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": BOT_ID, "is_bot": True, "username": "guest_bot"},
        "guest_bot_caller_user": {"id": OWNER_ID, "is_bot": False},
        "text": text,
    }


def normal_bot_reply(message_id=55, text="visible bot answer"):
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": BOT_ID, "is_bot": True, "username": "guest_bot"},
        "text": text,
    }


def other_bot_reply(message_id=56, text="other bot answer"):
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": 123456, "is_bot": True, "username": "otherbot"},
        "text": text,
    }


def other_user_reply(message_id=60, text="other text"):
    return {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": 12345, "is_bot": False, "username": "other"},
        "text": text,
    }


def mention_entity(text="@guest_bot", offset=0):
    return {"type": "mention", "offset": offset, "length": len(text)}



def invocation(number=10, reply=None, text=None, **kwargs):
    return owner_message(message_id=number, update_id=number, reply=reply,
                         text=text or f"@guest_bot question {number}",
                         entities=[mention_entity()], **kwargs)["guest_message"]


def establish(gw, number=10, reply=None):
    msg = invocation(number, reply)
    msg["_guest_context"] = gw._context_info(msg, number)
    sid = gw._guest_session_id(msg)
    gw._remember_reply_session_exchange(sid, gw._build_hermes_prompt(msg), f"answer {number}")
    answer = guest_bot_reply(number + 100, f"answer {number}")
    gw._register_bot_message_context(msg, answer, msg["_guest_context"]["thread_id"], msg["_guest_context"]["mode"])
    return msg, answer, sid


class SessionTests(unittest.TestCase):
    def test_two_new_mentions_have_distinct_sessions(self):
        gw = gateway()
        first, answer, sid = establish(gw)
        second, _, sid2 = establish(gw, 11)
        self.assertNotEqual(sid, sid2)
        self.assertLessEqual(len(sid), 64)
        self.assertFalse(second["_guest_context"]["uses_prior_context"])
        self.assertNotIn("question 10", json.dumps(gw._reply_session_history(second)))

    def test_return_to_older_answer_after_new_branch(self):
        gw = gateway()
        first, answer, sid = establish(gw)
        establish(gw, 11)
        follow = invocation(12, answer)
        follow["_guest_context"] = gw._context_info(follow, 12)
        self.assertEqual(gw._guest_session_id(follow), sid)
        self.assertTrue(follow["_guest_context"]["uses_prior_context"])
        history = json.dumps(gw._reply_session_history(follow)[1])
        self.assertIn("question 10", history)
        self.assertNotIn("question 11", history)

    def test_plain_reply_does_not_change_next_mention(self):
        gw = gateway()
        _, answer, sid = establish(gw)
        before = gw.state_path.read_bytes()
        gw.handle_guest(owner_message(text="plain reply", reply=answer, entities=[]))
        self.assertEqual(gw.jobs.qsize(), 0)
        self.assertEqual(before, gw.state_path.read_bytes())
        msg = invocation(12)
        msg["_guest_context"] = gw._context_info(msg, 12)
        self.assertFalse(msg["_guest_context"]["uses_prior_context"])
        self.assertNotEqual(sid, gw._guest_session_id(msg))

    def test_other_mention_and_unmentioned_root_are_ignored(self):
        for reply in (None, guest_bot_reply()):
            gw = gateway()
            gw.handle_guest(owner_message(text="@someone hi", reply=reply,
                                          entities=[mention_entity("@someone")]))
            self.assertEqual(gw.jobs.qsize(), 0)
            self.assertFalse(gw.state_path.exists())

    def test_plain_reply_is_silent_even_without_configured_username(self):
        for username in ("guest_bot", ""):
            for reply in (guest_bot_reply(), normal_bot_reply()):
                with self.subTest(username=username, reply=reply["message_id"]):
                    gw = gateway()
                    gw.cfg.bot_username = username
                    with patch.object(gw, "tg") as telegram, patch.object(gw, "call_hermes") as hermes:
                        gw.handle_guest(owner_message(text="ordinary reply", reply=reply, entities=[]))
                    telegram.assert_not_called()
                    hermes.assert_not_called()
                    self.assertEqual(gw.jobs.qsize(), 0)
                    self.assertFalse(gw.state_path.exists())

    def test_mention_inside_quoted_answer_does_not_invoke_bot(self):
        gw = gateway()
        answer = guest_bot_reply(text="@guest_bot quoted text")
        answer["entities"] = [mention_entity()]
        with patch.object(gw, "tg") as telegram:
            gw.handle_guest(owner_message(text="ordinary reply", reply=answer, entities=[]))
        telegram.assert_not_called()
        self.assertEqual(gw.jobs.qsize(), 0)

    def test_handle_mention_uses_reply_only_to_select_session(self):
        gw = gateway()
        first, answer, sid = establish(gw)
        with patch.object(gw, "tg"):
            for number, reply in ((12, answer), (13, None)):
                gw.handle_guest({"update_id": number, "guest_message": invocation(number, reply)})
        followup = gw.jobs.get_nowait()
        fresh = gw.jobs.get_nowait()
        self.assertEqual(followup.context_mode, "followup")
        self.assertEqual(gw._guest_session_id(followup.message), sid)
        self.assertEqual(fresh.context_mode, "standalone")
        self.assertNotEqual(gw._guest_session_id(fresh.message), sid)

    def test_missing_username_does_not_accept_an_arbitrary_mention(self):
        gw = gateway()
        gw.cfg.bot_username = ""
        msg = invocation(text="@someone hello")
        msg["entities"] = [mention_entity("@someone")]
        self.assertFalse(gw._has_explicit_mention(msg))

    def test_text_mention_requires_exact_bot_identity(self):
        gw = gateway()
        gw.cfg.bot_username = ""
        for target_id, expected in ((BOT_ID, True), (BOT_ID + 1, False)):
            msg = invocation(text="bot hello")
            msg["entities"] = [{"type": "text_mention", "offset": 0, "length": 3,
                                "user": {"id": target_id, "is_bot": True}}]
            self.assertEqual(gw._has_explicit_mention(msg), expected)

    def test_restored_plain_reply_is_not_executed(self):
        for version in (None, 2):
            with self.subTest(version=version):
                gw = gateway()
                msg = invocation(10, guest_bot_reply(), text="ordinary reply")
                msg["entities"] = []
                job = GuestJob(10, "gq-10", msg, time.time(), "old", "followup")
                gw.state_path.write_text(json.dumps({"offset": 11, "session_policy_version": version,
                                                    "pending_jobs": {job.key: job.to_state()}}))
                restored = GuestGateway(gw.cfg, state_path=gw.state_path)
                self.assertEqual(restored.offset, 11)
                self.assertEqual(restored.jobs.qsize(), 0)

    def test_nonowner_reply_does_not_mutate_session(self):
        gw = gateway()
        _, answer, _ = establish(gw)
        before = gw.state_path.read_bytes()
        gw.handle_guest(owner_message(reply=answer, caller_id=OWNER_ID + 1, entities=[mention_entity()]))
        self.assertEqual(before, gw.state_path.read_bytes())
        self.assertEqual(gw.jobs.qsize(), 0)

    def test_chat_caller_topic_and_dm_topic_are_boundaries(self):
        for field, value in (("chat", {"id": CHAT_ID - 1, "type": "supergroup"}),
                             ("from", {"id": OWNER_ID + 1}),
                             ("message_thread_id", 123), ("direct_messages_topic_id", 456)):
            with self.subTest(field=field):
                gw = gateway()
                _, answer, _ = establish(gw)
                msg = invocation(12, answer)
                msg[field] = value
                self.assertFalse(gw._context_info(msg, 12)["uses_prior_context"])

    def test_reply_topic_cannot_override_current_topic(self):
        gw = gateway()
        msg = invocation()
        msg["message_thread_id"] = 7
        answer = guest_bot_reply()
        answer["message_thread_id"] = 7
        gw._register_bot_message_context(msg, answer, "topic-7", "standalone")
        follow = invocation(11, answer)
        follow["message_thread_id"] = 8
        self.assertFalse(gw._context_info(follow, 11)["uses_prior_context"])

    def test_other_bot_or_wrong_guest_caller_cannot_reuse_registered_id(self):
        gw = gateway()
        _, answer, _ = establish(gw)
        for changed in (dict(answer, **{"from": other_bot_reply()["from"]}),
                        dict(answer, guest_bot_caller_user={"id": OWNER_ID + 1})):
            self.assertFalse(gw._context_info(invocation(12, changed), 12)["uses_prior_context"])

    def test_unknown_bot_answer_never_selects_latest_session(self):
        gw = gateway()
        establish(gw)
        msg = invocation(12, guest_bot_reply(999))
        msg["_guest_context"] = gw._context_info(msg, 12)
        self.assertEqual(msg["_guest_context"]["mode"], "unresolved_bot_reply")
        self.assertIsNotNone(gw._guest_session_id(msg))
        self.assertEqual(gw._reply_session_history(msg)[1], [])
        self.assertFalse(gw._context_info(invocation(13, guest_bot_reply(999)), 13)["uses_prior_context"])

    def test_reply_to_third_party_starts_new_session_and_keeps_only_selected_source(self):
        gw = gateway()
        establish(gw)
        source = other_user_reply(text="SELECTED_SOURCE")
        source["reply_to_message"] = other_user_reply(999, "UNRELATED_NESTED_MESSAGE")
        raw = invocation(12, source)
        raw["reference_messages"] = [other_user_reply(888, "UNRELATED_CHAT_HISTORY")]
        msg = gw._invocation_message(raw)
        msg["_guest_context"] = gw._context_info(msg, 12)
        self.assertEqual(msg["_guest_context"]["mode"], "anchored_new")
        prompt = gw._build_hermes_prompt(msg)
        self.assertIn("SELECTED_SOURCE", prompt)
        self.assertNotIn("UNRELATED", prompt)
        self.assertNotIn("question 10", prompt)
        sid = gw._guest_session_id(msg)
        gw._remember_reply_session_exchange(sid, prompt, "source answer")
        gw._register_bot_message_context(msg, guest_bot_reply(112), msg["_guest_context"]["thread_id"], "anchored_new")
        follow = invocation(13, guest_bot_reply(112))
        follow["_guest_context"] = gw._context_info(follow, 13)
        self.assertIn("SELECTED_SOURCE", json.dumps(gw._reply_session_history(follow)[1]))
        self.assertNotIn("UNRELATED", gw.state_path.read_text())

    def test_session_and_original_anchor_survive_restart_and_one_day(self):
        gw = gateway()
        with patch("guest_gateway.time.time", return_value=1000):
            _, answer, sid = establish(gw)
        with patch("guest_gateway.time.time", return_value=1000 + 86400):
            restored = GuestGateway(gw.cfg, state_path=gw.state_path)
            follow = invocation(12, answer)
            follow["_guest_context"] = restored._context_info(follow, 12)
            self.assertEqual(restored._guest_session_id(follow), sid)
            self.assertIn("question 10", json.dumps(restored._reply_session_history(follow)[1]))
            self.assertEqual(gw.state_path.stat().st_mode & 0o777, 0o600)

    def test_session_expires_by_inactivity_and_history_expires_together(self):
        gw = gateway()
        gw.cfg.session_ttl = 10
        with patch("guest_gateway.time.time", return_value=1000):
            _, answer, sid = establish(gw)
        with patch("guest_gateway.time.time", return_value=1011):
            msg = invocation(12, answer)
            self.assertFalse(gw._context_info(msg, 12)["uses_prior_context"])
            self.assertNotIn(sid, gw.reply_sessions)
            self.assertEqual(gw.context_threads, {})
            self.assertNotIn("question 10", gw.state_path.read_text())

    def test_activity_extends_all_answers_in_same_session(self):
        gw = gateway()
        gw.cfg.session_ttl = 10
        with patch("guest_gateway.time.time", return_value=1000):
            _, answer, _ = establish(gw)
        with patch("guest_gateway.time.time", return_value=1009):
            self.assertTrue(gw._context_info(invocation(12, answer), 12)["uses_prior_context"])
        with patch("guest_gateway.time.time", return_value=1018):
            self.assertTrue(gw._context_info(invocation(13, answer), 13)["uses_prior_context"])

    def test_legacy_ambiguous_state_is_not_reused_and_queue_is_preserved(self):
        gw = gateway()
        msg = invocation()
        msg["_guest_context"] = {"mode": "pending_followup", "thread_id": "old-polluted", "uses_prior_context": True}
        job = GuestJob(10, "gq-10", msg, time.time(), "old-polluted", "pending_followup")
        gw.state_path.parent.mkdir(exist_ok=True)
        gw.state_path.write_text(json.dumps({"offset": 11,
            "recent_answer_threads": {"old": {"thread_id": "old-polluted"}},
            "pending_reply_anchors": {"old": {"thread_id": "old-polluted"}},
            "pending_jobs": {job.key: job.to_state()}}))
        restored = GuestGateway(gw.cfg, state_path=gw.state_path)
        self.assertEqual(restored.offset, 11)
        self.assertEqual(restored.jobs.qsize(), 1)
        queued = restored.jobs.get_nowait()
        self.assertNotEqual(queued.context_thread_id, "old-polluted")
        self.assertFalse(queued.message["_guest_context"]["uses_prior_context"])

    def test_history_count_and_size_are_bounded(self):
        gw = gateway()
        msg, _, sid = establish(gw)
        for n in range(60):
            gw._remember_reply_session_exchange(sid, f"prompt {n}" + "x" * 5000, f"answer {n}" + "y" * 5000)
        history = gw._reply_session_history(msg)[1]
        self.assertLessEqual(len(history), 48)
        self.assertLessEqual(sum(len(item["content"]) for item in history), SESSION_HISTORY_CHARS)
        self.assertEqual([item["role"] for item in history], ["user", "assistant"] * (len(history) // 2))
        self.assertIn("answer 59", history[-1]["content"])
        self.assertIn("question 10", history[0]["content"])

    def test_bot_identity_changes_fresh_session_id(self):
        gw = gateway()
        msg = invocation()
        first = gw._context_info(msg, 10)["thread_id"]
        gw.cfg.bot_token = "456:test"
        self.assertNotEqual(first, gw._context_info(msg, 10)["thread_id"])

    def test_replacing_bot_cannot_reuse_previous_bot_anchors(self):
        gw = gateway()
        _, answer, _ = establish(gw)
        gw.cfg.bot_token = "456:test"
        answer["from"]["id"] = 456
        self.assertFalse(gw._context_info(invocation(12, answer), 12)["uses_prior_context"])

    def test_pending_followup_preserves_history_across_restart_past_ttl(self):
        gw = gateway()
        gw.cfg.session_ttl = 10
        with patch("guest_gateway.time.time", return_value=1000):
            _, answer, sid = establish(gw)
            msg = invocation(12, answer)
            msg["_guest_context"] = gw._context_info(msg, 12)
            gw._persist_and_queue_job(GuestJob(12, "q12", msg, 1000,
                msg["_guest_context"]["thread_id"], "followup"))
        with patch("guest_gateway.time.time", return_value=1100):
            restored = GuestGateway(gw.cfg, gw.state_path)
            self.assertIn(sid, restored.reply_sessions)
            self.assertTrue(restored.context_threads)
            self.assertEqual(restored.jobs.qsize(), 1)

    def test_session_limit_evicts_history_and_anchors_together(self):
        gw = gateway()
        with patch("guest_gateway.time.time", return_value=1000):
            first, answer, sid = establish(gw)
        for n in range(200):
            with patch("guest_gateway.time.time", return_value=1001 + n):
                gw._register_bot_message_context(invocation(1000 + n), guest_bot_reply(2000 + n),
                                                 f"thread-{n}", "standalone")
        self.assertEqual(len(gw.reply_sessions), 200)
        self.assertNotIn(sid, gw.reply_sessions)
        self.assertNotIn(gw._bot_message_key(first, answer), gw.context_threads)

    def test_answer_guest_registers_the_returned_inline_id(self):
        gw = gateway()
        msg = invocation()
        msg["_guest_context"] = gw._context_info(msg, 10)
        raw = struct.pack("<iqiq", 2, CHAT_ID + 1000000000000, 110, 777)
        inline_id = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        gw.tg = lambda *a, **kw: {"result": {"inline_message_id": inline_id}}
        gw.answer_guest("q10", "thinking", msg, purpose="placeholder")
        self.assertTrue(gw._context_info(invocation(12, guest_bot_reply(110)), 12)["uses_prior_context"])

    def test_inline_ids_register_exact_answers_and_survive_restart(self):
        for format_ in ("legacy", "64"):
            with self.subTest(format=format_):
                gw = gateway()
                msg = invocation()
                msg["_guest_context"] = gw._context_info(msg, 10)
                peer = CHAT_ID + 1000000000000
                # Legacy format needs a channel ID within the signed 32-bit range.
                if format_ == "legacy":
                    msg["chat"]["id"] = -1001234567890
                    peer = -1234567890
                    raw = struct.pack("<iiiq", 2, 110, peer, 777)
                else:
                    raw = struct.pack("<iqiq", 2, peer, 110, 777)
                inline_id = base64.urlsafe_b64encode(raw).decode().rstrip("=")
                gw._register_inline_message_context(msg, inline_id)
                answer = guest_bot_reply(110)
                answer["chat"] = msg["chat"]
                follow = invocation(12, answer, chat_id=msg["chat"]["id"])
                restored = GuestGateway(gw.cfg, state_path=gw.state_path)
                self.assertEqual(restored._context_info(follow, 12)["thread_id"], msg["_guest_context"]["thread_id"])

    def test_malformed_or_foreign_inline_id_is_not_registered(self):
        gw = gateway()
        msg = invocation()
        msg["_guest_context"] = gw._context_info(msg, 10)
        for raw in (b"invalid", struct.pack("<iqiq", 2, -1, 110, 777), struct.pack("<iqiq", 0, CHAT_ID + 1000000000000, 110, 777)):
            gw._register_inline_message_context(msg, base64.urlsafe_b64encode(raw).decode())
        self.assertEqual(gw.context_threads, {})

    def test_bot_command_without_mention_does_not_invoke_guest(self):
        gw = gateway()
        for bot in ("otherbot", "guest_bot"):
            command = f"/ask@{bot}"
            msg = invocation(text=command)
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
            self.assertFalse(gw._has_explicit_mention(msg))

    def test_same_session_workers_are_serialized_in_queue_order(self):
        gw = gateway()
        gw.cfg.worker_count = 3
        gw.cfg.placeholder_enabled = False
        entered = threading.Event()
        release = threading.Event()
        order = []
        def run(msg, progress_callback=None):
            number = msg["message_id"]
            order.append((number, "start"))
            if number == 10:
                entered.set()
                self.assertTrue(release.wait(2))
            order.append((number, "end"))
            return "done"
        gw.call_hermes = run
        gw._send_owner_media_for_reply = lambda *a: []
        gw.send_chat_message = lambda *a, **kw: None
        gw._set_message_reaction = lambda *a, **kw: None
        for number in (10, 11):
            msg = invocation(number)
            gw._persist_and_queue_job(GuestJob(number, f"q{number}", msg, time.time(), "same", "standalone"))
        gw.start_worker()
        try:
            self.assertTrue(entered.wait(2))
            self.assertNotIn((11, "start"), order)
            release.set()
            deadline = time.monotonic() + 3
            while gw.jobs.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(gw.jobs.unfinished_tasks, 0)
            self.assertEqual(order, [(10, "start"), (10, "end"), (11, "start"), (11, "end")])
        finally:
            release.set()
            gw.stop_worker()


if __name__ == "__main__":
    unittest.main()
