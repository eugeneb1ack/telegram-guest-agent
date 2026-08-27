import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from guest_gateway import Config, GuestGateway, GuestJob

OWNER_ID = 123456789
CHAT_ID = -1003600387625
BOT_ID = 8824886468


def gateway():
    cfg = Config(
        bot_token="123:test",
        owner_id=OWNER_ID,
        hermes_url="http://127.0.0.1:8644/v1",
        hermes_key="k",
        model="gpt-5.5",
        bot_username="guest_bot",
        pending_anchor_ttl=120,
    )
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    Path(tmp.name).unlink(missing_ok=True)
    return GuestGateway(cfg, state_path=Path(tmp.name))


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


def test_plain_owner_reply_to_guest_bot_is_ignored_and_not_queued():
    gw = gateway()
    update = owner_message(text="да, это", message_id=11, reply=guest_bot_reply(), entities=[])

    gw.handle_guest(update, dry_run=True)

    assert gw.jobs.qsize() == 0
    assert gw.pending_reply_anchors


def test_plain_reply_then_standalone_mention_consumes_recent_answer_thread():
    gw = gateway()
    first = owner_message(text="@guest_bot ты тут?", message_id=100, entities=[mention_entity()], update_id=200)["guest_message"]
    first_context = gw._context_info(first, 200)
    first["_guest_context"] = first_context
    assert first_context["mode"] == "standalone"
    gw._remember_recent_answer_thread(first, "thread-first", first_context["mode"])

    plain_reply = owner_message(text="ты видишь весь контекст?", message_id=101, reply=guest_bot_reply(message_id=201, text="тут"), entities=[], update_id=201)
    gw.handle_guest(plain_reply, dry_run=True)
    assert gw.jobs.qsize() == 0

    follow = owner_message(text="@guest_bot ты видишь весь контекст?", message_id=102, entities=[mention_entity()], update_id=202)["guest_message"]
    context = gw._context_info(follow, 202)

    assert context["mode"] == "pending_followup"
    assert context["thread_id"] == "thread-first"
    assert context["uses_prior_context"] is True
    assert gw.pending_reply_anchors == {}


def test_expired_pending_plain_reply_anchor_is_not_used(monkeypatch):
    gw = gateway()
    gw.cfg.pending_anchor_ttl = 1
    first = owner_message(text="@guest_bot ты тут?", message_id=103, entities=[mention_entity()], update_id=203)["guest_message"]
    gw._remember_recent_answer_thread(first, "thread-expired-pending", "anchored_new")
    plain_reply = owner_message(text="контекст?", message_id=104, reply=guest_bot_reply(message_id=204), entities=[], update_id=204)
    monkeypatch.setattr("guest_gateway.time.time", lambda: 1000.0)
    gw.handle_guest(plain_reply, dry_run=True)
    monkeypatch.setattr("guest_gateway.time.time", lambda: 1002.5)

    follow = owner_message(text="@guest_bot контекст?", message_id=105, entities=[mention_entity()], update_id=205)["guest_message"]
    context = gw._context_info(follow, 205)

    assert context["mode"] == "standalone"
    assert context["uses_prior_context"] is False


def test_plain_reply_to_registered_normal_bot_message_is_ignored():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=31, entities=[mention_entity()])["guest_message"]
    sent = normal_bot_reply(message_id=131)
    gw._register_bot_message_context(source, sent, "thread-normal", "anchored_new")
    update = owner_message(text="да", message_id=32, reply=sent, entities=[])

    gw.handle_guest(update, dry_run=True)

    assert gw.jobs.qsize() == 0


def test_mention_without_reply_starts_a_fresh_named_session():
    gw = gateway()
    msg = owner_message(text="@guest_bot новый вопрос", message_id=12, entities=[mention_entity()], update_id=101)["guest_message"]

    context = gw._context_info(msg, 101)
    msg["_guest_context"] = context

    assert context["mode"] == "standalone"
    assert context["uses_prior_context"] is False
    session_id = gw._guest_session_id(msg)
    assert session_id is not None
    assert session_id.startswith("guest-thread-")
    assert len(session_id) <= 64


def test_reply_to_standalone_guest_answer_reuses_its_fresh_session():
    gw = gateway()
    first = owner_message(text="@guest_bot начни тест", message_id=120, entities=[mention_entity()], update_id=220)["guest_message"]
    first_context = gw._context_info(first, 220)
    first["_guest_context"] = first_context
    first_session_id = gw._guest_session_id(first)
    gw._remember_recent_answer_thread(first, first_context["thread_id"], first_context["mode"])

    follow = owner_message(
        text="@guest_bot продолжай",
        message_id=121,
        reply=guest_bot_reply(message_id=221, text="первый вопрос"),
        entities=[mention_entity()],
        update_id=221,
    )["guest_message"]
    follow_context = gw._context_info(follow, 221)
    follow["_guest_context"] = follow_context

    assert follow_context["mode"] == "followup"
    assert follow_context["uses_prior_context"] is True
    assert follow_context["thread_id"] == first_context["thread_id"]
    assert gw._guest_session_id(follow) == first_session_id


def test_second_standalone_invocation_uses_a_different_fresh_session():
    gw = gateway()
    first = owner_message(text="@guest_bot первая новая тема", message_id=122, entities=[mention_entity()], update_id=222)["guest_message"]
    second = owner_message(text="@guest_bot вторая новая тема", message_id=123, entities=[mention_entity()], update_id=223)["guest_message"]
    first["_guest_context"] = gw._context_info(first, 222)
    second["_guest_context"] = gw._context_info(second, 223)

    assert first["_guest_context"]["mode"] == "standalone"
    assert second["_guest_context"]["mode"] == "standalone"
    assert gw._guest_session_id(first) != gw._guest_session_id(second)


def test_reply_to_other_user_plus_mention_is_anchored_new_without_prior_context():
    gw = gateway()
    msg = owner_message(text="@guest_bot ответь ему", message_id=13, reply=other_user_reply(), entities=[mention_entity()], update_id=102)["guest_message"]
    context = gw._context_info(msg, 102)
    msg["_guest_context"] = context
    session_id = gw._guest_session_id(msg)
    assert context["mode"] == "anchored_new"
    assert context["uses_prior_context"] is False
    assert session_id is not None
    assert session_id.startswith("guest-thread-")
    assert len(session_id) <= 64


def test_registered_guest_bot_reply_plus_mention_is_followup_with_prior_context():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=14, entities=[mention_entity()])["guest_message"]
    sent = guest_bot_reply(message_id=70, text="first answer")
    gw._register_bot_message_context(source, sent, "thread-abc", "anchored_new")
    msg = owner_message(text="@guest_bot продолжи", message_id=15, reply=sent, entities=[mention_entity()], update_id=103)["guest_message"]
    context = gw._context_info(msg, 103)
    assert context["mode"] == "followup"
    assert context["thread_id"] == "thread-abc"
    assert context["uses_prior_context"] is True


def test_registered_normal_bot_reply_plus_mention_is_followup_with_prior_context():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=33, entities=[mention_entity()])["guest_message"]
    sent = normal_bot_reply(message_id=133, text="visible first answer")
    gw._register_bot_message_context(source, sent, "thread-visible", "anchored_new")
    msg = owner_message(text="@guest_bot продолжи", message_id=34, reply=sent, entities=[mention_entity()], update_id=111)["guest_message"]
    context = gw._context_info(msg, 111)
    assert context["mode"] == "followup"
    assert context["thread_id"] == "thread-visible"
    assert context["uses_prior_context"] is True


def test_recent_inline_guest_answer_reply_plus_mention_is_followup_with_prior_context():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=36, entities=[mention_entity()], update_id=113)["guest_message"]
    gw._remember_recent_answer_thread(source, "thread-recent-inline", "anchored_new")
    msg = owner_message(text="@guest_bot продолжи", message_id=37, reply=guest_bot_reply(message_id=137), entities=[mention_entity()], update_id=114)["guest_message"]

    context = gw._context_info(msg, 114)

    assert context["mode"] == "followup"
    assert context["thread_id"] == "thread-recent-inline"
    assert context["uses_prior_context"] is True
    assert gw.context_threads == {}


def test_stale_recent_inline_guest_answer_reply_plus_mention_is_not_reused(monkeypatch):
    gw = gateway()
    gw.cfg.pending_anchor_ttl = 1
    source = owner_message(text="@guest_bot first", message_id=38, entities=[mention_entity()], update_id=115)["guest_message"]
    monkeypatch.setattr("guest_gateway.time.time", lambda: 1000.0)
    gw._remember_recent_answer_thread(source, "thread-stale-inline", "anchored_new")
    monkeypatch.setattr("guest_gateway.time.time", lambda: 1002.5)
    msg = owner_message(text="@guest_bot продолжи", message_id=39, reply=guest_bot_reply(message_id=139), entities=[mention_entity()], update_id=116)["guest_message"]

    context = gw._context_info(msg, 116)

    assert context["mode"] == "unresolved_bot_reply"
    assert context["uses_prior_context"] is False


def test_recent_inline_guest_answer_reply_plus_mention_is_scoped_to_chat_and_caller():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=40, entities=[mention_entity()], update_id=117)["guest_message"]
    gw._remember_recent_answer_thread(source, "thread-scoped-inline", "anchored_new")
    other_chat = owner_message(text="@guest_bot продолжи", message_id=41, reply=guest_bot_reply(message_id=141), entities=[mention_entity()], update_id=118, chat_id=CHAT_ID - 1)["guest_message"]
    other_caller = owner_message(text="@guest_bot продолжи", message_id=42, reply=guest_bot_reply(message_id=142), entities=[mention_entity()], update_id=119, caller_id=OWNER_ID + 1)["guest_message"]

    chat_context = gw._context_info(other_chat, 118)
    caller_context = gw._context_info(other_caller, 119)

    assert chat_context["mode"] == "unresolved_bot_reply"
    assert chat_context["uses_prior_context"] is False
    assert caller_context["mode"] == "unresolved_bot_reply"
    assert caller_context["uses_prior_context"] is False


def test_registered_guest_bot_reply_plus_mention_wins_over_recent_inline_thread():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=43, entities=[mention_entity()], update_id=120)["guest_message"]
    sent = guest_bot_reply(message_id=143, text="visible first answer")
    gw._remember_recent_answer_thread(source, "thread-recent-conflict", "anchored_new")
    gw._register_bot_message_context(source, sent, "thread-exact-conflict", "anchored_new")
    msg = owner_message(text="@guest_bot продолжи", message_id=44, reply=sent, entities=[mention_entity()], update_id=121)["guest_message"]

    context = gw._context_info(msg, 121)

    assert context["mode"] == "followup"
    assert context["thread_id"] == "thread-exact-conflict"
    assert context["uses_prior_context"] is True


def test_unregistered_guest_bot_reply_plus_mention_stays_unresolved_without_persisting_fake_thread():
    gw = gateway()
    msg = owner_message(text="@guest_bot продолжи", message_id=16, reply=guest_bot_reply(message_id=80), entities=[mention_entity()], update_id=104)["guest_message"]
    first = gw._context_info(msg, 104)
    second = gw._context_info(msg, 105)
    assert first["mode"] == "unresolved_bot_reply"
    assert first["uses_prior_context"] is False
    assert second["mode"] == "unresolved_bot_reply"
    assert second["uses_prior_context"] is False
    assert gw.context_threads == {}


def test_unregistered_other_bot_reply_plus_mention_is_anchored_new_not_sticky():
    gw = gateway()
    msg = owner_message(text="@guest_bot смотри", message_id=35, reply=other_bot_reply(message_id=135), entities=[mention_entity()], update_id=112)["guest_message"]
    first = gw._context_info(msg, 112)
    second = gw._context_info(msg, 113)
    assert first["mode"] == "anchored_new"
    assert first["uses_prior_context"] is False
    assert second["mode"] == "anchored_new"
    assert second["uses_prior_context"] is False
    assert gw.context_threads == {}


def test_expired_registered_guest_bot_context_is_not_reused(monkeypatch):
    gw = gateway()
    gw.cfg.pending_anchor_ttl = 1
    source = owner_message(text="@guest_bot first", message_id=22, entities=[mention_entity()])["guest_message"]
    sent = guest_bot_reply(message_id=94, text="old answer")
    gw._register_bot_message_context(source, sent, "thread-expired", "anchored_new")
    key = gw._bot_message_key(source, sent)
    gw.context_threads[key]["last_seen_at"] = 1
    gw.context_threads[key]["created_at"] = 1
    monkeypatch.setattr("guest_gateway.time.time", lambda: 2.5)
    msg = owner_message(text="@guest_bot продолжи", message_id=23, reply=sent, entities=[mention_entity()], update_id=110)["guest_message"]
    context = gw._context_info(msg, 110)
    assert context["mode"] == "unresolved_bot_reply"
    assert context["uses_prior_context"] is False
    assert key not in gw.context_threads


def test_reply_to_guest_bot_with_other_mention_is_still_ignored():
    gw = gateway()
    update = owner_message(text="@someone смотри", message_id=19, reply=guest_bot_reply(message_id=91), entities=[mention_entity(text="@someone")], update_id=107)
    gw.handle_guest(update, dry_run=True)
    assert gw.jobs.qsize() == 0


def test_bot_command_must_target_this_bot_when_username_configured():
    gw = gateway()
    wrong = owner_message(text="/ask@otherbot продолжи", message_id=20, reply=guest_bot_reply(message_id=92), entities=[{"type": "bot_command", "offset": 0, "length": len("/ask@otherbot")}], update_id=108)["guest_message"]
    right = owner_message(text="/ask@guest_bot продолжи", message_id=21, reply=guest_bot_reply(message_id=93), entities=[{"type": "bot_command", "offset": 0, "length": len("/ask@guest_bot")}], update_id=109)["guest_message"]
    assert gw._has_explicit_mention(wrong) is False
    assert gw._has_explicit_mention(right) is True


def test_prompt_marks_prior_context_only_for_registered_followup():
    gw = gateway()
    source = owner_message(text="@guest_bot first", message_id=17, entities=[mention_entity()])["guest_message"]
    sent = guest_bot_reply(message_id=90, text="first answer")
    gw._register_bot_message_context(source, sent, "thread-xyz", "anchored_new")
    msg = owner_message(text="@guest_bot дальше", message_id=18, reply=sent, entities=[mention_entity()], update_id=106)["guest_message"]
    msg["_guest_context"] = gw._context_info(msg, 106)
    prompt = gw._build_hermes_prompt(msg)
    assert '"uses_prior_context": true' in prompt
    assert '"thread_id": "thread-xyz"' in prompt


class _MonkeyPatch:
    def __init__(self):
        self.patchers = []

    def setattr(self, target, value):
        patcher = patch(target, value)
        patcher.start()
        self.patchers.append(patcher)

    def undo(self):
        while self.patchers:
            self.patchers.pop().stop()


def load_tests(_loader, _tests, _pattern):
    """Make the pytest-style context tests part of stdlib unittest discovery."""
    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        parameters = list(inspect.signature(function).parameters)
        if not parameters:
            suite.addTest(unittest.FunctionTestCase(function, description=name))
            continue
        if parameters == ["monkeypatch"]:
            def run_with_patch(test_function=function):
                monkeypatch = _MonkeyPatch()
                try:
                    test_function(monkeypatch)
                finally:
                    monkeypatch.undo()

            suite.addTest(unittest.FunctionTestCase(run_with_patch, description=name))
    return suite
