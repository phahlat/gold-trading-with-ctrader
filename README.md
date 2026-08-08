# Gold Bot Workspace

This repository now contains a single gold-focused trading bot.

## Primary Documentation

1. docs/RUNBOOK.md: operational runbook for backtest and live runs
2. docs/CONFIGURATION.md: environment variable setup and presets
3. bot/README.md: package-specific usage notes

## Quick Start

```bat
cd /d c:\CodePlay\gold-trading-bot
python -m venv .venv
.\.venv\Scripts\activate.bat
pip install -r requirements.txt
copy bot\.env.example bot\.env
```

## Documentation set

- [docs/STRATEGIES.md](docs/STRATEGIES.md): strategy behavior and recommended presets
- [docs/RUNBOOK.md](docs/RUNBOOK.md): backtest and live-run commands for each strategy
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): grouped environment variables and detailed comments
- [bot/README.md](bot/README.md): package-level overview and usage

## Root Launcher Examples

```bat
cd /d c:\CodePlay\gold-trading-bot

REM Backtest
.\.venv\Scripts\python.exe .\main.py --mode backtest --no-trade --symbols XAUUSD --strategy trend_following --backtest-data-dir bot\backtest\data

REM Dry run
.\.venv\Scripts\python.exe .\main.py --mode live --no-trade --symbols XAUUSD --strategy trend_following --no-plot --max-cycles 1
```

## Docker

Build and run with Docker Compose:

```bash
docker compose up --build
```

Run in DEBUG mode to show cTrader connection endpoint strings and reconnect parameters in logs:

```bash
LOG_LEVEL=DEBUG docker compose up --build
```

The container writes runtime artifacts to host-mounted paths:

1. `./logs` -> `/app/logs`
2. `./backtest/results` -> `/app/backtest/results`
3. `./position-data` -> `/app/position-data`

The Compose service includes:

1. A live heartbeat healthcheck based on `logs/live_heartbeat.json`
2. SIGTERM-based graceful shutdown with a 45-second stop grace period

Stop and remove the container:

```bash
docker compose down
```

## Safety

1. Use --no-trade for first-time validation and logic checks.
2. Validate settings on small data first, then scale to full backtests.
3. Keep cTrader credentials private and never commit the local bot/.env file.
