"""Document chunking service with recursive chunking and token-based overlap."""

import logging
from typing import List
import tiktoken

logger = logging.getLogger(__name__)

ENCODING = tiktoken.encoding_for_model("gpt-3.5-turbo")

def count_tokens(text: str) -> int:
    """Count tokens in text using OpenAI tokenizer (compatible with Claude)."""
    return len(ENCODING.encode(text))

def recursive_chunk(
    text: str,
    chunk_size: int = 500,
    overlap_size: int = 50,
) -> List[str]:
    """
    Recursively chunk text into token-based chunks with overlap.

    Args:
        text: Text to chunk
        chunk_size: Target tokens per chunk (~500)
        overlap_size: Overlap tokens between chunks (~50)

    Returns:
        List of text chunks with overlap
    """
    if count_tokens(text) <= chunk_size:
        return [text]

    chunks = []
    tokens = ENCODING.encode(text)

    start_idx = 0
    while start_idx < len(tokens):
        end_idx = min(start_idx + chunk_size, len(tokens))
        chunk_tokens = tokens[start_idx:end_idx]
        chunk_text = ENCODING.decode(chunk_tokens)

        chunks.append(chunk_text)

        start_idx = max(start_idx + chunk_size - overlap_size, start_idx + 1)

        if end_idx == len(tokens):
            break

    logger.info(
        "Chunked text into %d chunks (size: %d tokens, overlap: %d tokens)",
        len(chunks),
        chunk_size,
        overlap_size,
    )
    return chunks

def chunk_pdf_text(
    pdf_text: str,
    chunk_size: int = 500,
    overlap_size: int = 50,
) -> List[dict]:
    """
    Chunk PDF text and return metadata.

    Args:
        pdf_text: Extracted text from PDF
        chunk_size: Target tokens per chunk
        overlap_size: Overlap tokens

    Returns:
        List of chunks with metadata
    """
    chunks = recursive_chunk(pdf_text, chunk_size, overlap_size)

    chunks_with_metadata = []
    for i, chunk_text in enumerate(chunks):
        chunks_with_metadata.append({
            "chunk_id": i,
            "text": chunk_text,
            "token_count": count_tokens(chunk_text),
            "sequence": i,
        })

    return chunks_with_metadata
