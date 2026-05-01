# Schwab Signal Bot

Text-signal driven options bot for Schwab workflows.

This repo now includes the standalone pieces the bot needs to execute:

- `signal_bot/` — signal parser, option/futures monitoring, entry/scale-out/stop lifecycle
- `broker_api/` — minimal Schwab auth/client/rate-limit wrapper around `schwab-py`
- `config/` — environment-driven settings
- `execution/tests/` — parser/lifecycle tests

See [`docs/signal_bot.md`](docs/signal_bot.md) for usage and lifecycle details.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` locally:

```bash
SCHWAB_API_KEY=...
SCHWAB_API_SECRET=...
SCHWAB_ACCOUNT_ID=...
SCHWAB_TOKEN_PATH=~/.schwab_token.json
PAPER_TRADING=True
```

`PAPER_TRADING` defaults to `True` in this public repo for safety. Set `PAPER_TRADING=False` only when you intentionally want live Schwab orders.

## Schwab auth

Generate/save a `schwab-py` token file once using your Schwab developer app credentials and callback URL. The bot will load it from `SCHWAB_TOKEN_PATH`.

A quick auth check can be done with:

```bash
python - <<'PY'
from broker_api.auth_manager import auth_manager
print(auth_manager.token_info() if auth_manager else 'auth manager unavailable; check .env')
PY
```

## Safety

Do not commit `.env` files, Schwab token files, account IDs, logs, or runtime trade snapshots. This repo intentionally ignores those artifacts.
