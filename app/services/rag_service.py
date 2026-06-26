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
        # Step 1: Extract text from PDF
        logger.info(f"Extracting text from PDF for document {document_id}")
        pdf_text = _extract_text_from_pdf(pdf_bytes)

        if not pdf_text.strip():
            raise ValueError("No text extracted from PDF")

        # Step 2: Chunk the text
        logger.info(f"Chunking document into {settings.RAG_CHUNK_SIZE}-token chunks")
        chunks = chunk_pdf_text(
            pdf_text,
            chunk_size=settings.RAG_CHUNK_SIZE,
            overlap_size=settings.RAG_CHUNK_OVERLAP,
        )

        if not chunks:
            raise ValueError("No chunks generated from PDF")

        # Step 3: Generate embeddings
        logger.info(f"Generating embeddings for {len(chunks)} chunks")
        chunk_texts = [chunk["text"] for chunk in chunks]
        embeddings = await embed_chunks(chunk_texts)

        # Step 4: Store in Pinecone
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
        # Generate embedding for query
        logger.info(f"Generating embedding for query")
        query_embedding = await embed_text(query)

        # Search for similar chunks
        logger.info(f"Searching for similar chunks (top {settings.RAG_SEARCH_TOP_K})")
        similar_chunks = await search_chunks(
            query_embedding,
            tenant_id,
            top_k=settings.RAG_SEARCH_TOP_K,
        )

        # Extract text from results
        context_chunks = [chunk["text"] for chunk in similar_chunks]

        logger.info(f"Retrieved {len(context_chunks)} relevant chunks for query")
        return context_chunks

    except Exception as e:
        logger.error(f"Failed to retrieve context: {str(e)}")
        raise


async def answer_question(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024,
) -> dict:
    """
    Answer a question using RAG pipeline:
    1. Retrieve relevant chunks
    2. Build context
    3. Send to Claude for answer

    Args:
        tenant_id: Tenant ID
        query: User question
        max_tokens: Max tokens in response

    Returns:
        Answer and source chunks
    """
    from anthropic import Anthropic

    try:
        # Step 1: Retrieve context
        context_chunks = await retrieve_context_for_query(tenant_id, query)

        if not context_chunks:
            return {
                "answer": "No relevant documents found for your question.",
                "sources": [],
            }

        # Step 2: Build prompt with context
        context = "\n\n".join(context_chunks)
        prompt = f"""You are a helpful assistant answering questions based on provided documents.

CONTEXT:
{context}

QUESTION: {query}

Please answer the question based on the context provided. If the answer is not in the context, say so."""

        # Step 3: Send to Claude
        logger.info("Sending query to Claude for answer generation")
        client = Anthropic()
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=max_tokens,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        answer = response.content[0].text

        logger.info("Generated answer using RAG pipeline")

        return {
            "answer": answer,
            "sources": context_chunks,
            "model": "claude-3-5-sonnet-20241022",
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
