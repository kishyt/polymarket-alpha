"""
analysis/resolution_parser.py

Uses Claude to extract exact resolution conditions from Polymarket market descriptions.
This is the core of the Resolution Criteria strategy.

Only runs when settings.ENABLE_RESOLUTION_CRITERIA = True.
Results are cached in the DB and only re-run when market description changes.

The key insight: most traders price the *spirit* of a question, not its
*literal* resolution criteria. Claude finds the gap.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

import anthropic

from config.settings import settings

logger = logging.getLogger(__name__)

RESOLUTION_ANALYSIS_PROMPT = """You are an expert prediction market analyst specialising in 
resolution criteria interpretation. Your job is to find the gap between what traders think 
a market is asking and what its literal resolution criteria actually require.

Given a prediction market question and its resolution criteria, extract the following as 
structured JSON:

{
  "yes_conditions": [
    "Exact condition 1 that must be true for YES resolution",
    "Exact condition 2..."
  ],
  "no_conditions": [
    "Exact condition 1 that must be true for NO resolution",
    "..."
  ],
  "resolution_oracle": {
    "source": "The specific source/organisation/publication that will determine resolution",
    "method": "How they will make the determination",
    "timing": "When/how quickly after the event the oracle typically acts"
  },
  "trader_intent_vs_literal": {
    "what_traders_likely_assume": "What the typical trader thinks this market is asking",
    "what_it_actually_requires": "What the literal criteria actually requires",
    "divergence_exists": true or false
  },
  "steelman_no": [
    "Strongest argument 1 that this resolves NO even if the event appears to happen",
    "Strongest argument 2..."
  ],
  "steelman_yes": [
    "Strongest argument 1 that this resolves YES even if the event appears not to happen",
    "..."
  ],
  "key_risks": [
    {
      "risk": "Description of resolution risk",
      "direction": "biases_toward_yes or biases_toward_no or uncertain",
      "magnitude": "small, medium, or large"
    }
  ],
  "fair_value_adjustment": {
    "direction": "increase, decrease, or neutral",
    "magnitude_pp": "estimated adjustment in probability points (e.g. 3 means +3pp)",
    "reasoning": "Why this adjustment is warranted given the criteria"
  },
  "confidence": "high, medium, or low — confidence in this analysis"
}

Return ONLY the JSON object. No preamble, no explanation, no markdown fences."""


@dataclass
class ResolutionAnalysis:
    market_id: str
    description_hash: str
    yes_conditions: list[str]
    no_conditions: list[str]
    resolution_oracle: dict
    trader_intent_vs_literal: dict
    steelman_no: list[str]
    steelman_yes: list[str]
    key_risks: list[dict]
    fair_value_adjustment: dict
    confidence: str
    raw_response: str
    tokens_used: int


class ResolutionParser:
    """
    Parses resolution criteria using Claude.
    Implements caching via a simple in-memory store (swap for DB in production).
    """

    def __init__(self):
        if settings.ENABLE_RESOLUTION_CRITERIA:
            settings.validate_llm_config()
            self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        else:
            self._client = None

        # In-memory cache: description_hash -> ResolutionAnalysis
        # In production, back this with the signal_reasoning DB table
        self._cache: dict[str, ResolutionAnalysis] = {}

    def _description_hash(self, question: str, criteria: str) -> str:
        """Hash of question + criteria to detect changes."""
        content = f"{question}|||{criteria}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def analyse(
        self,
        market_id: str,
        question: str,
        description: str,
        resolution_criteria: str,
    ) -> Optional[ResolutionAnalysis]:
        """
        Analyse a market's resolution criteria.
        Returns None if the strategy is disabled.
        Returns cached result if description hasn't changed.
        """
        if not settings.ENABLE_RESOLUTION_CRITERIA:
            logger.debug("Resolution criteria strategy disabled, skipping", market_id=market_id)
            return None

        desc_hash = self._description_hash(question, resolution_criteria)

        # Return cached result if available
        if desc_hash in self._cache:
            logger.debug("Resolution analysis cache hit", market_id=market_id)
            return self._cache[desc_hash]

        logger.info("Running resolution analysis", market_id=market_id)

        user_content = f"""Market Question: {question}

Market Description: {description}

Resolution Criteria: {resolution_criteria}

Analyse this market's resolution criteria and return the structured JSON."""

        try:
            response = await self._client.messages.create(
                model=settings.ANTHROPIC_MODEL,
                max_tokens=settings.ANTHROPIC_MAX_TOKENS,
                system=RESOLUTION_ANALYSIS_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as e:
            logger.error("Anthropic API call failed", market_id=market_id, error=str(e))
            return None

        raw_text = response.content[0].text
        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse Claude response as JSON",
                market_id=market_id,
                error=str(e),
                raw=raw_text[:200],
            )
            return None

        analysis = ResolutionAnalysis(
            market_id=market_id,
            description_hash=desc_hash,
            yes_conditions=parsed.get("yes_conditions", []),
            no_conditions=parsed.get("no_conditions", []),
            resolution_oracle=parsed.get("resolution_oracle", {}),
            trader_intent_vs_literal=parsed.get("trader_intent_vs_literal", {}),
            steelman_no=parsed.get("steelman_no", []),
            steelman_yes=parsed.get("steelman_yes", []),
            key_risks=parsed.get("key_risks", []),
            fair_value_adjustment=parsed.get("fair_value_adjustment", {}),
            confidence=parsed.get("confidence", "low"),
            raw_response=raw_text,
            tokens_used=tokens_used,
        )

        self._cache[desc_hash] = analysis
        logger.info(
            "Resolution analysis complete",
            market_id=market_id,
            confidence=analysis.confidence,
            adjustment_direction=analysis.fair_value_adjustment.get("direction"),
            adjustment_pp=analysis.fair_value_adjustment.get("magnitude_pp"),
            tokens=tokens_used,
        )
        return analysis

    def fair_value_delta(self, analysis: ResolutionAnalysis) -> float:
        """
        Convert the qualitative fair value adjustment to a numeric delta.
        Returns a value in [-0.15, +0.15] to apply to the market price.
        """
        adj = analysis.fair_value_adjustment
        direction = adj.get("direction", "neutral")
        magnitude_pp = float(adj.get("magnitude_pp", 0))

        # Cap at 15 probability points — beyond that, it's speculative
        magnitude_pp = max(-15.0, min(15.0, magnitude_pp))

        confidence_multiplier = {
            "high": 1.0,
            "medium": 0.6,
            "low": 0.3,
        }.get(analysis.confidence, 0.3)

        delta = (magnitude_pp / 100.0) * confidence_multiplier

        if direction == "decrease":
            delta = -abs(delta)
        elif direction == "increase":
            delta = abs(delta)
        else:
            delta = 0.0

        return delta

    def invalidate_cache(self, description_hash: str):
        """Remove a cached analysis (e.g. when market description updates)."""
        self._cache.pop(description_hash, None)
