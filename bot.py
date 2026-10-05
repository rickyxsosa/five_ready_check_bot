"""Dota ready-check bot.

/dota [n]  - start a ready check (default 5 players). Everyone taps a button;
             when n people are "At my desk" the bot pings them all.
             Replaces the chat's previous ready check.
/cancel    - remove the chat's current ready check.

Ready checks are saved to PERSISTENCE_FILE, so a restart keeps them working.
A background tick expires checks after EXPIRE_HOURS and nudges anyone who has
sat on "Soon" for SOON_NUDGE_MINUTES while the group is still short.

Set ALLOWED_CHAT_IDS to keep a deployed bot to your own group(s).
"""

import html
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PersistenceInput,
    PicklePersistence,
    TypeHandler,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("dotabot")

DEFAULT_NEEDED = int(os.environ.get("PLAYERS_NEEDED", "5"))
PERSISTENCE_FILE = os.environ.get("PERSISTENCE_FILE", "/data/dotabot.pickle")
EXPIRE_SECONDS = float(os.environ.get("EXPIRE_HOURS", "6")) * 3600
NUDGE_SECONDS = float(os.environ.get("SOON_NUDGE_MINUTES", "15")) * 60
TICK_SECONDS = 60
SOURCE_URL = "https://github.com/rickyxsosa/five_ready_check_bot"


def parse_chat_ids(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(",", " ").split())


# Empty means open to any chat, which is what a fresh self-hosted copy wants.
# Set it and the bot only works in those chats, and leaves any other group.
ALLOWED_CHAT_IDS = parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))

STATUSES = {
    "ready": "✅ At my desk",
    "soon": "⏳ Soon",
    "later": "🕙 Later",
    "out": "❌ Not tonight",
}

# A poll, as stored in chat_data["poll"]:
#   needed      players required
#   votes       {user_id: (first_name, status, since)}, since = when that status was set
#   pinged      whether the "get in!" ping has gone out
#   message_id  the ready-check message
#   created     when the check was posted, for expiry
#   nudged      user ids already nudged for sitting on "Soon"
#   pinned      whether we managed to pin it (needs admin)


def mention(user_id: int, name: str) -> str:
    # tg://user links notify the user even if they have no @username
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"status:{key}") for key, label in STATUSES.items()]
    # Two per row: four labels in one row get truncated on a phone
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def ready_users(poll: dict) -> list[tuple[int, str]]:
    return [(uid, name) for uid, (name, status, _) in poll["votes"].items() if status == "ready"]


def render(poll: dict, note: str | None = None) -> str:
    ready = len(ready_users(poll))
    needed = poll["needed"]
    header = "🎮 <b>GAME ON!</b>" if ready >= needed else "🎮 <b>Dota tonight?</b>"
    lines = [note, ""] if note else []
    lines += [f"{header} ({ready}/{needed} ready)", ""]
    for key, label in STATUSES.items():
        names = [html.escape(name) for name, status, _ in poll["votes"].values() if status == key]
        lines.append(f"{label}: {', '.join(names) if names else '—'}")
    if not note:
        lines += ["", "<i>Tap a button to set your status. Tap it again to clear it.</i>"]
    return "\n".join(lines)


async def retire(bot, chat_id: int, poll: dict, note: str) -> None:
    """Take a ready check out of play. Delete it if we can: bots may delete their
    own group messages for 48h without being admin, and deleting also drops the
    pin. Past that, edit in a note, drop the buttons and unpin."""
    try:
        await bot.delete_message(chat_id, poll["message_id"])
        return
    except TelegramError as e:
        log.info("could not delete ready check %s: %s", poll["message_id"], e)
    try:
        await bot.edit_message_text(
            render(poll, note), chat_id=chat_id, message_id=poll["message_id"], parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        log.info("could not edit ready check %s: %s", poll["message_id"], e)
    if poll.get("pinned"):
        try:
            await bot.unpin_chat_message(chat_id, message_id=poll["message_id"])
        except TelegramError as e:
            log.info("could not unpin ready check %s: %s", poll["message_id"], e)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler. Lets allowed chats through and shuts out the rest."""
    chat = update.effective_chat
    if chat is None:
        return
    if not ALLOWED_CHAT_IDS:
        if update.message and update.message.text and update.message.text.startswith("/dota"):
            # Open mode: log ids so the owner can find theirs for ALLOWED_CHAT_IDS
            log.info("ready check in chat %s (%s)", chat.id, chat.title or chat.type)
        return
    if chat.id in ALLOWED_CHAT_IDS:
        return

    log.info("ignoring chat %s (%s): not in ALLOWED_CHAT_IDS", chat.id, chat.title or chat.type)
    if update.callback_query:
        await update.callback_query.answer()
    if chat.type == "private":
        msg = update.message
        if msg and msg.text and msg.text.startswith("/"):
            await msg.reply_text(f"This bot is private. Host your own copy: {SOURCE_URL}")
    else:
        try:
            await context.bot.leave_chat(chat.id)
        except TelegramError as e:
            log.info("could not leave chat %s: %s", chat.id, e)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Use /dota to start a ready check. I'll ping everyone once {DEFAULT_NEEDED} people are at their desk.\n"
        "Use /dota 10 (or any number) to change how many players you need.\n"
        "Use /cancel to remove the current ready check. A new /dota replaces the old one."
    )


async def dota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    needed = DEFAULT_NEEDED
    if context.args:
        try:
            needed = max(1, int(context.args[0]))
        except ValueError:
            pass

    chat = update.effective_chat
    # One live ready check per chat, so /cancel always knows which one it means
    old = context.chat_data.pop("poll", None)
    if old:
        await retire(context.bot, chat.id, old, "🔁 <i>Replaced by a newer ready check.</i>")

    poll = {"needed": needed, "votes": {}, "pinged": False, "created": time.time(), "nudged": set()}
    msg = await chat.send_message(render(poll), reply_markup=keyboard(), parse_mode=ParseMode.HTML)
    poll["message_id"] = msg.message_id
    context.chat_data["poll"] = poll

    # Pinning needs the bot to be an admin with "Pin messages". Without it the
    # check still works; it just isn't pinned.
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id, disable_notification=True)
        poll["pinned"] = True
    except TelegramError as e:
        log.info("could not pin ready check in chat %s: %s", chat.id, e)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    poll = context.chat_data.pop("poll", None)
    if poll is None:
        await update.effective_message.reply_text("There's no ready check to cancel. Start one with /dota")
        return
    name = update.effective_user.first_name
    await retire(context.bot, update.effective_chat.id, poll, f"❌ <i>Cancelled by {html.escape(name)}.</i>")
    await update.effective_message.reply_text(f"Ready check cancelled by {name}.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    poll = context.chat_data.get("poll")
    if poll is None or poll["message_id"] != query.message.message_id:
        await query.answer("This ready check has expired. Start a new one with /dota", show_alert=True)
        return

    user = query.from_user
    status = query.data.split(":", 1)[1]
    current = poll["votes"].get(user.id)

    if current and current[1] == status:
        del poll["votes"][user.id]
        await query.answer("Status cleared")
    else:
        poll["votes"].pop(user.id, None)  # re-insert so lists stay in order of last change
        poll["votes"][user.id] = (user.first_name, status, time.time())
        await query.answer(STATUSES[status])

    try:
        await query.edit_message_text(render(poll), reply_markup=keyboard(), parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise

    ready = ready_users(poll)
    needed = poll["needed"]
    if len(ready) >= needed and not poll["pinged"]:
        poll["pinged"] = True
        mentions = " ".join(mention(uid, name) for uid, name in ready)
        await query.message.reply_text(
            f"🎮 <b>{len(ready)} ready — get in!</b>\n{mentions}", parse_mode=ParseMode.HTML
        )
    elif len(ready) < needed and poll["pinged"]:
        # Someone dropped after we called it, so let the others know
        poll["pinged"] = False
        mentions = " ".join(mention(uid, name) for uid, name in ready)
        await query.message.reply_text(
            f"⚠️ {html.escape(user.first_name)} dropped. Back to {len(ready)}/{needed}.\n{mentions}",
            parse_mode=ParseMode.HTML,
        )


async def check_poll(bot, chat_id: int, data: dict, now: float) -> bool:
    """Expire or nudge one chat's ready check. Returns True if chat_data changed."""
    poll = data.get("poll")
    if not poll:
        return False

    if now - poll["created"] >= EXPIRE_SECONDS:
        data.pop("poll")
        await retire(bot, chat_id, poll, "⌛ <i>Expired.</i>")
        return True

    ready = ready_users(poll)
    needed = poll["needed"]
    if len(ready) >= needed:
        return False
    # Once per person per check: a nudge is a reminder, not a nag
    due = [
        (uid, name)
        for uid, (name, status, since) in poll["votes"].items()
        if status == "soon" and now - since >= NUDGE_SECONDS and uid not in poll["nudged"]
    ]
    if not due:
        return False
    poll["nudged"].update(uid for uid, _ in due)
    mentions = " ".join(mention(uid, name) for uid, name in due)
    await bot.send_message(
        chat_id,
        f"⏳ {mentions}, you said soon. {len(ready)}/{needed} at their desk.",
        parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(poll["message_id"], allow_sending_without_reply=True),
    )
    return True


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    now = time.time()
    for chat_id, data in list(app.chat_data.items()):
        try:
            changed = await check_poll(context.bot, chat_id, data, now)
        except Forbidden as e:
            # Kicked from the group: nothing left to manage there
            log.info("dropping ready check for chat %s: %s", chat_id, e)
            data.pop("poll", None)
            changed = True
        except TelegramError as e:
            log.warning("tick failed for chat %s: %s", chat_id, e)
            continue
        if changed:
            # Jobs aren't tied to an update, so tell persistence what changed
            app.mark_data_for_update_persistence(chat_ids=chat_id)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    persistence = PicklePersistence(
        filepath=PERSISTENCE_FILE,
        store_data=PersistenceInput(bot_data=False, user_data=False, callback_data=False),
    )
    app = Application.builder().token(token).persistence(persistence).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("dota", dota))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^status:"))
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=TICK_SECONDS, name="tick")

    if ALLOWED_CHAT_IDS:
        log.info("Bot started, limited to chats %s", sorted(ALLOWED_CHAT_IDS))
    else:
        log.info("Bot started, open to every chat (ALLOWED_CHAT_IDS is not set)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
