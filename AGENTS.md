# Polymarket Alpha System — Claude Code Agent Instructions

## Project Purpose
A modular prediction market alpha-generation system targeting Polymarket.
It identifies mispriced markets using three independently toggleable strategies:
1. **Resolution Criteria Analysis** — Claude parses exact resolution conditions to find where traders misread the literal criteria
2. **Base Rate Engine** — Historical frequency databases benchmark market prices against empirical base rates
3. **Cross-Platform Latency Arb** — Price discrepancies across Polymarket, Metaculus, Manifold, and Kalshi

All three strategies can be enabled/disabled independently via `config/settings.py`.

## Tech Stack
- **Language**: Python 3.12
- **Database**: PostgreSQL (via asyncpg + SQLAlchemy async)
- **API framework**: FastAPI + uvicorn
- **LLM**: Anthropic SDK (claude-sonnet-4-5) — used only when resolution_criteria strategy is enabled
- **Scheduler**: APScheduler
- **WebSocket client**: websockets library
- **HTTP client**: httpx (async)
- **Config**: pydantic-settings (reads from .env)

## Directory Structure
```
polymarket-alpha/
├── AGENTS.md                  ← you are here
├── README.md
├── .env.example
├── requirements.txt
├── config/
│   └── settings.py            ← strategy toggles + all config
├── ingestion/
│   ├── clob_client.py         ← Polymarket CLOB REST + WebSocket
│   ├── market_store.py        ← DB persistence for markets + order books
│   └── platform_clients.py   ← Metaculus, Manifold, Kalshi clients
├── analysis/
│   ├── resolution_parser.py   ← Claude-powered criteria extraction
│   ├── base_rates.py          ← Historical frequency DB + lookups
│   └── sentiment.py           ← News tone scoring vs base rates
├── signals/
│   ├── engine.py              ← Orchestrates enabled strategies → signals
│   ├── fair_value.py          ← Probability synthesis model
│   ├── kelly.py               ← Kelly criterion position sizing
│   └── correlation.py         ← Cross-market consistency checker
├── execution/
│   ├── order_manager.py       ← CLOB signed order placement
│   └── position_tracker.py   ← P&L + open exposure tracking
├── dashboard/
│   ├── app.py                 ← FastAPI app
│   ├── routes.py              ← API routes
│   └── templates/             ← Jinja2 HTML templates
└── tests/
    ├── test_resolution_parser.py
    ├── test_base_rates.py
    ├── test_signals.py
    └── test_clob_client.py
```

## Key Conventions
- All async — use `async`/`await` throughout; no blocking calls in hot paths
- All Claude API calls log the full reasoning trace to `signal_reasoning` table
- Never place orders > $50 without `REQUIRE_HUMAN_CONFIRMATION=true` in .env
- Strategy flags are checked at runtime — disabling a strategy stops its compute, not its data collection
- Use structured logging (structlog) with `market_id` on every log line
- Errors in one strategy must not crash others — each strategy runs in isolated try/except
- All monetary values stored as integers (cents/basis points) to avoid float precision issues

## Strategy Flag Behaviour
```python
# In config/settings.py
ENABLE_RESOLUTION_CRITERIA: bool = True   # Uses Anthropic API — has cost
ENABLE_BASE_RATES: bool = True            # Pure DB lookups — free
ENABLE_LATENCY_ARB: bool = True           # Polls external platforms — rate limit aware
```
When a strategy is disabled:
- Its signal contribution is set to None in the Signal dataclass
- The fair value model uses only enabled strategy inputs
- The dashboard clearly shows which strategies contributed to each signal

## Database Schema
Run `python -m scripts.init_db` to create all tables.

Tables:
- `markets` — Polymarket market metadata + resolution criteria
- `order_books` — Timestamped order book snapshots
- `platform_prices` — Cross-platform price snapshots (for latency arb)
- `base_rate_events` — Historical event frequency records
- `signals` — Generated trade signals with edge estimates
- `signal_reasoning` — Full Claude reasoning traces (JSONB)
- `positions` — Open and closed positions
- `trades` — Executed trade log

## Running the System
```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in env vars
cp .env.example .env

# Initialise database
python -m scripts.init_db

# Run ingestion + signal engine
python -m ingestion.clob_client &
python -m signals.engine &

# Run dashboard
uvicorn dashboard.app:app --reload --port 8000
```

## When Building New Modules
1. Check `config/settings.py` for the relevant feature flag before implementing
2. All DB access goes through repository classes in `*_store.py` files — no raw SQL in business logic
3. Claude API calls always use the structured output pattern in `analysis/resolution_parser.py` as a template
4. New strategies must implement the `BaseStrategy` protocol in `signals/engine.py`
5. Write tests in `tests/` — mock the Anthropic client and CLOB API with `pytest-mock`

## Polymarket CLOB API Reference
- Base URL: `https://clob.polymarket.com`
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Markets list: `GET /markets?next_cursor=<cursor>` (paginated)
- Order book: `GET /book?token_id=<token_id>`
- Trades: `GET /trades?market=<condition_id>`
- For order placement: requires ECDSA-signed requests via `py-clob-client`
- Gamma API (market metadata): `https://gamma-api.polymarket.com/markets`

## External Platform APIs
- **Metaculus**: `https://www.metaculus.com/api2/questions/` — public, no auth
- **Manifold**: `https://api.manifold.markets/v0/markets` — public read, API key for writes
- **Kalshi**: `https://trading-api.kalshi.com/trade-api/v2/markets` — requires auth

## Important Constraints
- Polymarket WebSocket drops connections — always implement exponential backoff reconnect
- Rate limit external platform APIs — max 1 req/sec per platform
- Anthropic API calls are expensive at scale — cache resolution analysis per market, invalidate on description change
- Never store private keys in code — use .env only, and ideally a secrets manager in prod
