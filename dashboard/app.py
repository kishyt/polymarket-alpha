"""
dashboard/app.py

FastAPI dashboard for the Polymarket Alpha system.

Features:
- Live signal feed with edge estimates
- Strategy toggle controls (enable/disable each strategy at runtime)
- Per-signal breakdown showing each strategy's contribution
- Position tracking

Run with:
    uvicorn dashboard.app:app --reload --port 8000
"""

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from config.settings import settings
from signals.engine import SignalEngine, Signal

# ── Global state ──────────────────────────────────────────────────────────────

engine: Optional[SignalEngine] = None
engine_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, engine_task
    engine = SignalEngine()
    engine_task = asyncio.create_task(engine.run())
    yield
    if engine_task:
        engine_task.cancel()


app = FastAPI(
    title="Polymarket Alpha Dashboard",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "strategies": {
            "resolution_criteria": settings.ENABLE_RESOLUTION_CRITERIA,
            "base_rates": settings.ENABLE_BASE_RATES,
            "latency_arb": settings.ENABLE_LATENCY_ARB,
        },
        "weights": settings.strategy_weights(),
    })


@app.get("/api/signals")
async def get_signals(only_significant: bool = True, limit: int = 50):
    """Return current signals as JSON."""
    if not engine:
        return JSONResponse({"signals": [], "error": "Engine not started"})

    signals = engine.get_signals(only_significant=only_significant)[:limit]
    return JSONResponse({
        "signals": [_signal_to_dict(s) for s in signals],
        "total": len(signals),
        "generated_at": time.time(),
        "active_strategies": settings.active_strategies(),
    })


@app.post("/api/strategies/toggle")
async def toggle_strategy(request: Request):
    """
    Toggle a strategy on or off at runtime.
    Body: {"strategy": "resolution_criteria", "enabled": true}
    """
    body = await request.json()
    strategy = body.get("strategy")
    enabled = body.get("enabled", True)

    valid_strategies = ["resolution_criteria", "base_rates", "latency_arb"]
    if strategy not in valid_strategies:
        return JSONResponse(
            {"error": f"Unknown strategy. Valid: {valid_strategies}"},
            status_code=400,
        )

    # Update the settings object in place
    # Note: in production, use a proper config store (Redis/DB) for persistence
    flag_map = {
        "resolution_criteria": "ENABLE_RESOLUTION_CRITERIA",
        "base_rates": "ENABLE_BASE_RATES",
        "latency_arb": "ENABLE_LATENCY_ARB",
    }
    setattr(settings, flag_map[strategy], enabled)

    return JSONResponse({
        "strategy": strategy,
        "enabled": enabled,
        "active_strategies": settings.active_strategies(),
        "weights": settings.strategy_weights(),
    })


@app.get("/api/strategies/status")
async def strategy_status():
    """Return current strategy enable/disable state and weights."""
    return JSONResponse({
        "strategies": {
            "resolution_criteria": {
                "enabled": settings.ENABLE_RESOLUTION_CRITERIA,
                "weight": settings.WEIGHT_RESOLUTION_CRITERIA,
                "normalised_weight": settings.strategy_weights().get("resolution_criteria"),
                "description": "Claude analyses exact resolution conditions",
                "has_api_cost": True,
            },
            "base_rates": {
                "enabled": settings.ENABLE_BASE_RATES,
                "weight": settings.WEIGHT_BASE_RATES,
                "normalised_weight": settings.strategy_weights().get("base_rates"),
                "description": "Historical frequency benchmarks vs market price",
                "has_api_cost": False,
            },
            "latency_arb": {
                "enabled": settings.ENABLE_LATENCY_ARB,
                "weight": settings.WEIGHT_LATENCY_ARB,
                "normalised_weight": settings.strategy_weights().get("latency_arb"),
                "description": "Cross-platform price discrepancies",
                "has_api_cost": False,
            },
        },
        "active_strategies": settings.active_strategies(),
    })


@app.get("/api/signal/{market_id}")
async def get_signal_detail(market_id: str):
    """Return detailed signal breakdown for a single market."""
    if not engine:
        return JSONResponse({"error": "Engine not started"}, status_code=503)

    all_signals = engine.get_signals(only_significant=False)
    signal = next((s for s in all_signals if s.market_id == market_id), None)

    if not signal:
        return JSONResponse({"error": "Market not found"}, status_code=404)

    return JSONResponse(_signal_to_dict(signal, detailed=True))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal_to_dict(signal: Signal, detailed: bool = False) -> dict:
    base = {
        "market_id": signal.market_id,
        "question": signal.question,
        "generated_at": signal.generated_at,
        "market_price": signal.market_price,
        "best_bid": signal.best_bid,
        "best_ask": signal.best_ask,
        "fair_value": signal.fair_value,
        "edge": signal.edge,
        "edge_significant": signal.edge_significant,
        "recommended_side": signal.recommended_side,
        "kelly_position_usd": signal.kelly_position_usd,
        "active_strategies": signal.active_strategies,
        "summary": signal.summary,
        "priority_score": signal.priority_score,
        "strategies": {
            "resolution_criteria": _contrib_to_dict(signal.resolution_criteria),
            "base_rates": _contrib_to_dict(signal.base_rates),
            "latency_arb": _contrib_to_dict(signal.latency_arb),
        },
    }

    if detailed:
        # Add raw strategy outputs (without the full Anthropic response for brevity)
        rc = signal.resolution_criteria.raw_output
        if rc:
            base["resolution_detail"] = {
                "yes_conditions": rc.yes_conditions,
                "no_conditions": rc.no_conditions,
                "steelman_no": rc.steelman_no,
                "key_risks": rc.key_risks,
                "confidence": rc.confidence,
                "trader_intent_vs_literal": rc.trader_intent_vs_literal,
            }

        br = signal.base_rates.raw_output
        if br:
            base["base_rate_detail"] = {
                "event_type": br.event_type,
                "base_probability": br.base_rate.base_probability if br.base_rate else None,
                "divergence": br.divergence,
                "signal_direction": br.signal_direction,
                "source": br.base_rate.source if br.base_rate else None,
            }

        la = signal.latency_arb.raw_output
        if la:
            base["latency_arb_detail"] = [
                {
                    "platform": d.external_platform,
                    "external_price": d.external_price,
                    "discrepancy": d.discrepancy,
                    "signal_direction": d.signal_direction,
                }
                for d in la
            ]

    return base


def _contrib_to_dict(contrib) -> dict:
    return {
        "enabled": contrib.enabled,
        "ran": contrib.ran,
        "fair_value_delta": contrib.fair_value_delta,
        "error": contrib.error,
    }
