"""Telegram ready-check bot for any game.

/readycheck [game] [n]  - start a ready check, e.g. "/readycheck Dota 5".
                          Everyone taps a button; when n people are "Ready"
                          the bot pings them all. The game is optional,
                          and n defaults to the last count used for that game
                          in this chat, else PLAYERS_NEEDED. Replaces the
                          chat's previous ready check. /rc is an alias.
/cancel                 - remove the chat's current ready check.

Ready checks are saved to PERSISTENCE_FILE, so a restart keeps them working.
A background tick expires checks at the next EXPIRE_AT (local time, from TZ)
after they were posted, and nudges anyone who has
sat on "Soon" for SOON_NUDGE_MINUTES while the group is still short.

Set ALLOWED_CHAT_IDS to keep a deployed bot to your own group(s).
"""

import html
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    PersistenceInput,
    PicklePersistence,
    TypeHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
# The 60s tick would otherwise log two lines a minute, every minute
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("readycheck")

DEFAULT_NEEDED = int(os.environ.get("PLAYERS_NEEDED", "5"))
PERSISTENCE_FILE = os.environ.get("PERSISTENCE_FILE", "/data/dotabot.pickle")


def parse_clock(raw: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in raw.strip().split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"EXPIRE_AT must be HH:MM, got {raw!r}")
    return hour, minute


# Checks close at this local time each night, e.g. "00:30". TZ decides whose
# night; without it that is UTC.
EXPIRE_AT = parse_clock(os.environ.get("EXPIRE_AT", "00:30"))
ZONE = ZoneInfo(os.environ.get("TZ") or "UTC")
NUDGE_MINUTES = int(os.environ.get("SOON_NUDGE_MINUTES", "30"))
NUDGE_SECONDS = NUDGE_MINUTES * 60
TICK_SECONDS = 60
TRANSIENT_SECONDS = 30
SOURCE_URL = "https://github.com/rickyxsosa/five_ready_check_bot"
MAX_GAME_NAME = 40
MAX_BAR = 10


def parse_chat_ids(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(",", " ").split())


# Empty means open to any chat, which is what a fresh self-hosted copy wants.
# Set it and the bot only works in those chats, and leaves any other group.
ALLOWED_CHAT_IDS = parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))

STATUSES = {
    "ready": "✅ Ready",
    # The label promises what the nudge does, so it is built from the same setting.
    # "Later" gets no nudge at all.
    "soon": f"⏳ Soon ({NUDGE_MINUTES} min)",
    "later": "🕙 Later",
    "out": "❌ Not tonight",
}

# Reacting to the live check is a shortcut for these buttons
REACTIONS = {"👍": "ready", "👎": "out"}

# A poll, as stored in chat_data["poll"]:
#   game        game name as typed, or None for a plain ready check
#   by          first name of whoever started it
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


def parse_args(args: list[str]) -> tuple[str | None, int | None]:
    """Split command args into (game, count). Dota 5 -> ("Dota", 5), 5 -> (None, 5),
    Slay the Spire -> ("Slay the Spire", None). The count may come first or last."""
    needed = None
    if args and args[-1].isdigit():
        needed, args = int(args[-1]), args[:-1]
    elif args and args[0].isdigit():
        needed, args = int(args[0]), args[1:]
    game = " ".join(args).strip()[:MAX_GAME_NAME].strip() or None
    return game, (max(1, needed) if needed is not None else None)


def game_label(poll: dict) -> str:
    game = poll.get("game")  # absent on checks saved before games existed
    return html.escape(game) if game else ""


def progress(ready: int, needed: int) -> str:
    # Past 10 squares the bar wraps on a phone, so big checks get the count only
    if needed > MAX_BAR:
        return f"{ready}/{needed} ready"
    filled = min(ready, needed)
    return "🟩" * filled + "⬜" * (needed - filled) + f"  {ready}/{needed} ready"


def render(poll: dict, note: str | None = None) -> str:
    # Telegram gives bots no font sizes, so the title earns its weight from
    # capitals, bold and a line of its own, with the stakes on the line below
    ready = len(ready_users(poll))
    needed = poll["needed"]
    # Upper-case before escaping, or "&amp;" would become "&AMP;"
    game = html.escape((poll.get("game") or "").upper())
    if ready >= needed:
        title = f"🎮 <b>GAME ON: {game}!</b>" if game else "🎮 <b>GAME ON!</b>"
    else:
        title = f"🎮 <b>READY CHECK: {game}</b>" if game else "🎮 <b>READY CHECK</b>"
    by = poll.get("by")  # absent on checks saved before it was recorded
    subtitle = f"Started by {html.escape(by)} · needs {needed}" if by else f"Needs {needed}"

    lines = [note, ""] if note else []
    lines += [title, subtitle, progress(ready, needed), ""]
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
        if update.message and update.message.text and update.message.text.startswith("/"):
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
        "/readycheck Dota 5 starts a ready check for Dota that needs 5 players. "
        "When enough people are ready, I'll ping them all.\n"
        "The game and number are optional. I remember each game's number, so next time /readycheck Dota is enough. "
        f"A new game defaults to {DEFAULT_NEEDED}.\n"
        "/rc works the same. /cancel removes the current ready check, and a new one replaces it."
    )


async def tidy_command(update: Update) -> None:
    """Delete the /readycheck or /cancel someone typed, so the chat shows the ready
    check and not a trail of commands. Deleting other people's messages needs
    admin with "Delete messages"; without it the command just stays."""
    try:
        await update.effective_message.delete()
    except TelegramError as e:
        log.info("could not delete command in chat %s: %s", update.effective_chat.id, e)


async def delete_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError as e:
        log.info("could not delete transient message %s: %s", message_id, e)


async def say_briefly(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """A short reply that cleans itself up. Sent, not replied, because the
    command it answers may already be deleted. Not persisted: a restart in the
    next 30 seconds leaves it in the chat, which is harmless."""
    msg = await update.effective_chat.send_message(text)
    context.job_queue.run_once(delete_later, TRANSIENT_SECONDS, data=(msg.chat_id, msg.message_id))


async def drop_pin_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telegram posts "<bot> pinned a message" for every pin; that is pure noise
    msg = update.effective_message
    if msg.from_user and msg.from_user.id == context.bot.id:
        try:
            await msg.delete()
        except TelegramError as e:
            log.info("could not delete pin notice in chat %s: %s", msg.chat_id, e)


async def readycheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await tidy_command(update)
    game, needed = parse_args(context.args or [])

    # Each game keeps its last player count per chat, so "/readycheck STS 4"
    # once makes a bare "/readycheck STS" mean 4 afterwards
    counts = context.chat_data.setdefault("counts", {})
    key = game.casefold() if game else ""
    if needed is None:
        needed = counts.get(key, DEFAULT_NEEDED)
    else:
        counts[key] = needed

    chat = update.effective_chat
    # One live ready check per chat, so /cancel always knows which one it means
    old = context.chat_data.pop("poll", None)
    if old:
        await retire(context.bot, chat.id, old, "🔁 <i>Replaced by a newer ready check.</i>")

    poll = {"game": game, "by": update.effective_user.first_name, "needed": needed, "votes": {}, "pinged": False, "created": time.time(), "nudged": set()}
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
    await tidy_command(update)
    poll = context.chat_data.pop("poll", None)
    if poll is None:
        await say_briefly(update, context, "There's no ready check to cancel. Start one with /readycheck")
        return
    name = update.effective_user.first_name
    await retire(context.bot, update.effective_chat.id, poll, f"❌ <i>Cancelled by {html.escape(name)}.</i>")
    await say_briefly(update, context, f"Ready check cancelled by {name}.")


async def set_status(bot, chat_id: int, poll: dict, user_id: int, name: str, status: str | None) -> None:
    """Record someone's status (None clears it), redraw the check, and send the
    "get in!" or "dropped" ping if this crossed the line. Buttons and reactions
    both come through here, so whichever someone used last wins."""
    poll["votes"].pop(user_id, None)  # re-insert so lists stay in order of last change
    if status is not None:
        poll["votes"][user_id] = (name, status, time.time())

    try:
        await bot.edit_message_text(
            render(poll), chat_id=chat_id, message_id=poll["message_id"],
            reply_markup=keyboard(), parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise

    ready = ready_users(poll)
    needed = poll["needed"]
    reply = ReplyParameters(poll["message_id"], allow_sending_without_reply=True)
    if len(ready) >= needed and not poll["pinged"]:
        poll["pinged"] = True
        mentions = " ".join(mention(uid, n) for uid, n in ready)
        game = game_label(poll)
        await bot.send_message(
            chat_id,
            f"🎮 <b>{len(ready)} ready{' for ' + game if game else ''} — get in!</b>\n{mentions}",
            parse_mode=ParseMode.HTML,
            reply_parameters=reply,
        )
    elif len(ready) < needed and poll["pinged"]:
        # Someone dropped after we called it, so let the others know
        poll["pinged"] = False
        mentions = " ".join(mention(uid, n) for uid, n in ready)
        await bot.send_message(
            chat_id,
            f"⚠️ {html.escape(name)} dropped. Back to {len(ready)}/{needed}.\n{mentions}",
            parse_mode=ParseMode.HTML,
            reply_parameters=reply,
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    poll = context.chat_data.get("poll")
    if poll is None or poll["message_id"] != query.message.message_id:
        await query.answer("This ready check has expired. Start a new one with /readycheck", show_alert=True)
        return

    user = query.from_user
    status = query.data.split(":", 1)[1]
    current = poll["votes"].get(user.id)
    if current and current[1] == status:
        status = None  # tapping your current status clears it
    await query.answer(STATUSES[status] if status else "Status cleared")
    await set_status(context.bot, query.message.chat_id, poll, user.id, user.first_name, status)


def emojis(reactions) -> set[str]:
    # Custom-emoji and paid reactions have no .emoji; they never mean a status
    return {r.emoji for r in reactions if getattr(r, "emoji", None)}


async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """👍 on the live check means Ready, 👎 means Not tonight, and taking the
    reaction away clears it. Telegram only sends these to admin bots. Reactions
    from anonymous admins carry no user, so they can't count."""
    change = update.message_reaction
    poll = context.chat_data.get("poll")
    actor = getattr(change, "actor_chat", None)
    who = change.user.first_name if change.user else f"anonymous ({getattr(actor, 'title', None)})"
    old, new = emojis(change.old_reaction), emojis(change.new_reaction)
    # One line per reaction, whatever happens to it: reactions fail silently
    # from the user's side, so the log is the only way to see why one didn't count
    def outcome(result: str) -> None:
        log.info(
            "reaction in chat %s on message %s by %s: %s -> %s (raw %r): %s",
            change.chat.id, change.message_id, who, sorted(old), sorted(new),
            [getattr(r, "emoji", None) or r.type for r in change.new_reaction], result,
        )

    if change.user is None:
        return outcome("ignored, no user")
    if poll is None:
        return outcome("ignored, no live check in this chat")
    if poll["message_id"] != change.message_id:
        return outcome(f"ignored, live check is message {poll['message_id']}")

    added, removed = new - old, old - new
    current = poll["votes"].get(change.user.id)
    current_status = current[1] if current else None

    status = next((REACTIONS[e] for e in added if e in REACTIONS), None)
    if status is None:
        # Only clear if the reaction taken away is what set the current status,
        # so removing a stale 👍 after tapping "Soon" leaves "Soon" alone
        if not any(REACTIONS.get(e) == current_status for e in removed):
            return outcome(f"no change, status stays {current_status}")
    elif status == current_status:
        return outcome(f"no change, already {status}")
    outcome(f"{current_status} -> {status}")
    await set_status(context.bot, change.chat.id, poll, change.user.id, change.user.first_name, status)


def reaction_handler() -> MessageReactionHandler:
    # Keyword, not positional: MessageReactionHandler's second parameter is
    # chat_id, and passing the reaction type there filtered every chat out
    return MessageReactionHandler(
        on_reaction, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED
    )


def expires_at(created: float) -> float:
    """The first EXPIRE_AT after a check was posted. Built from the calendar date
    rather than by adding 24h, so it stays on the wall-clock time across DST."""
    start = datetime.fromtimestamp(created, ZONE)
    for day in (start.date(), start.date() + timedelta(days=1)):
        cutoff = datetime(day.year, day.month, day.day, *EXPIRE_AT, tzinfo=ZONE)
        if cutoff > start:
            return cutoff.timestamp()
    raise AssertionError("unreachable: tomorrow's cutoff is always later")


async def check_poll(bot, chat_id: int, data: dict, now: float) -> bool:
    """Expire or nudge one chat's ready check. Returns True if chat_data changed."""
    poll = data.get("poll")
    if not poll:
        return False

    if now >= expires_at(poll["created"]):
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
        f"⏳ {mentions}, you said soon. {len(ready)}/{needed} ready.",
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
    app.add_handler(CommandHandler(["readycheck", "rc"], readycheck))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^status:"))
    app.add_handler(reaction_handler())
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, drop_pin_notice))
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=TICK_SECONDS, name="tick")

    if ALLOWED_CHAT_IDS:
        log.info("Bot started, limited to chats %s", sorted(ALLOWED_CHAT_IDS))
    else:
        log.info("Bot started, open to every chat (ALLOWED_CHAT_IDS is not set)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
