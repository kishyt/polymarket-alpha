"""
analysis/base_rates.py

Historical base rate database for prediction market event types.

Strategy: Compare market prices to empirical historical frequencies.
When the market price diverges significantly from base rates, that's a signal.

Only runs when settings.ENABLE_BASE_RATES = True.

Event type classification uses keyword matching on the market question.
In production, add a Claude-powered classifier for better accuracy.

Base rate data sources to populate:
- Political/electoral: historical election results by type and conditions
- FDA/biotech: approval rates by phase, indication, and sponsor tier
- Economic: NBER recession declarations, Fed meeting outcomes
- Legal: SCOTUS ruling patterns, regulatory outcomes
- Sports: win rates by seed/ranking in relevant competitions
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    ELECTION_US_PRESIDENTIAL = "election_us_presidential"
    ELECTION_US_CONGRESSIONAL = "election_us_congressional"
    ELECTION_INTERNATIONAL = "election_international"
    FDA_APPROVAL = "fda_approval"
    FDA_APPROVAL_PHASE3 = "fda_approval_phase3"
    ECONOMIC_RECESSION = "economic_recession"
    FED_RATE_DECISION = "fed_rate_decision"
    LEGAL_SUPREME_COURT = "legal_supreme_court"
    GEOPOLITICAL_EVENT = "geopolitical_event"
    SPORTS_CHAMPIONSHIP = "sports_championship"
    CRYPTO_PRICE = "crypto_price"
    UNKNOWN = "unknown"


@dataclass
class BaseRate:
    event_type: EventType
    base_probability: float          # historical frequency (0.0 to 1.0)
    sample_size: int                 # number of historical events in the sample
    condition_description: str       # what conditions this rate applies to
    source: str                      # data source
    confidence: str                  # "high", "medium", "low"
    notes: str = ""


@dataclass
class BaseRateLookup:
    event_type: EventType
    base_rate: Optional[BaseRate]
    market_price: float
    divergence: float                # market_price - base_probability (signed)
    divergence_significant: bool     # |divergence| > threshold
    signal_direction: str            # "fade_market_up", "fade_market_down", "neutral"
    confidence: str


# ── Base rate database ────────────────────────────────────────────────────────
# Each entry represents a set of conditions and the historical outcome frequency.
# These are starting-point values — replace with rigorous backtested data.

BASE_RATE_DATABASE: dict[EventType, list[BaseRate]] = {

    EventType.ELECTION_US_PRESIDENTIAL: [
        BaseRate(
            event_type=EventType.ELECTION_US_PRESIDENTIAL,
            base_probability=0.52,
            sample_size=24,
            condition_description="Incumbent party with positive GDP growth year prior",
            source="US presidential election history 1900-2024",
            confidence="medium",
            notes="Economic voting model. Strong predictor but not deterministic.",
        ),
        BaseRate(
            event_type=EventType.ELECTION_US_PRESIDENTIAL,
            base_probability=0.38,
            sample_size=24,
            condition_description="Incumbent party with negative GDP growth year prior",
            source="US presidential election history 1900-2024",
            confidence="medium",
        ),
    ],

    EventType.FDA_APPROVAL: [
        BaseRate(
            event_type=EventType.FDA_APPROVAL,
            base_probability=0.85,
            sample_size=500,
            condition_description="BLA/NDA with priority review designation",
            source="FDA approval rate data 2015-2024",
            confidence="high",
        ),
        BaseRate(
            event_type=EventType.FDA_APPROVAL,
            base_probability=0.75,
            sample_size=1200,
            condition_description="Standard NDA/BLA review",
            source="FDA approval rate data 2015-2024",
            confidence="high",
        ),
    ],

    EventType.FDA_APPROVAL_PHASE3: [
        BaseRate(
            event_type=EventType.FDA_APPROVAL_PHASE3,
            base_probability=0.58,
            sample_size=800,
            condition_description="Phase 3 trial succeeds (all indications)",
            source="Clinical trial success rates, BIO/Informa 2011-2020",
            confidence="high",
        ),
        BaseRate(
            event_type=EventType.FDA_APPROVAL_PHASE3,
            base_probability=0.72,
            sample_size=200,
            condition_description="Phase 3 trial — oncology indication",
            source="Clinical trial success rates, BIO/Informa 2011-2020",
            confidence="medium",
        ),
    ],

    EventType.ECONOMIC_RECESSION: [
        BaseRate(
            event_type=EventType.ECONOMIC_RECESSION,
            base_probability=0.15,
            sample_size=80,
            condition_description="Any given year in the US economy",
            source="NBER recession dating, 1945-2024",
            confidence="high",
            notes="NBER declares recessions with a significant lag (6-18 months). "
                  "Markets price on narrative, not NBER's lagged technical definition.",
        ),
    ],

    EventType.FED_RATE_DECISION: [
        BaseRate(
            event_type=EventType.FED_RATE_DECISION,
            base_probability=0.89,
            sample_size=150,
            condition_description="Fed decision matches market pricing >75% implied probability",
            source="Fed funds futures vs outcomes 2000-2024",
            confidence="high",
            notes="When market is >75% confident, Fed almost always confirms. "
                  "Edge is in early-cycle pricing before CME futures converge.",
        ),
    ],
}


# ── Keyword classifier ─────────────────────────────────────────────────────────

KEYWORD_RULES: list[tuple[list[str], EventType]] = [
    (["president", "presidential", "white house", "electoral college"], EventType.ELECTION_US_PRESIDENTIAL),
    (["congress", "senate", "house of representatives", "midterm"], EventType.ELECTION_US_CONGRESSIONAL),
    (["election", "prime minister", "chancellor", "parliament"], EventType.ELECTION_INTERNATIONAL),
    (["fda", "nda", "bla", "approval", "drug approval", "accelerated approval"], EventType.FDA_APPROVAL),
    (["phase 3", "phase iii", "clinical trial", "pivotal trial"], EventType.FDA_APPROVAL_PHASE3),
    (["recession", "gdp contraction", "nber"], EventType.ECONOMIC_RECESSION),
    (["federal reserve", "fed", "fomc", "interest rate", "rate hike", "rate cut"], EventType.FED_RATE_DECISION),
    (["supreme court", "scotus", "ruling", "decision"], EventType.LEGAL_SUPREME_COURT),
]


class BaseRateEngine:
    """
    Looks up historical base rates for a market question and
    calculates the divergence from the current market price.
    """

    SIGNIFICANCE_THRESHOLD = 0.07   # 7 probability points divergence = significant

    def classify_event(self, question: str) -> EventType:
        """Classify the event type from the market question using keyword matching."""
        lower = question.lower()
        for keywords, event_type in KEYWORD_RULES:
            if any(kw in lower for kw in keywords):
                return event_type
        return EventType.UNKNOWN

    def get_base_rate(
        self,
        event_type: EventType,
        question: str,
        additional_context: str = "",
    ) -> Optional[BaseRate]:
        """
        Look up the most relevant base rate for an event type.
        In production, this should do more sophisticated condition matching.
        """
        rates = BASE_RATE_DATABASE.get(event_type, [])
        if not rates:
            return None
        # Simple: return the first (most general) matching rate
        # TODO: add condition matching based on question text
        return rates[0]

    def lookup(
        self,
        question: str,
        market_price: float,
        description: str = "",
    ) -> Optional[BaseRateLookup]:
        """
        Main entry point. Returns a lookup result or None if strategy is disabled
        or no base rate exists for this event type.
        """
        if not settings.ENABLE_BASE_RATES:
            logger.debug("Base rate strategy disabled", question=question[:60])
            return None

        event_type = self.classify_event(question)
        if event_type == EventType.UNKNOWN:
            logger.debug("No event type match", question=question[:60])
            return None

        base_rate = self.get_base_rate(event_type, question, description)
        if not base_rate:
            logger.debug("No base rate found", event_type=event_type)
            return None

        divergence = market_price - base_rate.base_probability
        significant = abs(divergence) > self.SIGNIFICANCE_THRESHOLD

        if divergence > self.SIGNIFICANCE_THRESHOLD:
            signal_direction = "fade_market_down"   # market is too high vs base rate
        elif divergence < -self.SIGNIFICANCE_THRESHOLD:
            signal_direction = "fade_market_up"     # market is too low vs base rate
        else:
            signal_direction = "neutral"

        lookup = BaseRateLookup(
            event_type=event_type,
            base_rate=base_rate,
            market_price=market_price,
            divergence=divergence,
            divergence_significant=significant,
            signal_direction=signal_direction,
            confidence=base_rate.confidence,
        )

        if significant:
            logger.info(
                "Base rate divergence detected",
                question=question[:80],
                event_type=event_type,
                base_rate=base_rate.base_probability,
                market_price=market_price,
                divergence=f"{divergence:+.2%}",
                direction=signal_direction,
            )

        return lookup

    def fair_value_delta(self, lookup: BaseRateLookup) -> float:
        """
        Convert lookup result to a fair value delta in [-0.20, +0.20].
        Positive = we think YES is underpriced.
        """
        if not lookup.divergence_significant:
            return 0.0

        confidence_multiplier = {
            "high": 1.0,
            "medium": 0.65,
            "low": 0.35,
        }.get(lookup.confidence, 0.35)

        # Pull toward base rate — positive divergence means market > base rate
        # so we want a negative adjustment (the market is overpriced)
        raw_delta = -lookup.divergence * confidence_multiplier

        # Cap at ±20 probability points
        return max(-0.20, min(0.20, raw_delta))
