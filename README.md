# five_ready_check_bot

A Telegram ready-check bot for any game. `/readycheck Dota 5` posts a ready check, everyone taps a button, and once 5 people are **✅ At my desk** the bot pings them all to get in.

## Commands

- `/readycheck Dota 5`: a ready check for Dota that needs 5 players
- `/readycheck Slay the Spire 4`: multi-word names work. The number can go first or last
- `/readycheck STS`: no number uses the last count for that game in this group, or `PLAYERS_NEEDED` (5) for a game it hasn't seen
- `/readycheck`: a plain ready check with no game name
- `/rc`: short for `/readycheck`
- `/cancel`: remove the current ready check
- `/help`: show the commands

Buttons: **✅ At my desk**, **⏳ Soon (30 min)**, **🕙 Later**, **❌ Not tonight**. Tap the one you already picked to clear it.

**Reactions work too:** react 👍 to the ready check for **At my desk** or 👎 for **Not tonight**, and remove the reaction to clear it. Whichever you used last, button or reaction, counts. This needs the bot to be a group admin, because Telegram only tells admin bots about reactions. Bots can't change anyone's reactions, so after a 👍 followed by tapping **Soon**, the 👍 stays on the message while the list says Soon.

## Behaviour

- **One live check per group.** A new `/readycheck` replaces the old one, whatever the game. The bot deletes the old message, or, past Telegram's 48h limit on deleting, marks it "Replaced" and removes its buttons. `/cancel` works the same way.
- **The "get in!" ping** goes out once the target is reached. If someone drops after that, the rest are told.
- **Survives restarts.** Ready checks are saved to `PERSISTENCE_FILE` (`/data/dotabot.pickle` in the image), so buttons keep working after a redeploy or reboot. Mount `/data` as a volume.
- **Expires nightly** at `EXPIRE_AT` (00:30) in the `TZ` timezone, so last night's check can't fire a ping the next day. A check started at 00:10 closes 20 minutes later.
- **Nudges "Soon".** The button reads **⏳ Soon (30 min)**. Anyone still on it after `SOON_NUDGE_MINUTES` (30) while the group is short gets one ping, once per person per check. **🕙 Later** is never nudged.
- **Pins the check** if the bot is a group admin with "Pin messages". Without that it still works, just unpinned.
- **Keeps the chat tidy.** It deletes the `/readycheck` and `/cancel` people type, and Telegram's "pinned a message" notice. Short replies like "cancelled by …" delete themselves after 30 seconds. Deleting other people's messages needs admin with "Delete messages"; without it the commands just stay.

For the full experience, make the bot a group admin with only **Pin messages** and **Delete messages** turned on.

## Keeping your bot to your own group

A bot's @username is public, so anyone can message it or add it to their group, and your server would serve them. To stop that:

1. Set `ALLOWED_CHAT_IDS` to your group's chat id. To find it, leave the setting empty, send any command such as `/readycheck` in your group, and read it from the log line `ready check in chat -100…`. Separate several ids with commas.
2. In @BotFather, send `/setjoingroups` and choose **Disable** once the bot is in your group.

With the list set, the bot leaves any other group it is added to, and answers a stranger's direct message with a pointer to this repo.

## Settings

| Variable | Default | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | From @BotFather |
| `ALLOWED_CHAT_IDS` | (empty: any chat) | Chat ids the bot works in |
| `PLAYERS_NEEDED` | `5` | Default player count |
| `EXPIRE_AT` | `00:30` | Local time (HH:MM) each night when open checks close |
| `TZ` | UTC | Timezone for `EXPIRE_AT`, e.g. `America/Los_Angeles` |
| `SOON_NUDGE_MINUTES` | `30` | When to nudge "Soon"; also shown on the button |
| `PERSISTENCE_FILE` | `/data/dotabot.pickle` | Where checks are saved |

## Running it

Standalone: `cp .env.example .env`, fill in the token, then `docker compose up -d --build`.

In the homelab, it runs from the `rickyxsosa/Docker` stack using `ghcr.io/rickyxsosa/five_ready_check_bot:latest`, which GitHub Actions publishes on every push to `main` (see `.github/workflows/image.yml`).

## Tests

```bash
pip install -r requirements.txt
python test_bot.py
```

The tests drive the handlers with fake Telegram objects, with no network. CI runs them before publishing.
