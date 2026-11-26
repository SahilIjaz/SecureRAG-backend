"""Unit tests: chunking (app.core.chunking) and Reciprocal Rank Fusion (hybrid_search)."""
from app.core import chunking
from app.services import hybrid_search as hs


def _text(n):  # n whitespace-separated words, one token each under any sane tokenizer
    return " ".join(f"word{i}" for i in range(n))


def test_short_text_is_single_chunk():
    assert chunking.recursive_chunk("hello world") == ["hello world"]


def test_long_text_splits_into_multiple_chunks():
    assert len(chunking.recursive_chunk(_text(3000), 500, 50)) > 1


def test_chunks_never_exceed_size():
    for c in chunking.recursive_chunk(_text(3000), 500, 50):
        assert chunking.count_tokens(c) <= 500 + 5


def test_consecutive_chunks_overlap():
    chunks = chunking.recursive_chunk(_text(1200), 500, 50)
    assert len(chunks) >= 2
    tail = set(chunks[0].split()[-30:])
    head = set(chunks[1].split()[:60])
    assert tail & head


def test_all_content_is_covered():
    text = _text(2000)
    joined = " ".join(chunking.recursive_chunk(text, 500, 50))
    assert "word0" in joined and "word1999" in joined


def test_offsets_are_monotonic_and_counts_positive():
    res = chunking._chunk_with_offsets(_text(2500), 500, 50)
    offs = [o for _, o, _ in res]
    assert offs == sorted(offs) and offs[0] == 0
    assert all(n > 0 for _, _, n in res)


def test_empty_text_does_not_crash():
    assert chunking.recursive_chunk("") in ([""], [])


# ── RRF ──────────────────────────────────────────────────────────────────────
def _c(i, doc="d"):
    return {"document_id": doc, "chunk_id": f"c{i}", "text": f"t{i}"}


def test_rrf_chunk_in_both_lists_ranks_first():
    vec = [_c(1), _c(2), _c(3)]
    kw = [_c(9), _c(1), _c(8)]
    out = hs._reciprocal_rank_fusion([vec, kw], top_k=5)
    assert out[0]["chunk_id"] == "c1"


def test_rrf_dedupes_same_chunk():
    out = hs._reciprocal_rank_fusion([[_c(1), _c(2)], [_c(1), _c(2)]], top_k=10)
    assert [c["chunk_id"] for c in out] == ["c1", "c2"]


def test_rrf_respects_top_k():
    out = hs._reciprocal_rank_fusion([[_c(i) for i in range(10)]], top_k=3)
    assert len(out) == 3


def test_rrf_score_formula():
    out = hs._reciprocal_rank_fusion([[_c(1)], [_c(1)]], top_k=1)
    assert out[0]["fused_score"] == round(2 / (hs._RRF_K + 1), 6)


def test_rrf_empty_inputs():
    assert hs._reciprocal_rank_fusion([[], []], top_k=5) == []


def test_rrf_same_chunk_id_in_different_documents_not_merged():
    out = hs._reciprocal_rank_fusion([[_c(1, "docA")], [_c(1, "docB")]], top_k=5)
    assert len(out) == 2   # tenant/doc isolation of identity key
