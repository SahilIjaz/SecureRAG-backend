"""Curated, hand-written metadata for the tenant-facing Model page — display
name and a plain-language benefit tag per selectable provider/model. This is
editorial copy, not live-measured telemetry: no real-time latency
benchmarking is being done anywhere in this codebase, so "fast"/"accurate"
claims below are guidance, not a measured SLA. Adding a provider later means
adding one entry here, nothing else.
"""

from __future__ import annotations

CATALOG: dict[str, dict] = {
    "gemini": {
        "displayName": "Gemini",
        "tagline": "Fastest & cheapest",
        "description": "Google's Gemini Flash models — quick replies, low cost, the default for every new chatbot.",
    },
    "anthropic": {
        "displayName": "Claude",
        "tagline": "Best accuracy",
        "description": "Anthropic's Claude — stronger reasoning and nuance for businesses that want the highest answer quality.",
    },
}


def get_catalog_entry(provider: str) -> dict:
    return CATALOG.get(provider, {"displayName": provider, "tagline": "", "description": ""})
