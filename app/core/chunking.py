"""Document chunking service with recursive chunking and token-based overlap."""

import bisect
import logging
from typing import List, Optional, Tuple
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
    return [text for text, _offset, _count in _chunk_with_offsets(text, chunk_size, overlap_size)]

def _chunk_with_offsets(
    text: str,
    chunk_size: int,
    overlap_size: int,
) -> List[Tuple[str, int, int]]:
    """
    Same chunking as recursive_chunk, but also returns each chunk's
    approximate starting character offset in `text` (cheap, O(1) extra work
    per chunk via incremental decode — not an exact substring index, since
    BPE token boundaries don't always align with character boundaries, but
    close enough for page/citation lookups) and its token count — already
    known from the slice length, so callers don't need to re-encode the
    chunk text just to count it.
    """
    all_tokens = ENCODING.encode(text)
    if len(all_tokens) <= chunk_size:
        return [(text, 0, len(all_tokens))]

    results: List[Tuple[str, int, int]] = []
    tokens = all_tokens

    start_idx = 0
    char_offset = 0
    while start_idx < len(tokens):
        end_idx = min(start_idx + chunk_size, len(tokens))
        chunk_tokens = tokens[start_idx:end_idx]
        chunk_text = ENCODING.decode(chunk_tokens)

        results.append((chunk_text, char_offset, len(chunk_tokens)))

        next_start_idx = max(start_idx + chunk_size - overlap_size, start_idx + 1)
        char_offset += len(ENCODING.decode(tokens[start_idx:next_start_idx]))
        start_idx = next_start_idx

        if end_idx == len(tokens):
            break

    logger.info(
        "Chunked text into %d chunks (size: %d tokens, overlap: %d tokens)",
        len(results),
        chunk_size,
        overlap_size,
    )
    return results

def _page_for_offset(page_map: List[Tuple[int, int]], char_offset: int) -> Optional[int]:
    """Given [(char_start, page_number), ...] sorted by char_start, find the
    page whose range contains char_offset (the last boundary <= char_offset)."""
    if not page_map:
        return None
    starts = [s for s, _ in page_map]
    idx = bisect.bisect_right(starts, char_offset) - 1
    if idx < 0:
        idx = 0
    return page_map[idx][1]

def chunk_pdf_text(
    pdf_text: str,
    chunk_size: int = 500,
    overlap_size: int = 50,
    page_map: Optional[List[Tuple[int, int]]] = None,
) -> List[dict]:
    """
    Chunk text and return metadata.

    Args:
        pdf_text: Extracted text
        chunk_size: Target tokens per chunk
        overlap_size: Overlap tokens
        page_map: Optional [(char_start, page_number), ...] from PDF
            extraction, used to attach a best-effort `page` to each chunk.

    Returns:
        List of chunks with metadata
    """
    chunks_with_offsets = _chunk_with_offsets(pdf_text, chunk_size, overlap_size)

    chunks_with_metadata = []
    for i, (chunk_text, char_offset, token_count) in enumerate(chunks_with_offsets):
        chunks_with_metadata.append({
            "chunk_id": i,
            "text": chunk_text,
            "token_count": token_count,
            "sequence": i,
            "char_offset": char_offset,
            "page": _page_for_offset(page_map, char_offset) if page_map else None,
        })

    return chunks_with_metadata
