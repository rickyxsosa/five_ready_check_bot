"""Behaviour tests against fake Telegram objects. Run: python test_bot.py"""

import asyncio
from types import SimpleNamespace as NS

from telegram.error import BadRequest

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


async def send_message(text, **kw):
    next_id[0] += 1
    calls.append(("send", next_id[0]))
    return NS(message_id=next_id[0])


async def reply_text(text, **kw):
    calls.append(("reply", text))


def make(**bot_kw):
    ctx = NS(bot=FakeBot(**bot_kw), chat_data={}, args=[])
    upd = NS(
        effective_chat=NS(id=1, send_message=send_message),
        effective_message=NS(reply_text=reply_text),
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
    assert calls[-1][0] == "reply" and "no ready check" in calls[-1][1]

    await bot.dota(upd, ctx)
    first = ctx.chat_data["poll"]["message_id"]
    assert ("pin", first) in calls and ctx.chat_data["poll"]["pinned"]
    await bot.dota(upd, ctx)
    second = ctx.chat_data["poll"]["message_id"]
    assert ("delete", first) in calls and second != first

    assert "expired" in await press(ctx, first, 7, "A", "later")
    assert await press(ctx, second, 7, "A", "later") == bot.STATUSES["later"]

    await bot.cancel(upd, ctx)
    assert ("delete", second) in calls and "poll" not in ctx.chat_data


async def test_old_message_falls_back_to_edit_and_unpin():
    calls.clear()
    ctx, upd = make(fail_delete=True)
    await bot.dota(upd, ctx)
    mid = ctx.chat_data["poll"]["message_id"]
    await bot.cancel(upd, ctx)
    kinds = [c[0] for c in calls]
    assert kinds == ["send", "pin", "delete", "edit", "unpin", "reply"], calls
    assert "Cancelled by Ricky" in calls[3][2]
    assert ("unpin", mid) in calls


async def test_pin_without_admin_still_works():
    calls.clear()
    ctx, upd = make(can_pin=False)
    await bot.dota(upd, ctx)
    assert "poll" in ctx.chat_data and not ctx.chat_data["poll"].get("pinned")


async def test_expiry():
    calls.clear()
    ctx, upd = make()
    await bot.dota(upd, ctx)
    poll = ctx.chat_data["poll"]
    now = poll["created"]
    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, now + bot.EXPIRE_SECONDS - 1)
    assert await bot.check_poll(ctx.bot, 1, ctx.chat_data, now + bot.EXPIRE_SECONDS)
    assert "poll" not in ctx.chat_data and ("delete", poll["message_id"]) in calls


async def test_soon_nudge_once_and_only_when_short():
    calls.clear()
    ctx, upd = make()
    ctx.args = ["2"]
    await bot.dota(upd, ctx)
    poll = ctx.chat_data["poll"]
    await press(ctx, poll["message_id"], 7, "Sam", "soon")
    since = poll["votes"][7][2]

    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + bot.NUDGE_SECONDS - 1)
    assert await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + bot.NUDGE_SECONDS)
    nudge = [c for c in calls if c[0] == "message"]
    assert len(nudge) == 1 and "Sam" in nudge[0][1] and "0/2" in nudge[0][1], nudge
    # Not again for the same person on the same check
    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + 2 * bot.NUDGE_SECONDS)

    # Nobody gets nudged once the group is full
    await press(ctx, poll["message_id"], 8, "Ann", "ready")
    await press(ctx, poll["message_id"], 9, "Bo", "ready")
    await press(ctx, poll["message_id"], 10, "Cy", "soon")
    poll["votes"][10] = ("Cy", "soon", since)
    assert not await bot.check_poll(ctx.bot, 1, ctx.chat_data, since + bot.NUDGE_SECONDS)


async def main():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        await t()
        print("ok  ", t.__name__)
    print(f"ALL OK ({len(tests)} tests)")


if __name__ == "__main__":
    asyncio.run(main())
