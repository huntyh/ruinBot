# AGENTS.md — ruinBot

Hungarian Discord bot ("cirkadián adjusztment", not a real robot) built on discord.py 2.x.

## Project
- Entry point: `main.py` — creates `commands.Bot` (prefix `r!`, slash tree), opens SQLite at `usr/db.sqlite` in `on_ready`, loads every `cogs/*.py` as an extension, syncs slash commands to a hardcoded guild.
- Config: `.env` (see `.env-example`) — `DISCORD_TOKEN`, `CHANNEL` (required); `MORNING_HOUR` and `MORNING_MINUTE` are required for the morning cog to load and specify UTC. `IMGFLIP_USER/PASS`, `BOT_FRIENDS`, `SPECIAL_DATE/ADJECTIVE`, `CZ/CZ_SERV`, `KOZEL_*`, `MUSICBOT_*`, and `VOCARD_IDS` are optional settings/credentials for individual features, read via `os.getenv`.
- Data/state lives in `usr/`: `db.sqlite` (soma tables), `markov/<guild_id>/<channel_id>.txt` text corpora, `markov_config.json`, `holidays.json` (API cache), and persistent settings in `linkfixer.json`, `rolepicker_roles.json`, and `teletal_config.json`. This dir is volume-mounted in production.
- Static assets in `res/` (fonts, templates, namedays, demotivator JSON).

## Commands
- Run from the repository root with Python 3.12 (matching Docker): `python main.py`. Ensure `usr/` exists before starting.
- Install deps: `pip install -r requirements.txt` (venv already at `.venv/`).
- Tests: `unittest` tests live in `tests/`; run with `python -m unittest discover -s tests`.
- Production: Dockerfile + `docker-compose.yml` (image `ghcr.io/kassh7/ruinbot`); CI at `.github/workflows/main.yml` builds & deploys to a VPS on push to `master`.
- Captchas are built from the markov dataset; the old `captcha-proxy/` sidecar is gone.

## Architecture
- `cogs/markov.py` — the core: stores per-channel corpora and combines them into per-guild Markov (markovify) models, bot-friend ping replies (`BOT_FRIENDS`), meme/motivator generation with Pillow. Largest cog (~1200 lines).
- `cogs/captcha.py` — generates captcha images with Pillow from words in the Markov corpora.
- `cogs/morning.py` — daily "morning" post: scrapes + generates day/month content, Hungarian namedays (`res/namedays_hu_mmdd.json`).
- `cogs/soma.py` — gambling/cooldown game; SQLite tables created in `main.py` (`soma`, `somacd`, `soma_tries`, `soma_wins`).
- `cogs/holidays.py` — Hungarian public holidays via `utils/holiday_cache.py` (caches API to `usr/holidays.json`, timezone `Europe/Budapest`).
- Others: `teletal` (Teletál food-order reminders), `steam_release` (game lookup and Discord event creation on request, including custom dates; no continuous release tracking), `rolepicker`, `kifogas` (excuses from `res/kifogasok.json`), `linkfixer` (X/IG/Reddit/TikTok/Facebook embed links + language suffix buttons), `cz`, `grok`, `vocardcleanup`, `util` (owner commands: `r!sync`, musicbot control).
- Each cog defines a `commands.Cog` subclass + `async def setup(bot)` at module bottom.

## Conventions
- Casual Hungarian tone in user-facing strings and comments; keep it.
- Logs to `ruinbot.log` + console, configured once in `main.py` (discord logger silenced to WARNING). Prefer `logging.getLogger(__name__)`-style loggers for new runtime messages; many existing cogs still use `print()`.
- No configured type checker or linter; type annotations are used in parts of the code. Follow the surrounding style.
- `allowed_mentions` is restrictive (roles/users/everyone off) at the bot level — don't re-enable broad mentions.
- Don't commit `.env`, `ruinbot.log`, or `usr/` state (see `.gitignore`).
