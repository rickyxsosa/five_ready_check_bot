"""Behaviour tests against fake Telegram objects. Run: python test_bot.py"""

import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

import bot

calls = []
next_id = [100]


class FakeBot:
    def __init__(self, fail_delete=False, can_pin=True):
        self.fail_delete = fail_delete
        self.can_pin = can_pin

    async def delete_message(self, chat_id, mid):
        calls.append(("delete", mid))
        if self.fail_delete:
            raise BadRequest("Message can't be deleted")

    async def edit_message_text(self, text, chat_id, message_id, parse_mode):
        calls.append(("edit", message_id, text.splitlines()[0]))

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        if not self.can_pin:
            raise BadRequest("Not enough rights to manage pinned messages in the chat")
        calls.append(("pin", message_id))

    async def unpin_chat_message(self, chat_id, message_id):
        calls.append(("unpin", message_id))

    async def send_message(self, chat_id, text, **kw):
        calls.append(("message", text))

    async def leave_chat(self, chat_id):
        calls.append(("leave", chat_id))


async def send_message(text, **kw):
    next_id[0] += 1
    calls.append(("send", next_id[0], text))
    return NS(message_id=next_id[0], chat_id=1)


async def reply_text(text, **kw):
    calls.append(("reply", text))


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, data=None):
        self.jobs.append((callback, when, data))


def make(can_delete_commands=True, **bot_kw):
    async def delete_command():
        if not can_delete_commands:
            raise BadRequest("Message can't be deleted")
        calls.append(("delcmd",))

    ctx = NS(bot=FakeBot(**bot_kw), chat_data={}, args=[], job_queue=FakeJobQueue())
    upd = NS(
        effective_chat=NS(id=1, send_message=send_message),
        effective_message=NS(reply_text=reply_text, delete=delete_command),
        effective_user=NS(first_name="Ricky"),
    )
    return ctx, upd


async def press(ctx, message_id, uid, name, status):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit(*a, **kw):
        pass

    q = NS(
        message=NS(message_id=message_id, reply_text=reply_text),
        answer=answer,
        edit_message_text=edit,
        data=f"status:{status}",
        from_user=NS(id=uid, first_name=name),
    )
    await bot.on_button(NS(callback_query=q), ctx)
    return answers[-1]


async def test_cancel_and_replace():
    calls.clear()
    ctx, upd = make()
    await bot.cancel(upd, ctx)
    assert calls[-1][0] == "send" and "no ready check" in calls[-1][2]

    await bot.readycheck(upd, ctx)
    first = ctx.chat_data["poll"]["message_id"]
    assert ("pin", first) in calls and ctx.chat_data["poll"]["pinned"]
    await bot.readycheck(upd, ctx)
    second = ctx.chat_data["poll"]["message_id"]
    assert ("delete", first) in calls and second != first

    assert "expired" in await press(ctx, first, 7, "A", "later")
    assert await press(ctx, second, 7, "A", "later") == bot.STATUSES["later"]

    await bot.cancel(upd, ctx)
    assert ("delete", second) in calls and "poll" not in ctx.chat_data


async def test_old_message_falls_back_to_edit_and_unpin():
    calls.clear()
    ctx, upd = make(fail_delete=True)
    await bot.readycheck(upd, ctx)
    mid = ctx.chat_data["poll"]["message_id"]
    calls.clear()
    await bot.cancel(upd, ctx)
    kinds = [c[0] for c in calls]
    assert kinds == ["delcmd", "delete", "edit", "unpin", "send"], calls
    assert "Cancelled by Ricky" in calls[2][2]
    assert ("unpin", mid) in calls


async def test_pin_without_admin_still_works():
    calls.clear()
    ctx, upd = make(can_pin=False)
    await bot.readycheck(upd, ctx)
    assert "poll" in ctx.chat_data and not ctx.chat_data["poll"].get("pinned")


async def test_commands_are_tidied():
    calls.clear()
    ctx, upd = make()
    await bot.readycheck(upd, ctx)
    assert calls[0] == ("delcmd",), calls
    await bot.cancel(upd, ctx)
    assert calls.count(("delcmd",)) == 2

    # The cancel confirmation is scheduled to delete itself
    confirm = calls[-1]
    assert confirm[0] == "send" and "cancelled by Ricky" in confirm[2]
    callback, when, data = ctx.job_queue.jobs[-1]
    assert callback is bot.delete_later and when == bot.TRANSIENT_SECONDS and data == (1, confirm[1])
    calls.clear()
    await callback(NS(bot=ctx.bot, job=NS(data=data)))
    assert calls == [("delete", confirm[1])]


async def test_no_delete_rights_still_works():
    calls.clear()
    ctx, upd = make(can_delete_commands=False)
    await bot.readycheck(upd, ctx)
    assert "poll" in ctx.chat_data and ("delcmd",) not in calls


async def test_pin_notice_dropped_only_for_our_pins():
    deleted = []

    async def delete():
        deleted.append(True)

    ctx = NS(bot=NS(id=42))
    ours = NS(effective_message=NS(from_user=NS(id=42), chat_id=1, delete=delete))
    theirs = NS(effective_message=NS(from_user=NS(id=7), chat_id=1, delete=delete))
    await bot.drop_pin_notice(theirs, ctx)
    assert deleted == []
    await bot.drop_pin_notice(ours, ctx)
    assert deleted == [True]


def test_expires_at_next_nightly_cutoff():
    la = ZoneInfo("America/Los_Angeles")
    saved = bot.ZONE, bot.EXPIRE_AT
    bot.ZONE, bot.EXPIRE_AT = la, (0, 30)
    try:
        def at(*a):
            return datetime(*a, tzinfo=la).timestamp()

        # Evening check: closes at 00:30 the next morning
        assert bot.expires_at(at(2026, 10, 5, 20, 0)) == at(2026, 10, 6, 0, 30)
        # Started after midnight but before 00:30: closes that same night
        assert bot.expires_at(at(2026, 10, 6, 0, 10)) == at(2026, 10, 6, 0, 30)
        # Started exactly at, or just after, the cutoff: the next night's
        assert bot.expires_at(at(2026, 10, 6, 0, 30)) == at(2026, 10, 7, 0, 30)
        assert bot.expires_at(at(2026, 10, 6, 0, 31)) == at(2026, 10, 7, 0, 30)
        # Across the DST change (1 Nov 2026) it stays at 00:30 wall-clock
        assert bot.expires_at(at(2026, 10, 31, 22, 0)) == at(2026, 11, 1, 0, 30)
        assert bot.expires_at(at(2026, 11, 1, 3, 0)) == at(2026, 11, 2, 0, 30)
    finally:
        bot.ZONE, bot.EXPIRE_AT = saved

    assert bot.parse_clock("00:30") == (0, 30) and bot.parse_clock(" 23:05 ") == (23, 5)
    for bad in ("24:00", "12:60", "noon"):
        try:
            bot.parse_clock(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


async def test_expiry():
    calls.clear()
    ctx, upd = make()
    await bot.readycheck(upd, ctx)
    poll = ctx.chat_data["poll"]
    cutoff = bot.expires_at(poll["created"])
    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, cutoff - 1)
    assert await bot.check_poll(ctx.bot, 1, ctx.chat_data, cutoff)
    assert "poll" not in ctx.chat_data and ("delete", poll["message_id"]) in calls


async def test_soon_nudge_once_and_only_when_short():
    calls.clear()
    ctx, upd = make()
    assert bot.NUDGE_MINUTES == 30 and bot.STATUSES["soon"] == "⏳ Soon (30 min)"
    ctx.args = ["2"]
    await bot.readycheck(upd, ctx)
    poll = ctx.chat_data["poll"]
    await press(ctx, poll["message_id"], 7, "Sam", "soon")
    since = poll["votes"][7][2]

    # "Later" is never nudged, however long it sits
    await press(ctx, poll["message_id"], 11, "Lee", "later")
    poll["votes"][11] = ("Lee", "later", since - 10 * bot.NUDGE_SECONDS)

    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + bot.NUDGE_SECONDS - 1)
    assert await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + bot.NUDGE_SECONDS)
    nudge = [c for c in calls if c[0] == "message"]
    assert len(nudge) == 1 and "Sam" in nudge[0][1] and "0/2" in nudge[0][1], nudge
    assert "Lee" not in nudge[0][1]
    # Not again for the same person on the same check
    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + 2 * bot.NUDGE_SECONDS)

    # Nobody gets nudged once the group is full
    await press(ctx, poll["message_id"], 8, "Ann", "ready")
    await press(ctx, poll["message_id"], 9, "Bo", "ready")
    await press(ctx, poll["message_id"], 10, "Cy", "soon")
    poll["votes"][10] = ("Cy", "soon", since)
    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + bot.NUDGE_SECONDS)


def test_parse_args():
    p = bot.parse_args
    assert p([]) == (None, None)
    assert p(["Dota", "5"]) == ("Dota", 5)
    assert p(["STS", "4"]) == ("STS", 4)
    assert p(["4", "STS"]) == ("STS", 4)
    assert p(["Slay", "the", "Spire", "4"]) == ("Slay the Spire", 4)
    assert p(["Slay", "the", "Spire"]) == ("Slay the Spire", None)
    assert p(["3"]) == (None, 3)
    assert p(["0"]) == (None, 1)
    assert p(["x" * 100])[0] == "x" * bot.MAX_GAME_NAME


async def test_game_names_and_remembered_counts():
    calls.clear()
    ctx, upd = make()
    ctx.args = ["STS", "4"]
    await bot.readycheck(upd, ctx)
    poll = ctx.chat_data["poll"]
    assert poll["game"] == "STS" and poll["needed"] == 4
    text = bot.render(poll).splitlines()
    assert text[0] == "🎮 <b>READY CHECK: STS</b>", text
    assert text[1] == "Started by Ricky · needs 4"
    assert text[2] == "⬜⬜⬜⬜  0/4 ready"

    # Same game, no number: remembers 4, whatever the case
    ctx.args = ["sts"]
    await bot.readycheck(upd, ctx)
    assert ctx.chat_data["poll"]["needed"] == 4

    # A game it hasn't seen falls back to the default
    ctx.args = ["Dota"]
    await bot.readycheck(upd, ctx)
    assert ctx.chat_data["poll"]["needed"] == bot.DEFAULT_NEEDED

    # No game at all: a plain ready check
    ctx.args = []
    await bot.readycheck(upd, ctx)
    poll = ctx.chat_data["poll"]
    assert poll["game"] is None and bot.render(poll).startswith("🎮 <b>READY CHECK</b>\n")

    # Names are upper-cased then escaped, and the full-house title names the game
    poll = {"game": "<b>R&d</b>", "needed": 1, "votes": {7: ("A", "ready", 0)}}
    text = bot.render(poll)
    assert "GAME ON: &lt;B&gt;R&amp;D&lt;/B&gt;!" in text and "&AMP;" not in text, text
    assert "🟩  1/1 ready" in text

    # Over-full checks don't overflow the bar; big checks drop it
    assert bot.progress(6, 5) == "🟩🟩🟩🟩🟩  6/5 ready"
    assert bot.progress(3, 12) == "3/12 ready"

    # Checks saved before games and starters were recorded still render
    old = {"needed": 5, "votes": {}}
    lines = bot.render(old).splitlines()
    assert lines[0] == "🎮 <b>READY CHECK</b>" and lines[1] == "Needs 5"


def gate_update(chat_id, chat_type, text=None, callback=False):
    answered = []

    async def answer(*a, **kw):
        answered.append(True)

    msg = NS(text=text, reply_text=reply_text) if text is not None else None
    upd = NS(
        effective_chat=NS(id=chat_id, type=chat_type, title="Some group"),
        message=msg,
        callback_query=NS(answer=answer) if callback else None,
    )
    return upd, answered


async def gated(upd, ctx):
    try:
        await bot.gate(upd, ctx)
    except ApplicationHandlerStop:
        return True
    return False


async def test_allow_list():
    assert bot.parse_chat_ids("-1001, 42  7") == {-1001, 42, 7}
    assert bot.parse_chat_ids("") == frozenset()
    ctx, _ = make()
    saved = bot.ALLOWED_CHAT_IDS
    try:
        # Open mode: everything passes
        bot.ALLOWED_CHAT_IDS = frozenset()
        upd, _ = gate_update(-999, "supergroup", "/readycheck")
        assert not await gated(upd, ctx)

        bot.ALLOWED_CHAT_IDS = frozenset({-1001})
        upd, _ = gate_update(-1001, "supergroup", "/readycheck")
        assert not await gated(upd, ctx)

        # A stranger's group: stopped, and the bot leaves
        calls.clear()
        upd, _ = gate_update(-999, "supergroup", "/readycheck")
        assert await gated(upd, ctx) and ("leave", -999) in calls

        # A stranger's DM: stopped, one pointer to the source, no leave
        calls.clear()
        upd, _ = gate_update(555, "private", "/readycheck")
        assert await gated(upd, ctx)
        assert [c[0] for c in calls] == ["reply"] and bot.SOURCE_URL in calls[0][1]

        # Plain chatter in a stranger's DM gets no reply at all
        calls.clear()
        upd, _ = gate_update(555, "private", "hello")
        assert await gated(upd, ctx) and calls == []

        # A button press from an old message in a stranger's group: answered, stopped
        calls.clear()
        upd, answered = gate_update(-999, "supergroup", callback=True)
        assert await gated(upd, ctx) and answered
    finally:
        bot.ALLOWED_CHAT_IDS = saved


async def main():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        result = t()
        if asyncio.iscoroutine(result):
            await result
        print("ok  ", t.__name__)
    print(f"ALL OK ({len(tests)} tests)")


if __name__ == "__main__":
    asyncio.run(main())
