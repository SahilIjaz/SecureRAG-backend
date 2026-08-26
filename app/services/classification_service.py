"""
Conversation Classification Service.

After every bot answer, this classifies the conversation so the dashboard's
analytics cards reflect real data instead of defaults:

  topic     -> Knowledge gaps + Conversation topics cards
  sentiment -> Customer sentiment + CSAT score cards
  answered  -> Resolution rate card (sets status = "Resolved")

Runs as a fire-and-forget background task (like the document indexer), so the
visitor's reply is never delayed by classification. Uses Haiku with structured
output — cheap enough (~$0.0004/message) to run on every turn, and the schema
guarantees the topic comes from the fixed taxonomy rather than free text
(free-form labels would fragment "billing"/"Billing"/"payments" into separate
knowledge gaps and make aggregation meaningless).
"""

import asyncio
import logging
import uuid
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.conversation import Conversation

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-haiku-4-5"

# Fixed taxonomy — aggregation only works if labels are a closed set.
TOPICS = [
    "Account & billing",
    "Product features",
    "Integrations",
    "Troubleshooting",
    "Pricing",
    "Getting started",
    "Other",
]

SENTIMENTS = ["Positive", "Neutral", "Negative"]

# Shown when a conversation hasn't been classified yet (used by both the
# gaps and topics endpoints so the label never disagrees between cards).
UNCLASSIFIED_TOPIC = "Uncategorized"

_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "enum": TOPICS,
            "description": "Which subject the visitor's question belongs to.",
        },
        "sentiment": {
            "type": "string",
            "enum": SENTIMENTS,
            "description": "The visitor's tone in this conversation.",
        },
        "answered": {
            "type": "boolean",
            "description": (
                "True only if the assistant's reply actually answered the visitor's "
                "question. False if it deflected, said it didn't know, asked to hand "
                "off to a human, or replied with a generic fallback."
            ),
        },
    },
    "required": ["topic", "sentiment", "answered"],
    "additionalProperties": False,
}

def schedule_classification(conversation_id: uuid.UUID) -> None:
    """Kick off background classification for a conversation (non-blocking)."""
    asyncio.create_task(_classify_safe(conversation_id))

async def _classify_safe(conversation_id: uuid.UUID) -> None:
    try:
        await classify_conversation(conversation_id)
    except Exception:
        # Analytics are best-effort — never let a classification failure
        # affect the conversation itself.
        logger.exception("Classification failed for conversation %s", conversation_id)

async def classify_conversation(conversation_id: uuid.UUID) -> Optional[dict]:
    """
    Classify a conversation and persist topic / sentiment / resolved status.
    Returns the classification dict, or None if there was nothing to classify.
    """
    from sqlalchemy.orm import selectinload

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .options(selectinload(Conversation.messages))
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            return None

        transcript = _build_transcript(conversation)
        if not transcript:
            return None

        # Don't let the classifier override a human outcome: once an agent has
        # replied, or the bot escalated via the low-confidence handoff, the
        # status belongs to the humans until they close it out.
        agent_involved = any(m.role == "agent" for m in conversation.messages)
        human_owned = agent_involved or conversation.status == "Handed off"

        data = await _call_classifier(transcript)
        if data is None:
            return None

        conversation.topic = data["topic"]
        conversation.sentiment = data["sentiment"]

        if not human_owned:
            if data["answered"]:
                conversation.status = "Resolved"
                conversation.unresolved_reason = None
            else:
                conversation.status = "Open"
                if not conversation.unresolved_reason:
                    conversation.unresolved_reason = "Low confidence"

        await db.commit()
        logger.info(
            "Classified conversation %s: topic=%s sentiment=%s answered=%s status=%s",
            conversation_id, data["topic"], data["sentiment"], data["answered"],
            conversation.status,
        )
        return data

def _build_transcript(conversation: Conversation, max_turns: int = 12) -> str:
    """Render the last N messages as a plain transcript for the classifier."""
    messages = conversation.messages[-max_turns:]
    lines = []
    for m in messages:
        speaker = {"user": "Visitor", "bot": "Assistant", "agent": "Human agent"}.get(m.role, m.role)
        text = m.text if len(m.text) <= 600 else m.text[:600] + "..."
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)

async def _call_classifier(transcript: str) -> Optional[dict]:
    import json

    from anthropic import AsyncAnthropic

    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping classification")
        return None

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=200,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify this customer-support chat transcript.\n\n"
                    f"TRANSCRIPT:\n{transcript}"
                ),
            }
        ],
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Classifier returned non-JSON: %s", text[:200])
        return None

    # Defensive: the schema constrains these, but never trust blindly.
    if data.get("topic") not in TOPICS:
        data["topic"] = "Other"
    if data.get("sentiment") not in SENTIMENTS:
        data["sentiment"] = "Neutral"
    data["answered"] = bool(data.get("answered"))
    return data
