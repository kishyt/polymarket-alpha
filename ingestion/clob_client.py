"""
ingestion/clob_client.py

Polymarket CLOB API client — REST polling + WebSocket streaming.

Responsibilities:
- Fetch and paginate all active markets from the Gamma API
- Stream live order book updates and trades via WebSocket
- Reconnect with exponential backoff on disconnect
- Emit events to an asyncio Queue consumed by the signal engine

Usage:
    client = CLOBClient()
    await client.start()  # runs forever, reconnects on drop
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import settings

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Market:
    condition_id: str
    question: str
    description: str
    resolution_criteria: str
    end_date_iso: str
    tokens: list[dict]          # [{token_id, outcome}, ...]
    volume_24h: float
    active: bool
    closed: bool
    archived: bool


@dataclass
class OrderBookSnapshot:
    market_id: str
    token_id: str
    timestamp: float
    bids: list[tuple[float, float]]   # [(price, size), ...]
    asks: list[tuple[float, float]]
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid_price: Optional[float]


@dataclass
class TradeEvent:
    market_id: str
    token_id: str
    timestamp: float
    price: float
    size: float
    side: str   # "BUY" or "SELL"
    taker_order_id: str


# ── REST client ───────────────────────────────────────────────────────────────

class CLOBRestClient:
    """Handles all REST interactions with the Polymarket CLOB and Gamma APIs."""

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._http.aclose()

    async def fetch_all_markets(self, min_volume: float = None) -> list[Market]:
        """
        Paginate through all active markets from the Gamma API.
        Filters by minimum 24h volume if specified.
        """
        min_volume = min_volume or settings.MIN_MARKET_VOLUME_USD
        markets = []
        next_cursor = ""

        while True:
            params = {"active": "true", "closed": "false", "limit": 100}
            if next_cursor:
                params["next_cursor"] = next_cursor

            try:
                resp = await self._http.get(
                    f"{settings.GAMMA_API_URL}/markets", params=params
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error("Failed to fetch markets", error=str(e))
                break

            for raw in data.get("data", []):
                try:
                    market = self._parse_market(raw)
                    if market.volume_24h >= min_volume:
                        markets.append(market)
                except Exception as e:
                    logger.warning(
                        "Failed to parse market", market_id=raw.get("conditionId"), error=str(e)
                    )

            next_cursor = data.get("next_cursor", "")
            if not next_cursor or next_cursor == "LTE=":
                break

            # Respect rate limits
            await asyncio.sleep(0.2)

        logger.info("Fetched markets", count=len(markets))
        return markets[: settings.MAX_TRACKED_MARKETS]

    async def fetch_order_book(self, token_id: str, market_id: str) -> Optional[OrderBookSnapshot]:
        """Fetch current order book for a single token."""
        try:
            resp = await self._http.get(
                f"{settings.CLOB_BASE_URL}/book", params={"token_id": token_id}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch order book", token_id=token_id, error=str(e))
            return None

        bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]

        best_bid = max((p for p, _ in bids), default=None) if bids else None
        best_ask = min((p for p, _ in asks), default=None) if asks else None
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None

        return OrderBookSnapshot(
            market_id=market_id,
            token_id=token_id,
            timestamp=time.time(),
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
        )

    async def fetch_recent_trades(self, condition_id: str, limit: int = 50) -> list[TradeEvent]:
        """Fetch recent trades for a market."""
        try:
            resp = await self._http.get(
                f"{settings.CLOB_BASE_URL}/trades",
                params={"market": condition_id, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch trades", market_id=condition_id, error=str(e))
            return []

        trades = []
        for raw in data:
            try:
                trades.append(TradeEvent(
                    market_id=condition_id,
                    token_id=raw["asset_id"],
                    timestamp=float(raw["timestamp"]),
                    price=float(raw["price"]),
                    size=float(raw["size"]),
                    side=raw["side"],
                    taker_order_id=raw["taker_order_id"],
                ))
            except Exception:
                pass
        return trades

    @staticmethod
    def _parse_market(raw: dict) -> Market:
        return Market(
            condition_id=raw["conditionId"],
            question=raw.get("question", ""),
            description=raw.get("description", ""),
            resolution_criteria=raw.get("resolutionCriteria", ""),
            end_date_iso=raw.get("endDateIso", ""),
            tokens=raw.get("tokens", []),
            volume_24h=float(raw.get("volumeNum24hr", 0) or 0),
            active=raw.get("active", False),
            closed=raw.get("closed", False),
            archived=raw.get("archived", False),
        )


# ── WebSocket client ──────────────────────────────────────────────────────────

class CLOBWebSocketClient:
    """
    Streams live market events from the Polymarket CLOB WebSocket.
    Reconnects with exponential backoff on any disconnect.
    Pushes parsed events to an asyncio Queue.
    """

    MAX_BACKOFF_SECONDS = 60
    INITIAL_BACKOFF_SECONDS = 1

    def __init__(self, event_queue: asyncio.Queue, token_ids: list[str]):
        self._queue = event_queue
        self._token_ids = token_ids
        self._running = False

    async def start(self):
        """Run forever, reconnecting on disconnect."""
        self._running = True
        backoff = self.INITIAL_BACKOFF_SECONDS

        while self._running:
            try:
                await self._connect_and_stream()
                backoff = self.INITIAL_BACKOFF_SECONDS  # reset on clean exit
            except ConnectionClosed as e:
                logger.warning("WebSocket disconnected", reason=str(e), backoff=backoff)
            except Exception as e:
                logger.error("WebSocket error", error=str(e), backoff=backoff)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF_SECONDS)

    async def stop(self):
        self._running = False

    async def _connect_and_stream(self):
        logger.info("Connecting to CLOB WebSocket", url=settings.CLOB_WS_URL)

        async with websockets.connect(settings.CLOB_WS_URL) as ws:
            # Subscribe to order book and trade events for tracked tokens
            subscribe_msg = {
                "auth": {},
                "type": "Market",
                "markets": [],
                "assets_ids": self._token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info("WebSocket subscribed", token_count=len(self._token_ids))

            async for raw_msg in ws:
                try:
                    msg = json.loads(raw_msg)
                    await self._handle_message(msg)
                except Exception as e:
                    logger.warning("Failed to handle WS message", error=str(e))

    async def _handle_message(self, msg: dict):
        event_type = msg.get("event_type")

        if event_type == "book":
            # Full order book snapshot
            await self._queue.put(("book", msg))
        elif event_type == "price_change":
            # Incremental price update
            await self._queue.put(("price_change", msg))
        elif event_type == "last_trade_price":
            await self._queue.put(("trade", msg))


# ── Top-level orchestrator ────────────────────────────────────────────────────

class CLOBClient:
    """
    Orchestrates REST polling + WebSocket streaming.
    Exposes an event stream via async iteration.
    """

    def __init__(self):
        self.rest = CLOBRestClient()
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._ws_client: Optional[CLOBWebSocketClient] = None
        self._markets: list[Market] = []

    async def initialise_markets(self) -> list[Market]:
        """Fetch all markets and return them. Call once at startup."""
        self._markets = await self.rest.fetch_all_markets()
        return self._markets

    async def start_streaming(self):
        """Start WebSocket stream for all tracked markets."""
        all_token_ids = [
            token["token_id"]
            for market in self._markets
            for token in market.tokens
        ]
        self._ws_client = CLOBWebSocketClient(self._event_queue, all_token_ids)
        await self._ws_client.start()

    async def events(self) -> AsyncIterator[tuple[str, dict]]:
        """Async iterator over incoming market events."""
        while True:
            event = await self._event_queue.get()
            yield event

    async def snapshot_all_order_books(self) -> list[OrderBookSnapshot]:
        """
        Poll order books for all tracked markets via REST.
        Used as a fallback/supplement to the WebSocket stream.
        """
        snapshots = []
        for market in self._markets:
            for token in market.tokens:
                snapshot = await self.rest.fetch_order_book(
                    token["token_id"], market.condition_id
                )
                if snapshot:
                    snapshots.append(snapshot)
                await asyncio.sleep(0.05)  # gentle rate limiting
        return snapshots

    async def close(self):
        if self._ws_client:
            await self._ws_client.stop()
        await self.rest.close()
