"""RAG (Retrieval-Augmented Generation) service."""

import asyncio
import logging
import time
import uuid
from typing import AsyncIterator, List

from app.core.embeddings import embed_text
from app.core.vector_store import search_chunks
from app.config import settings

logger = logging.getLogger(__name__)

_genai_client = None

def _get_genai_client():
    """Lazy-cached Gemini client — was previously reconstructed on every chat request."""
    global _genai_client
    if _genai_client is None:
        from google import genai
        _genai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _genai_client

async def retrieve_context_for_query(
    tenant_id: str,
    query: str,
) -> List[str]:
    """
    Retrieve relevant document chunks for a query.

    Args:
        tenant_id: Tenant ID
        query: User query

    Returns:
        List of relevant document chunks
    """
    try:
        logger.info(f"Generating embedding for query")
        query_embedding = await embed_text(query)

        logger.info(f"Searching for similar chunks (top {settings.RAG_SEARCH_TOP_K})")
        similar_chunks = await search_chunks(
            query_embedding,
            tenant_id,
            top_k=settings.RAG_SEARCH_TOP_K,
        )

        context_chunks = [chunk["text"] for chunk in similar_chunks]

        logger.info(f"Retrieved {len(context_chunks)} relevant chunks for query")
        return context_chunks

    except Exception as e:
        logger.error(f"Failed to retrieve context: {str(e)}")
        raise

_PERSONA_STYLES = {
    "friendly": "Be warm, friendly, and helpful.",
    "professional": "Be professional and businesslike.",
    "playful": "Be lighthearted and playful (while staying accurate).",
    "concise": "Be as brief as possible — no filler, no pleasantries.",
}

_TONE_STYLES = {
    "balanced": "Use a balanced, neutral tone.",
    "formal": "Use a formal tone. No emojis, no slang.",
    "casual": "Use a casual, conversational tone.",
    "empathetic": "Use an empathetic tone — acknowledge the user's situation before answering.",
}

_LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese"}

# maxResponseLength → (prompt instruction, output token cap)
_LENGTH_RULES = {
    "short": ("Answer in 1-2 sentences maximum.", 300),
    "medium": ("Keep the answer focused — a short paragraph unless more detail is truly needed.", 512),
    "long": ("Give a detailed, thorough answer when the question warrants it.", 2048),
}

# How many prior messages (not turns) to replay into the prompt. 8 ≈ 4
# user/bot exchanges — enough for "did we already greet?" and a pronoun
# follow-up, while adding only a few hundred tokens next to the RAG context
# chunks already in the prompt.
_HISTORY_MESSAGE_LIMIT = 8
_HISTORY_TEXT_CAP = 500  # guard against one huge pasted message

def _format_history(history: list[dict] | None, bot_name: str) -> str:
    """Render the last few turns as a 'CONVERSATION SO FAR' block, or '' when
    this is genuinely the first message."""
    if not history:
        return ""
    lines = []
    for msg in history[-_HISTORY_MESSAGE_LIMIT:]:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if len(text) > _HISTORY_TEXT_CAP:
            text = text[:_HISTORY_TEXT_CAP] + "..."
        speaker = "User" if (msg.get("role") or "").lower() == "user" else bot_name
        lines.append(f"{speaker}: {text}")
    if not lines:
        return ""
    return "\nCONVERSATION SO FAR (oldest first — do not repeat yourself):\n" + "\n".join(lines) + "\n"

def _build_behavior_prompt(config: dict, context: str, query: str, history: list[dict] | None = None) -> tuple:
    """Build (prompt, max_tokens, handoff_enabled) from the tenant's chatbot config."""
    identity = config.get("identity", {}) if config else {}
    behavior = config.get("behavior", {}) if config else {}

    bot_name = identity.get("name", "Support Assistant")
    welcome = (identity.get("welcomeMessage") or "").strip()
    persona = _PERSONA_STYLES.get(identity.get("persona"), _PERSONA_STYLES["friendly"])
    tone = _TONE_STYLES.get(behavior.get("tone"), _TONE_STYLES["balanced"])
    language = _LANGUAGE_NAMES.get(identity.get("language"), "English")
    length_instruction, max_tokens = _LENGTH_RULES.get(
        behavior.get("maxResponseLength"), _LENGTH_RULES["medium"]
    )

    rules = [persona, tone, length_instruction, f"Respond in {language} unless the user writes in another language."]

    rules.append(
        "Keep one consistent voice for the whole conversation. Work out from your own name, "
        "your opening message and the knowledge base whether you are speaking AS the person or "
        'organisation the knowledge base describes (use "I"/"my", or "we"/"our" for a team), or '
        "ON BEHALF OF someone you refer to by name. Whichever it is, never switch part-way "
        "through — do not start answering in the third person about someone you introduced "
        "yourself as."
    )

    if behavior.get("stayOnTopic", True):
        rules.append(
            "Only answer questions related to the documents in your knowledge base. "
            "Greetings, thanks and goodbyes are always fine — reply to them naturally and briefly. "
            "If the question is about an unrelated subject, politely say it's outside what you can help with."
        )
    else:
        rules.append("You may also answer general questions beyond the documents when helpful.")

    if behavior.get("showSources", True):
        rules.append('When your answer uses a document, name it (e.g. "According to <document name>...").')
    else:
        rules.append("Do not mention document names or that you are reading from documents.")

    handoff = bool(behavior.get("handoffToHuman"))
    confidence_instruction = ""
    if handoff:
        confidence_instruction = (
            "\nIMPORTANT: The very first line of your reply must be exactly "
            "'CONFIDENCE: <number 0-100>' — your confidence that you can properly answer "
            "this message. Then continue the reply on the next line. "
            "For a real question, this is your confidence that the context below actually "
            "answers it. If the message needs nothing looked up at all — a greeting, a "
            "thank-you, a goodbye, or a reply that follows from the conversation so far — "
            "then you already have everything you need, so score CONFIDENCE: 100."
        )

    rules_text = "\n".join(f"- {r}" for r in rules)
    history_block = _format_history(history, bot_name)

    if welcome:
        opening_block = (
            f'The chat window already opened with this message from you:\n"{welcome}"\n'
            "Use exactly the same voice and point of view as that message in every reply — if it "
            "speaks in the first person, so do you; if it refers to a person or company by name in "
            "the third person, so do you. Do not introduce yourself again, do not state your own "
            "name, and do not open with a greeting — just answer the message directly. "
        )
    else:
        opening_block = (
            "The chat window already opened with a welcome message from you, so the user knows who "
            "you are. Do not introduce yourself, do not state your own name, and do not open with a "
            "greeting — just answer the message directly. "
        )

    prompt = f"""You are "{bot_name}". Answer the user from the knowledge base below.

The knowledge base may describe a company and its product or service, or it may describe a single person (a resume, portfolio or bio). Read the context and work out which — do not assume it is a company. If it is about one person and that is who you are named after or introduced as, then you ARE that person: answer as yourself, in the first person.

STYLE RULES:
{rules_text}

Do not describe your own capabilities or list what documents you have. If the user explicitly asks what you can do or what documents you have, greet them briefly and ask what they need instead of listing anything.

{opening_block}The only exception is when the user's own message is purely a greeting, in which case greet them back briefly.{confidence_instruction}
{history_block}
CONTEXT FROM THE KNOWLEDGE BASE:
{context}

USER MESSAGE: {query}"""
    return prompt, max_tokens, handoff

def _parse_confidence(answer: str) -> tuple:
    """Strip a leading 'CONFIDENCE: NN' line; returns (confidence or None, remaining answer)."""
    import re

    match = re.match(r"\s*CONFIDENCE:\s*(\d{1,3})\s*\n?", answer)
    if not match:
        return None, answer
    return min(int(match.group(1)), 100), answer[match.end():].strip()

def _build_citation_sources(chunks: list[dict], doc_names: dict | None) -> list[dict]:
    """Shape retrieved chunks into the citation payload the frontend renders —
    document identity, a short snippet, and the retrieval score as a
    grounding-confidence proxy (works unconditionally, unlike the LLM's
    self-reported CONFIDENCE line which only exists when handoff is on)."""
    out = []
    for c in chunks:
        text = c.get("text", "")
        out.append({
            "documentId": str(c["document_id"]) if c.get("document_id") else None,
            "documentName": (doc_names or {}).get(str(c.get("document_id"))),
            "snippet": text[:220] + ("…" if len(text) > 220 else ""),
            "page": c.get("page"),
            "score": round(float(c.get("score", 0)), 4),
        })
    return out

async def _prepare_generation(
    tenant_id: str,
    query: str,
    max_tokens: int,
    config: dict | None,
    doc_names: dict | None,
    history: list[dict] | None,
) -> tuple:
    """
    Shared front half of both answer_question() and answer_question_stream():
    embed the query, search Pinecone, label chunks, build the prompt.

    Returns (chunks, prompt, response_max_tokens, handoff_enabled). An empty
    `chunks` list means nothing relevant was retrieved — callers fall back
    to the tenant's configured fallback message rather than calling Gemini
    with no context.
    """
    t0 = time.monotonic()
    query_embedding = await embed_text(query)
    t_embed = time.monotonic()
    chunks = await search_chunks(query_embedding, tenant_id, top_k=settings.RAG_SEARCH_TOP_K)
    t_search = time.monotonic()
    logger.info(
        "rag timing tenant=%s embed=%.0fms search=%.0fms chunks=%d",
        tenant_id, (t_embed - t0) * 1000, (t_search - t_embed) * 1000, len(chunks),
    )

    if not chunks:
        return [], "", max_tokens, False

    # Label each chunk with its source document so the model can cite it.
    labeled = []
    for chunk in chunks:
        name = (doc_names or {}).get(str(chunk.get("document_id")), "")
        header = f"[Source: {name}]\n" if name else ""
        labeled.append(f"{header}{chunk['text']}")
    context = "\n\n---\n\n".join(labeled)

    if config:
        prompt, response_max_tokens, handoff_enabled = _build_behavior_prompt(config, context, query, history)
    else:
        handoff_enabled = False
        response_max_tokens = max_tokens
        prompt = f"""You are a helpful assistant answering questions based on provided documents.

CONTEXT:
{context}

QUESTION: {query}

Please answer the question based on the context provided. If the answer is not in the context, say so."""

    return chunks, prompt, response_max_tokens, handoff_enabled

async def answer_question(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024,
    config: dict = None,
    doc_names: dict = None,
    history: list[dict] | None = None,
) -> dict:
    """
    Answer a question using the RAG pipeline, shaped by the tenant's chatbot
    config (identity + behavior tabs): persona, tone, language, response
    length, stay-on-topic, source citing, and human-handoff confidence.

    Args:
        tenant_id: Tenant ID
        query: User question
        max_tokens: Max tokens (used only when no config is given)
        config: The tenant's chatbot config dict (frontend wire format)
        doc_names: Optional {document_id: filename} map for source citing
        history: Optional prior turns, each {"role": "user"|"bot", "text": str},
            oldest first — lets the model know this isn't the first message.

    Returns:
        {"answer", "sources", "model", "confidence", "handoff"}
    """
    # --- Anthropic (previous provider) — kept, commented, as a rollback path.
    # from anthropic import AsyncAnthropic
    # ANSWER_MODEL = "claude-sonnet-5"
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    model_chain = settings.GEMINI_MODEL_CHAIN

    try:
        chunks, prompt, response_max_tokens, handoff_enabled = await _prepare_generation(
            tenant_id, query, max_tokens, config, doc_names, history
        )

        if not chunks:
            return {
                "answer": "No relevant documents found for your question.",
                "sources": [],
                "model": model_chain[0] if model_chain else None,
                "confidence": 0,
                "handoff": False,
            }

        t_prep = time.monotonic()
        # --- Anthropic (previous provider) — kept, commented, as a rollback path.
        # client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        # response = await client.messages.create(
        #     model=ANSWER_MODEL,
        #     max_tokens=response_max_tokens + (50 if handoff_enabled else 0),
        #     thinking={"type": "adaptive"},
        #     messages=[
        #         {"role": "user", "content": prompt}
        #     ],
        # )
        # answer = next(
        #     (block.text for block in response.content if block.type == "text"), ""
        # )
        client = _get_genai_client()

        async def _generate(model: str):
            return await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=response_max_tokens + (50 if handoff_enabled else 0),
                ),
            )

        # Try each configured model once in order, then give the *last* one
        # (the most deliberately chosen fallback) one more shot after a brief
        # delay to absorb a transient overload rather than give up outright.
        attempts = [(model, 0) for model in model_chain]
        if model_chain:
            attempts.append((model_chain[-1], 1.2))

        response = None
        used_model = model_chain[0] if model_chain else None
        last_error: Exception | None = None
        attempt_count = 0
        for model, delay in attempts:
            attempt_count += 1
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await _generate(model)
                used_model = model
                break
            except genai_errors.APIError as e:
                last_error = e
                logger.warning("Gemini model %s returned an error: %s", model, e)
        if response is None:
            raise last_error
        answer = response.text or ""

        confidence = None
        if handoff_enabled:
            confidence, answer = _parse_confidence(answer)

        t_gen = time.monotonic()
        logger.info(
            "rag timing tenant=%s generate=%.0fms model=%s attempt=%d/%d confidence=%s",
            tenant_id, (t_gen - t_prep) * 1000, used_model, attempt_count, len(attempts), confidence,
        )

        return {
            "answer": answer,
            "sources": _build_citation_sources(chunks, doc_names),
            "model": used_model,
            "confidence": confidence,
            "handoff": False,
        }

    except Exception as e:
        logger.error(f"Failed to generate answer: {str(e)}")
        raise

_CLASSIFY_MAX_TOKENS = 80  # confirmed against a live call: 60 was enough, this leaves headroom
_CLASSIFY_TOPIC_MAX_LEN = 255  # matches Conversation.topic's column width

def _parse_classification(text: str) -> tuple:
    """Pull SENTIMENT/TOPIC lines out of a classification reply; either half is
    None if the model didn't produce it in a recognizable shape."""
    import re

    sentiment = None
    match = re.search(r"SENTIMENT:\s*(Positive|Neutral|Negative)", text, re.IGNORECASE)
    if match:
        sentiment = match.group(1).capitalize()

    topic = None
    match = re.search(r"TOPIC:\s*(.+)", text)
    if match:
        topic = match.group(1).strip()[:_CLASSIFY_TOPIC_MAX_LEN] or None

    return sentiment, topic

async def classify_conversation_turn(tenant_id: str, user_message: str, bot_reply: str) -> tuple:
    """
    Best-effort sentiment + topic classification for one chat turn, e.g.
    ("Negative", "Refund policy"). Deliberately decoupled from the answer
    generation path (see answer_question/answer_question_stream) — callers
    run this *after* the visitor already has their reply, so a slow or
    failing classification call never adds latency to, or breaks, the
    actual chat. Any failure returns (None, None); callers should treat
    that as "leave the stored sentiment/topic as they were."
    """
    if not settings.GEMINI_MODEL_CHAIN:
        return None, None

    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    prompt = (
        "Classify this customer support exchange. Reply with exactly two lines, nothing else:\n"
        "SENTIMENT: Positive, Neutral, or Negative — the customer's tone, not the assistant's.\n"
        "TOPIC: a 2-4 word label for what the customer was asking about.\n\n"
        f"Customer: {user_message}\n"
        f"Assistant: {bot_reply}"
    )
    try:
        client = _get_genai_client()
        # The chain's *last* model, not the first: confirmed live that the
        # primary answer model (gemini-flash-latest) spends an unpredictable
        # chunk of its output budget on internal "thinking" tokens before any
        # visible text — it truncated at MAX_TOKENS with empty/partial text
        # even at 200 tokens. The lighter fallback model has no such
        # overhead and completes this two-line reply cleanly well under
        # _CLASSIFY_MAX_TOKENS, so it's the better fit for a cheap
        # background call, not just a fallback.
        response = await client.aio.models.generate_content(
            model=settings.GEMINI_MODEL_CHAIN[-1],
            contents=prompt,
            config=genai_types.GenerateContentConfig(max_output_tokens=_CLASSIFY_MAX_TOKENS),
        )
        return _parse_classification(response.text or "")
    except genai_errors.APIError as e:
        logger.warning("Conversation classification failed for tenant %s: %s", tenant_id, e)
        return None, None
    except Exception as e:
        logger.warning("Conversation classification errored for tenant %s: %s", tenant_id, e)
        return None, None

async def store_conversation_classification(
    conversation_id, tenant_id: str, user_message: str, bot_reply: str
) -> None:
    """
    classify_conversation_turn() + persist the result — meant to be scheduled
    via core.background.fire_and_forget() after a reply has already been
    sent, from any caller that just wrote a real Conversation turn (the
    public widget, or the dashboard's Test Chatbot in "simulate real visitor"
    mode). Runs in its own DB session since the caller's request-scoped
    session is typically gone (committed and closed, or about to be) by the
    time this executes. A failure here only means this turn's sentiment/topic
    stay at their previous values — it can never affect the chat reply
    already sent to the caller.
    """
    from sqlalchemy import update

    from app.database import AsyncSessionLocal
    from app.models.conversation import Conversation

    sentiment, topic = await classify_conversation_turn(str(tenant_id), user_message, bot_reply)
    if sentiment is None and topic is None:
        return
    values = {k: v for k, v in {"sentiment": sentiment, "topic": topic}.items() if v is not None}
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(update(Conversation).where(Conversation.id == conversation_id).values(**values))
            await session.commit()
    except Exception:
        logger.exception("Failed to store conversation classification for %s", conversation_id)

_CONFIDENCE_PREFIX_MAX = 64  # if no newline by here, the model ignored the instruction

def _try_parse_confidence_prefix(buf: str) -> tuple:
    """
    Streaming counterpart to _parse_confidence(). Must never guess on a
    partial buffer — "CONFIDENCE: 8" must not be read as confidence=8 when
    the model was about to type 85.

    Returns:
        ("pending", None, "")         - keep buffering, the line isn't complete yet
        ("parsed", confidence, rest)  - CONFIDENCE line consumed, `rest` is what follows it
        ("absent", None, buf)         - no CONFIDENCE line; treat `buf` as answer text
    """
    import re

    if "\n" in buf:
        match = re.match(r"\s*CONFIDENCE:\s*(\d{1,3})\s*\n", buf)
        if match:
            return "parsed", min(int(match.group(1)), 100), buf[match.end():]
        return "absent", None, buf
    if len(buf) > _CONFIDENCE_PREFIX_MAX:
        return "absent", None, buf
    return "pending", None, ""

async def _prepend(first, aiter):
    """Yield `first`, then everything else from `aiter` — used to put back
    the first chunk consumed as the model-chain-retry litmus test."""
    yield first
    async for item in aiter:
        yield item

async def _aclose(aiter) -> None:
    """Best-effort stop of an in-flight stream (e.g. once handoff makes the
    rest of the tokens moot) without leaking the underlying connection."""
    try:
        await aiter.aclose()
    except Exception:
        pass

async def answer_question_stream(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024,
    config: dict = None,
    doc_names: dict = None,
    history: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """
    Streaming counterpart to answer_question(). Yields incremental
    {"type": "token", "text": ...} events, then exactly one terminal event:
    {"type": "final", "answer", "sources", "model", "confidence", "handoff"}
    or {"type": "error", "message": ...}.

    Human-handoff confidence gating happens *inside* this generator, not the
    caller — the model's reply starts with a 'CONFIDENCE: NN' line (see
    _build_behavior_prompt) whenever handoff is enabled, and that line is
    buffered (never shown) until parsed. If it's below threshold, nothing
    of the real answer is ever streamed — only the tenant's fallback
    message is, as a single token event. This preserves the exact
    confidence-gating behavior of answer_question()/its callers while still
    letting the bulk of a passing answer stream token-by-token.
    """
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    model_chain = settings.GEMINI_MODEL_CHAIN
    identity = (config or {}).get("identity", {})
    behavior = (config or {}).get("behavior", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")
    threshold = behavior.get("confidenceThreshold", 60)

    try:
        chunks, prompt, response_max_tokens, handoff_enabled = await _prepare_generation(
            tenant_id, query, max_tokens, config, doc_names, history
        )
    except Exception as e:
        logger.exception("Failed to prepare generation for tenant %s", tenant_id)
        yield {"type": "error", "message": str(e)}
        return

    if not chunks:
        yield {"type": "token", "text": fallback}
        yield {
            "type": "final", "answer": fallback, "sources": [],
            "model": model_chain[0] if model_chain else None, "confidence": 0, "handoff": False,
        }
        return

    client = _get_genai_client()

    # Same retry policy as answer_question()'s non-streaming loop, but the
    # litmus test for "did this model work" is "did it produce a first
    # chunk," not "did the whole call succeed." Deliberate scope boundary:
    # once a model has yielded its first chunk we commit to it for the rest
    # of the stream — an APIError raised *mid-stream* becomes an error
    # event, not a retry on another model, since partial text is already
    # visible and replaying it under a different model would duplicate or
    # contradict it. This covers the real failure mode (an overloaded or
    # retired model, which always fails at request time), not a rare
    # mid-stream fault.
    attempts = [(model, 0) for model in model_chain]
    if model_chain:
        attempts.append((model_chain[-1], 1.2))

    t_attempts_start = time.monotonic()
    iterator = None
    first_chunk = None
    used_model = None
    last_error: Exception | None = None
    attempt_count = 0
    for model, delay in attempts:
        attempt_count += 1
        if delay:
            await asyncio.sleep(delay)
        try:
            raw = await client.aio.models.generate_content_stream(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=response_max_tokens + (50 if handoff_enabled else 0),
                ),
            )
            candidate = raw.__aiter__()
            first_chunk = await candidate.__anext__()
            iterator, used_model = candidate, model
            break
        except StopAsyncIteration:
            last_error = RuntimeError(f"{model} returned an empty stream")
        except genai_errors.APIError as e:
            last_error = e
            logger.warning("Gemini model %s failed to start streaming: %s", model, e)

    if iterator is None:
        logger.info(
            "rag timing tenant=%s first_token=FAILED after=%.0fms attempts=%d/%d",
            tenant_id, (time.monotonic() - t_attempts_start) * 1000, attempt_count, len(attempts),
        )
        yield {"type": "error", "message": str(last_error)}
        return

    t_first_chunk = time.monotonic()
    logger.info(
        "rag timing tenant=%s first_token=%.0fms model=%s attempt=%d/%d",
        tenant_id, (t_first_chunk - t_attempts_start) * 1000, used_model, attempt_count, len(attempts),
    )

    full: list[str] = []
    confidence = None
    gate_open = not handoff_enabled  # no CONFIDENCE line to wait for
    buf = ""
    try:
        async for chunk in _prepend(first_chunk, iterator):
            text = getattr(chunk, "text", None) or ""
            if not text:
                continue
            if gate_open:
                full.append(text)
                yield {"type": "token", "text": text}
                continue
            buf += text
            state, parsed, rest = _try_parse_confidence_prefix(buf)
            if state == "pending":
                continue
            confidence = parsed
            if handoff_enabled and confidence is not None and confidence < threshold:
                await _aclose(iterator)
                yield {"type": "token", "text": fallback}
                yield {
                    "type": "final", "answer": fallback, "sources": [],
                    "model": used_model, "confidence": confidence, "handoff": True,
                }
                return
            gate_open = True
            if rest:
                full.append(rest)
                yield {"type": "token", "text": rest}

        # Stream ended while still buffering a possible CONFIDENCE line (the
        # whole reply was shorter than the line itself) — flush it instead
        # of silently dropping it; there's nothing more coming either way.
        if not gate_open and buf:
            state, parsed, rest = _try_parse_confidence_prefix(buf)
            text = rest if state == "parsed" else buf
            if state == "parsed":
                confidence = parsed
            if handoff_enabled and confidence is not None and confidence < threshold:
                yield {"type": "token", "text": fallback}
                yield {
                    "type": "final", "answer": fallback, "sources": [],
                    "model": used_model, "confidence": confidence, "handoff": True,
                }
                return
            if text:
                full.append(text)
                yield {"type": "token", "text": text}
    except Exception as e:
        logger.exception("Streaming generation failed for tenant %s", tenant_id)
        yield {"type": "error", "message": str(e)}
        return

    answer = "".join(full).strip()
    t_gen = time.monotonic()
    logger.info(
        "rag timing tenant=%s rest_of_stream=%.0fms total_generate=%.0fms model=%s confidence=%s",
        tenant_id, (t_gen - t_first_chunk) * 1000, (t_gen - t_attempts_start) * 1000, used_model, confidence,
    )
    yield {
        "type": "final", "answer": answer, "sources": _build_citation_sources(chunks, doc_names),
        "model": used_model, "confidence": confidence, "handoff": False,
    }

async def verify_gemini_models() -> None:
    """
    Startup sanity check: confirm the configured Gemini model chain
    (settings.GEMINI_MODEL_CHAIN) is actually reachable with the current
    GEMINI_API_KEY. Uses models.get() (cheap metadata fetch, no generation
    cost) rather than generate_content. Strictly non-fatal — logs loudly,
    never raises — so a bad key/model never blocks app boot. This turns a
    deprecated/inaccessible model into a startup log line instead of a live
    user's failed chat (this has already happened twice: gemini-2.0-flash,
    then gemini-2.5-flash, were both retired for this free-tier key).
    """
    if not settings.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not set — skipping Gemini model reachability check")
        return

    from google.genai import errors as genai_errors

    client = _get_genai_client()
    reachable = []
    for model in settings.GEMINI_MODEL_CHAIN:
        try:
            await client.aio.models.get(model=model)
            reachable.append(model)
            logger.info("Gemini model %r is reachable with the configured API key", model)
        except genai_errors.APIError as e:
            logger.warning(
                "Gemini model %r is NOT reachable with the configured API key (%s). "
                "Dated/pinned model names are the most likely to be retired — prefer "
                "'-latest' aliases in GEMINI_ANSWER_MODEL/GEMINI_FALLBACK_MODELS.",
                model, e,
            )
        except Exception as e:
            logger.warning("Could not verify Gemini model %r (%s): %s", model, type(e).__name__, e)

    if not reachable:
        logger.error(
            "None of the configured Gemini models (%s) are reachable with the current "
            "GEMINI_API_KEY — chat answers will fail until this is fixed.",
            ", ".join(settings.GEMINI_MODEL_CHAIN),
        )
