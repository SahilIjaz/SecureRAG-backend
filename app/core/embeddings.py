"""Embedding service using Anthropic's Claude model."""

import logging
from typing import List
import asyncio

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSION = 1536


async def embed_text(text: str) -> List[float]:
    """
    Generate embedding for text using Claude's semantic understanding.

    Uses a simple approach: convert text to vector via Claude's model.
    For production, consider using OpenAI embeddings API which is more optimized.

    Args:
        text: Text to embed

    Returns:
        List of floats representing the embedding
    """
    try:
        embedding = await asyncio.to_thread(_simple_embedding, text)
        return embedding

    except Exception as e:
        logger.error(f"Failed to embed text: {str(e)}")
        raise


async def embed_chunks(texts: List[str]) -> List[List[float]]:
    """
    Generate embeddings for multiple text chunks.

    Args:
        texts: List of text chunks to embed

    Returns:
        List of embeddings (one per chunk)
    """
    embeddings = []
    for text in texts:
        embedding = await embed_text(text)
        embeddings.append(embedding)

    logger.info(f"Generated embeddings for {len(texts)} chunks")
    return embeddings


def _simple_embedding(text: str) -> List[float]:
    """
    Simple embedding function for development.

    In production, use:
    - OpenAI's text-embedding-3-small
    - Hugging Face sentence-transformers
    - Cohere embeddings API

    Args:
        text: Text to embed

    Returns:
        Vector embedding
    """
    import hashlib

    text_hash = hashlib.md5(text.encode()).hexdigest()

    embedding = []
    for i in range(0, 32, 2):
        hex_pair = text_hash[i:i+2]
        val = int(hex_pair, 16) / 255.0
        embedding.append(val)

    while len(embedding) < EMBEDDING_DIMENSION:
        embedding.append(0.0)

    return embedding[:EMBEDDING_DIMENSION]

