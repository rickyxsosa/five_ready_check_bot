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

Buttons: **✅ At my desk**, **⏳ Soon**, **🕙 Later**, **❌ Not tonight**. Tap the one you already picked to clear it.

## Behaviour

- **One live check per group.** A new `/readycheck` replaces the old one, whatever the game. The bot deletes the old message, or, past Telegram's 48h limit on deleting, marks it "Replaced" and removes its buttons. `/cancel` works the same way.
- **The "get in!" ping** goes out once the target is reached. If someone drops after that, the rest are told.
- **Survives restarts.** Ready checks are saved to `PERSISTENCE_FILE` (`/data/dotabot.pickle` in the image), so buttons keep working after a redeploy or reboot. Mount `/data` as a volume.
- **Expires** after `EXPIRE_HOURS` (6), so last night's check can't fire a ping the next day.
- **Nudges "Soon".** Anyone who has been on ⏳ Soon for `SOON_NUDGE_MINUTES` (15) while the group is still short gets one ping. That's one per person per check.
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
| `EXPIRE_HOURS` | `6` | Ready-check lifetime |
| `SOON_NUDGE_MINUTES` | `15` | When to nudge "Soon" |
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
