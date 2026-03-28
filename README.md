# Polymarket Alpha

A modular prediction market alpha system targeting Polymarket, with three independently toggleable signal strategies.

## Quickstart

```bash
# 1. Clone and install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL and ANTHROPIC_API_KEY (if using resolution criteria)

# 3. Start the system
uvicorn dashboard.app:app --reload --port 8000

# 4. Open the dashboard
open http://localhost:8000
```

## Strategies

Toggle each strategy via the dashboard UI or `.env`:

| Strategy | Flag | Cost | Description |
|---|---|---|---|
| Resolution Criteria | `ENABLE_RESOLUTION_CRITERIA` | Anthropic API | Claude parses exact resolution conditions, finds where traders misread the literal criteria |
| Base Rates | `ENABLE_BASE_RATES` | Free | Compares market prices to historical frequencies (elections, FDA, macro) |
| Latency Arb | `ENABLE_LATENCY_ARB` | Free | Monitors Metaculus, Manifold, Kalshi for price discrepancies |

When strategies are disabled, their weight is redistributed to the remaining active strategies automatically.

## Architecture

```
ingestion/           — Polymarket CLOB + external platform data
analysis/            — Strategy logic (resolution parser, base rates, sentiment)
signals/             — Fair value synthesis, Kelly sizing, signal ranking
execution/           — Order placement (manual confirmation required by default)
dashboard/           — FastAPI + HTML dashboard with live signal feed
config/settings.py   — All config + strategy toggles
```

## Adding a New Strategy

1. Create `analysis/my_strategy.py` implementing the analysis logic
2. Add a feature flag to `config/settings.py`
3. Add a `_run_my_strategy()` method to `signals/engine.py`
4. Include the contribution in the `Signal` dataclass
5. Add the weight to `settings.strategy_weights()`

## Safety

- `REQUIRE_HUMAN_CONFIRMATION=true` — orders above threshold require manual approval
- `MAX_POSITION_USD` — hard cap on any single position
- `KELLY_FRACTION=0.25` — quarter-Kelly applied by default
- Private keys are `.env` only — never commit them
