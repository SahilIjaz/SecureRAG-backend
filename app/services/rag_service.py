"""RAG (Retrieval-Augmented Generation) service."""

import logging
import uuid
from typing import List
from PyPDF2 import PdfReader
import io

from app.core.chunking import chunk_pdf_text
from app.core.embeddings import embed_text, embed_chunks
from app.core.vector_store import upsert_chunks, search_chunks
from app.config import settings

logger = logging.getLogger(__name__)

async def process_document_for_rag(
    tenant_id: str,
    document_id: str,
    pdf_bytes: bytes,
) -> dict:
    """
    Process a PDF document for RAG:
    1. Extract text from PDF
    2. Chunk into overlapping segments
    3. Generate embeddings
    4. Store in Pinecone

    Args:
        tenant_id: Tenant ID
        document_id: Document ID
        pdf_bytes: Raw PDF content

    Returns:
        Processing result with chunk count
    """
    try:
        logger.info(f"Extracting text from PDF for document {document_id}")
        pdf_text = _extract_text_from_pdf(pdf_bytes)

        if not pdf_text.strip():
            raise ValueError("No text extracted from PDF")

        logger.info(f"Chunking document into {settings.RAG_CHUNK_SIZE}-token chunks")
        chunks = chunk_pdf_text(
            pdf_text,
            chunk_size=settings.RAG_CHUNK_SIZE,
            overlap_size=settings.RAG_CHUNK_OVERLAP,
        )

        if not chunks:
            raise ValueError("No chunks generated from PDF")

        logger.info(f"Generating embeddings for {len(chunks)} chunks")
        chunk_texts = [chunk["text"] for chunk in chunks]
        embeddings = await embed_chunks(chunk_texts)

        logger.info(f"Storing {len(chunks)} chunks in Pinecone")
        await upsert_chunks(tenant_id, document_id, chunks, embeddings)

        logger.info(
            f"Successfully processed document {document_id} with {len(chunks)} chunks"
        )

        return {
            "document_id": document_id,
            "chunk_count": len(chunks),
            "total_tokens": sum(c["token_count"] for c in chunks),
            "status": "success",
        }

    except Exception as e:
        logger.error(f"Failed to process document for RAG: {str(e)}")
        raise

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
    "medium": ("Keep the answer focused — a short paragraph unless more detail is truly needed.", 1024),
    "long": ("Give a detailed, thorough answer when the question warrants it.", 2048),
}

def _build_behavior_prompt(config: dict, context: str, query: str) -> tuple:
    """Build (prompt, max_tokens, handoff_enabled) from the tenant's chatbot config."""
    identity = config.get("identity", {}) if config else {}
    behavior = config.get("behavior", {}) if config else {}

    bot_name = identity.get("name", "Support Assistant")
    persona = _PERSONA_STYLES.get(identity.get("persona"), _PERSONA_STYLES["friendly"])
    tone = _TONE_STYLES.get(behavior.get("tone"), _TONE_STYLES["balanced"])
    language = _LANGUAGE_NAMES.get(identity.get("language"), "English")
    length_instruction, max_tokens = _LENGTH_RULES.get(
        behavior.get("maxResponseLength"), _LENGTH_RULES["medium"]
    )

    rules = [persona, tone, length_instruction, f"Respond in {language} unless the user writes in another language."]

    if behavior.get("stayOnTopic", True):
        rules.append(
            "Only answer questions related to the documents in your knowledge base. "
            "If the question is off-topic (small talk is fine, but unrelated subjects are not), "
            "politely say it's outside what you can help with."
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
            "'CONFIDENCE: <number 0-100>' — your confidence that the context actually answers "
            "the question. Then continue the reply on the next line."
        )

    rules_text = "\n".join(f"- {r}" for r in rules)
    prompt = f"""You are "{bot_name}", a customer support chatbot answering from the company's knowledge base.

STYLE RULES:
{rules_text}

Do not describe your own capabilities or list what documents you have unless the user explicitly asks. Greet briefly and ask what they need instead.{confidence_instruction}

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

async def answer_question(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024,
    config: dict = None,
    doc_names: dict = None,
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

    Returns:
        {"answer", "sources", "model", "confidence", "handoff"}
    """
    from anthropic import AsyncAnthropic

    ANSWER_MODEL = "claude-opus-4-8"

    try:
        query_embedding = await embed_text(query)
        chunks = await search_chunks(
            query_embedding, tenant_id, top_k=settings.RAG_SEARCH_TOP_K
        )

        if not chunks:
            return {
                "answer": "No relevant documents found for your question.",
                "sources": [],
                "model": ANSWER_MODEL,
                "confidence": 0,
                "handoff": False,
            }

        # Label each chunk with its source document so the model can cite it.
        labeled = []
        for chunk in chunks:
            name = (doc_names or {}).get(str(chunk.get("document_id")), "")
            header = f"[Source: {name}]\n" if name else ""
            labeled.append(f"{header}{chunk['text']}")
        context = "\n\n---\n\n".join(labeled)

        if config:
            prompt, response_max_tokens, handoff_enabled = _build_behavior_prompt(config, context, query)
        else:
            handoff_enabled = False
            response_max_tokens = max_tokens
            prompt = f"""You are a helpful assistant answering questions based on provided documents.

CONTEXT:
{context}

QUESTION: {query}

Please answer the question based on the context provided. If the answer is not in the context, say so."""

        logger.info("Sending query to Claude for answer generation")
        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=ANSWER_MODEL,
            max_tokens=response_max_tokens + (50 if handoff_enabled else 0),
            thinking={"type": "adaptive"},
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        answer = next(
            (block.text for block in response.content if block.type == "text"), ""
        )

        confidence = None
        if handoff_enabled:
            confidence, answer = _parse_confidence(answer)

        logger.info("Generated answer using RAG pipeline (confidence=%s)", confidence)

        return {
            "answer": answer,
            "sources": [c["text"] for c in chunks],
            "model": ANSWER_MODEL,
            "confidence": confidence,
            "handoff": False,
        }

    except Exception as e:
        logger.error(f"Failed to generate answer: {str(e)}")
        raise

def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extract text from PDF bytes.

    Args:
        pdf_bytes: Raw PDF content

    Returns:
        Extracted text
    """
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        pdf_reader = PdfReader(pdf_file)

        text_parts = []
        for page_num, page in enumerate(pdf_reader.pages):
            page_text = page.extract_text()
            if page_text.strip():
                text_parts.append(f"--- Page {page_num + 1} ---\n{page_text}")

        return "\n\n".join(text_parts)

    except Exception as e:
        logger.error(f"Failed to extract text from PDF: {str(e)}")
        raise
