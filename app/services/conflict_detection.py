"""
Contradiction detection over retrieved knowledge-base chunks.

Three bounded, deterministic detectors run over sentence-level *statements*
extracted from the chunks the retriever returned for one question:

  numeric   — the same quantity (shared subject words) with different values:
              "refunds within 7 days" vs "refunds within 14 days"
  date      — the same subject with different calendar dates:
              "policy effective 1 March 2026" vs "… effective 1 June 2026"
  semantic  — the same statement with opposite polarity, where polarity is
              negation ("not", "never", "without" …) or a known antonym
              ("refundable" vs "non-refundable", "allowed" vs "prohibited"):
              "refunds are allowed" vs "refunds are not allowed"

Every detector requires the two statements to be about the *same subject*
(shared content words after normalisation) under *compatible conditions*
(if both sentences state a condition — "if …", "unless …", "for enterprise
customers" — the conditions must overlap; otherwise two scoped rules such as
"7 days for digital products" / "30 days for physical products" are
correctly left alone) and, for numeric facts, in *compatible units*.

Semantic candidates are heuristic by nature, so rag_service can optionally
confirm them with one cheap classification call per pair
(`verify_semantic_conflicts`, gated by RAG_CONFLICT_LLM_VERIFY, cached in
Redis). Numeric and date conflicts need no model — they are exact.

Tenant safety is structural: callers pass chunks that retrieval already
scoped to one tenant's Pinecone namespace / Postgres rows, so any two
statements compared here belong to the same tenant. Nothing here reads the
database.

Output: a list of conflict dicts (see `_make_conflict`) — the shape
rag_service, the resolver and the evaluation script share.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

_MIN_RELATIVE_DIFF = 0.01       # 10 vs 10.0 is float noise, not a conflict
_MIN_SHARED_WORDS = 2           # same-subject floor for numeric/date facts
_MIN_SHARED_SEMANTIC = 2        # … for polarity statements
_MIN_JACCARD_SEMANTIC = 0.4     # polarity statements must largely overlap
_MIN_JACCARD_INTRA = 0.6        # same-document pairs must be near-identical
_NUMERIC_WINDOW = 5             # content words taken each side of a number
_MAX_SENTENCES_PER_CHUNK = 30  # a 500-token chunk is ~8–20 sentences; bounds the O(n²) scan
_MAX_SENTENCE_CHARS = 400
_STATEMENT_PREVIEW = 200

_STOPWORDS = frozenset("""
the and for are was were with from that this have has had you your our their its
all any can will each per more than then when where which who whom what how why
into onto about above below over under again further once here there both few
most other some such only own same too very just also may might must shall should
would could been being does did doing because while until among between through
during before after around across off out up down these those them they him her
his she his it its itself please note see page section
""".split())

# Measure/time words: fine as *supporting* context but never enough on their
# own to prove two numbers describe the same fact ("7 days" and "2 days" in
# unrelated sentences share "days" and nothing else).
_GENERIC_WORDS = frozenset("""
day days week weeks month months year years hour hours minute minutes second
seconds percent pct usd dollar dollars rs pkr inr eur gbp cent cents time times
maximum minimum least most within per up after before business working calendar
approximately around about roughly nearly almost total number amount
""".split())

_NEGATION_TOKENS = frozenset("""
not no never cannot cant can't dont don't doesnt doesn't isnt isn't arent aren't
wasnt wasn't werent weren't wont won't wouldnt wouldn't shouldnt shouldn't
without neither nor none nothing nobody
""".split())

# Antonym → (base word, flips polarity). Mapping both sides onto one base
# word lets "refunds are non-refundable" and "refunds are not refundable"
# compare as the same statement with the same (negative) polarity, while
# "refunds are refundable" is the opposite polarity of both.
_ANTONYMS = {
    "unavailable": "available", "nonrefundable": "refundable", "non-refundable": "refundable",
    "prohibited": "allowed", "forbidden": "allowed", "disallowed": "allowed", "banned": "allowed",
    "excluded": "included", "optional": "mandatory", "closed": "open", "disabled": "enabled",
    "unsupported": "supported", "unlimited": "limited", "impossible": "possible",
    "unpaid": "paid", "inactive": "active", "ineligible": "eligible", "unverified": "verified",
    "offline": "online", "nontransferable": "transferable", "non-transferable": "transferable",
    "denied": "approved", "rejected": "approved", "declined": "approved",
}
# Words that are *affirmative* synonyms of an antonym base — normalised so
# "permitted" and "allowed" count as the same word.
_SYNONYMS = {
    "permitted": "allowed", "accepted": "allowed", "possible": "possible",
    "required": "mandatory", "compulsory": "mandatory", "obligatory": "mandatory",
    "refund": "refund", "refunds": "refund", "refundable": "refundable",
    "returnable": "returnable", "free": "free", "complimentary": "free",
    "chargeable": "paid", "charged": "paid",
}

_CONDITION_TRIGGERS = re.compile(
    r"\b(if|unless|except|excluding|only|applies to|apply to|in case of|provided that|when)\b",
    re.IGNORECASE,
)
_SCOPE_NOUNS = r"(customers?|clients?|orders?|plans?|products?|items?|members?|accounts?|subscriptions?|users?|purchases?|services?|regions?|countries|country|tiers?|visitors?|guests?|students?|employees?|staff)"
_FOR_SCOPE = re.compile(r"\bfor\s+((?:[a-z][a-z\-]+\s+){0,3}" + _SCOPE_NOUNS + r")\b", re.IGNORECASE)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']{1,}")
_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w.])")
_UNIT_AFTER_RE = re.compile(r"^\s*(%|percent|days?|weeks?|months?|years?|hours?|minutes?|business days?|working days?|usd|pkr|rs|eur|gbp|dollars?|cents?|items?|units?|gb|mb|tb|kg|g|km|m)\b", re.IGNORECASE)
_CURRENCY_BEFORE_RE = re.compile(r"(\$|€|£|rs\.?|pkr|usd|eur|gbp)\s*$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n{1,}|\s*[•▪●]\s*|\s+-\s+(?=[A-Z])")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),                                            # 2026-03-01
    re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b"),                                  # 01/03/2026
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RE + r",?\s+(\d{4})\b", re.I),  # 1 March 2026
    re.compile(r"\b" + _MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I),  # March 1, 2026
]

_UNIT_FAMILY = {
    "%": "pct", "percent": "pct",
    "day": "day", "days": "day", "business day": "day", "business days": "day",
    "working day": "day", "working days": "day",
    "week": "week", "weeks": "week", "month": "month", "months": "month",
    "year": "year", "years": "year", "hour": "hour", "hours": "hour",
    "minute": "minute", "minutes": "minute",
    "usd": "money", "pkr": "money", "rs": "money", "eur": "money", "gbp": "money",
    "dollar": "money", "dollars": "money", "cent": "money", "cents": "money",
    "$": "money", "€": "money", "£": "money",
    "gb": "bytes", "mb": "bytes", "tb": "bytes", "kg": "mass", "g": "mass", "km": "length", "m": "length",
    "item": "count", "items": "count", "unit": "count", "units": "count",
}


# ── Normalisation ─────────────────────────────────────────────────────────────

def _norm(word: str) -> str:
    """Lower-case, strip possessives, crude singularisation, synonym fold."""
    w = word.lower().strip("'-")
    if w.endswith("'s"):
        w = w[:-2]
    if w in _SYNONYMS:
        return _SYNONYMS[w]
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return _SYNONYMS.get(w, w)


_NEGATION_REACH = 3  # content words after a negation token that it can govern


def _content_words(text: str) -> tuple[set, list]:
    """(normalised content-word set, negation targets) for one sentence.

    Negation tokens and antonyms are removed from the word set. Each one
    records what it *governs* — the next few content words after a negation
    token ("not allowed" → {allowed}), or the antonym's base word
    ("non-refundable" → {refundable}) — so that a pair of statements only
    counts as opposite in polarity when the negation is about a word the two
    statements share. "No questions asked" then no longer flips the polarity
    of a sentence about refunds. An empty target set (negation at the very
    end of the sentence) is treated as governing the whole statement."""
    tokens = [(raw, raw.lower()) for raw in _WORD_RE.findall(text)]
    normed = []
    for raw, low in tokens:
        if low in _NEGATION_TOKENS or low in _ANTONYMS or (low.startswith("non-") and len(low) > 5):
            normed.append(None)
        else:
            n = _norm(raw)
            normed.append(n if len(n) >= 3 and n not in _STOPWORDS else None)

    words: set = set()
    targets: list = []
    for i, (raw, low) in enumerate(tokens):
        if low in _NEGATION_TOKENS:
            following = [n for n in normed[i + 1:] if n][:_NEGATION_REACH]
            targets.append(frozenset(following))
            continue
        if low in _ANTONYMS:
            base = _norm(_ANTONYMS[low])
            words.add(base)
            targets.append(frozenset({base}))
            continue
        if low.startswith("non-") and len(low) > 5:
            base = _norm(low[4:])
            words.add(base)
            targets.append(frozenset({base}))
            continue
        if normed[i]:
            words.add(normed[i])
    return words, targets


def _polarity_flips(targets: list, shared: set) -> int:
    """How many negations in a statement govern a word the pair shares
    (or govern nothing specific, i.e. the whole statement)."""
    return sum(1 for t in targets if not t or (t & shared))


_SCOPE_NOUN_WORDS = frozenset(_norm(w) for w in (
    "customer client order plan product item member account subscription user "
    "purchase service region country tier visitor guest student employee staff"
).split())


# Comparators are stopwords elsewhere but are exactly what distinguishes
# "if the order is over 100" from "if the order is under 100".
_CONDITION_KEEP = frozenset(
    "over under above below before after more less least most than within outside "
    "inside new existing first prior during without".split()
)


def _clause_words(clause: str) -> set:
    ws, _ = _content_words(clause)
    ws |= {w.lower() for w in _WORD_RE.findall(clause) if w.lower() in _CONDITION_KEEP}
    return ws


def _condition_words(sentence: str) -> set:
    """Qualifying words of any conditional/scoping clause in the sentence —
    the *distinguishing* part of "for digital products" is "digital", so the
    scope noun itself and measure words are dropped."""
    found: set = set()
    for m in _CONDITION_TRIGGERS.finditer(sentence):
        clause = re.split(r"[,.;:]|\bthen\b", sentence[m.end():], maxsplit=1)[0]
        found |= _clause_words(clause)
    for m in _FOR_SCOPE.finditer(sentence):
        found |= _clause_words(m.group(1))
    return {w for w in found if w not in _GENERIC_WORDS and w not in _SCOPE_NOUN_WORDS}


def _conditions_compatible(a: set, b: set) -> bool:
    """Two statements can only contradict if they apply in the same
    situation. Both unconditional → compatible. One conditional → still
    compatible (a general rule vs a scoped one is worth surfacing). Both
    conditional with no shared qualifier → different situations, not a
    conflict ("7 days for digital products" vs "30 days for physical ones")."""
    if not a or not b:
        return True
    return bool(a & b)


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Statement extraction ──────────────────────────────────────────────────────

@dataclass
class _Statement:
    doc_id: str
    chunk_index: int
    text: str
    words: set
    negations: list                               # negation targets, see _content_words
    conditions: set
    tokens: list = field(default_factory=list)    # normalised words in sentence order
    numbers: list = field(default_factory=list)   # [(value, unit_family|None, local_words)]
    dates: list = field(default_factory=list)     # [date]

    def negated_for(self, shared: set) -> bool:
        return _polarity_flips(self.negations, shared) % 2 == 1


def _parse_date(m: re.Match, pattern_index: int) -> Optional[date]:
    try:
        g = m.groups()
        if pattern_index == 0:
            return date(int(g[0]), int(g[1]), int(g[2]))
        if pattern_index == 1:
            d, mth, y = int(g[0]), int(g[1]), int(g[2])
            try:
                return date(y, mth, d)       # day-first (most of the world)
            except ValueError:
                return date(y, d, mth)       # month-first fallback
        if pattern_index == 2:
            return date(int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0]))
        if pattern_index == 3:
            return date(int(g[2]), _MONTHS[g[0].lower()[:3]], int(g[1]))
    except (ValueError, KeyError):
        return None
    return None


def _extract_dates(sentence: str) -> tuple[list, str]:
    """Dates in the sentence and the sentence with those spans masked so the
    numeric extractor doesn't also read "2026" as a quantity."""
    dates, masked = [], sentence
    for i, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(sentence):
            d = _parse_date(m, i)
            if d:
                dates.append(d)
                masked = masked.replace(m.group(0), " " * len(m.group(0)))
    return dates, masked


def _unit_family(before: str, after: str) -> Optional[str]:
    m = _UNIT_AFTER_RE.match(after)
    if m:
        return _UNIT_FAMILY.get(m.group(1).lower())
    m = _CURRENCY_BEFORE_RE.search(before)
    if m:
        return "money"
    return None


def _looks_like_year(raw: str, unit: Optional[str]) -> bool:
    """A bare 4-digit number in 1900–2100 with no unit ("Refund Policy 2024",
    "founded in 2019") is a year, not a quantity to compare."""
    return unit is None and raw.isdigit() and len(raw) == 4 and 1900 <= int(raw) <= 2100


def _extract_numbers(masked: str) -> list:
    out = []
    for m in _NUMBER_RE.finditer(masked):
        raw = m.group(1)
        before, after = masked[: m.start()], masked[m.end():]
        unit = _unit_family(before, after)
        if _looks_like_year(raw, unit):
            continue
        value = float(raw.replace(",", ""))
        local = [w for w in _WORD_RE.findall(before)[-_NUMERIC_WINDOW:]]
        local += [w for w in _WORD_RE.findall(after)[:_NUMERIC_WINDOW]]
        words, _ = _content_words(" ".join(local))
        out.append((value, unit, words))
    return out


def _statements(chunks: List[dict]) -> List[_Statement]:
    out: List[_Statement] = []
    for idx, chunk in enumerate(chunks):
        doc_id = str(chunk.get("document_id"))
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        for raw in _SENTENCE_SPLIT_RE.split(text)[:_MAX_SENTENCES_PER_CHUNK]:
            sentence = " ".join(raw.split())[:_MAX_SENTENCE_CHARS]
            if len(sentence.split()) < 3:
                continue
            dates, masked = _extract_dates(sentence)
            words, negations = _content_words(masked)
            if not words:
                continue
            out.append(_Statement(
                doc_id=doc_id, chunk_index=idx, text=sentence, words=words,
                negations=negations, conditions=_condition_words(sentence),
                tokens=[_norm(t) for t in _WORD_RE.findall(masked)],
                numbers=_extract_numbers(masked), dates=dates,
            ))
    return out


# ── Detection ─────────────────────────────────────────────────────────────────

def _values_disagree(a: float, b: float) -> bool:
    baseline = max(abs(a), abs(b))
    return baseline > 0 and abs(a - b) / baseline >= _MIN_RELATIVE_DIFF


def _make_conflict(kind: str, a: _Statement, b: _Statement, value_a, value_b, shared: set) -> dict:
    return {
        "type": kind,
        "scope": "cross_document" if a.doc_id != b.doc_id else "intra_document",
        "documentA": a.doc_id,
        "documentB": b.doc_id,
        "chunkA": a.chunk_index,
        "chunkB": b.chunk_index,
        "valueA": value_a,
        "valueB": value_b,
        "statementA": a.text[:_STATEMENT_PREVIEW],
        "statementB": b.text[:_STATEMENT_PREVIEW],
        "context": " ".join(sorted(shared)),
        # None = heuristic only; True/False once verify_semantic_conflicts ran.
        "verified": True if kind in ("numeric", "date") else None,
    }


def _qualifier_before(tokens: list, shared: set) -> dict:
    """For each shared specific word, the nearest *specific* content word
    that precedes its first occurrence in the sentence ("Basic" before
    "plan", "Express" before "shipping", "Exchanges" before "processed").
    Stopwords/generic words are skipped over; a shared word is never its
    own qualifier."""
    out = {}
    for w in shared:
        if w in _GENERIC_WORDS:
            continue
        try:
            idx = tokens.index(w)
        except ValueError:
            continue
        for prev in reversed(tokens[:idx]):
            if prev in _STOPWORDS or len(prev) < 3:
                continue  # "are", "the", "of" sit between a qualifier and its noun
            # The first real token decides: a generic/shared word there means
            # the subject is unqualified ("within 30 days of purchase").
            if prev not in shared and prev not in _GENERIC_WORDS:
                out[w] = prev
            break
    return out


def _sibling_facts(a: _Statement, b: _Statement, shared: set) -> bool:
    """True when both statements qualify a shared subject word with a
    *different* specific word — "Basic plan … 10" vs "Pro plan … 20",
    "Standard shipping 5 days" vs "Express shipping 2 days", "Refunds …
    processed" vs "Exchanges … processed". Those are sibling facts, not a
    contradiction. A qualifier on one side only ("full refunds" vs
    "refunds") is fine — a general rule and a specific one can still clash."""
    qa, qb = _qualifier_before(a.tokens, shared), _qualifier_before(b.tokens, shared)
    for w in qa.keys() & qb.keys():
        if qa[w] != qb[w]:
            return True
    return False


def _pair_allowed(a: _Statement, b: _Statement, intra_document: bool) -> bool:
    if a.doc_id != b.doc_id:
        return True
    if not intra_document or a.text == b.text:
        return False
    return _jaccard(a.words, b.words) >= _MIN_JACCARD_INTRA


def _specific(shared: set) -> set:
    return {w for w in shared if w not in _GENERIC_WORDS}


def detect_conflicts(
    chunks: List[dict],
    intra_document: Optional[bool] = None,
    context_size: Optional[int] = None,
) -> List[dict]:
    """Scan retrieved chunks for numeric, date and semantic contradictions.

    `intra_document` (default: settings.RAG_CONFLICT_INTRA_DOCUMENT) also
    compares near-identical statements *within* one document; those are
    reported with scope="intra_document" and are disclosure-only downstream.
    `context_size`, when given, means only the first N chunks reach the
    model: a conflict is kept only if at least one side is among them, so
    tail-vs-tail disagreements never consume the report cap or the LLM
    verification budget.
    Returns at most settings.RAG_CONFLICT_MAX_REPORTED conflicts, cross-
    document first, strongest subject overlap first.
    """
    if intra_document is None:
        intra_document = settings.RAG_CONFLICT_INTRA_DOCUMENT
    stmts = _statements(chunks)
    found: list[tuple[tuple, dict]] = []
    seen: set = set()

    def _relevant(a: _Statement, b: _Statement) -> bool:
        return context_size is None or a.chunk_index < context_size or b.chunk_index < context_size

    for i in range(len(stmts)):
        a = stmts[i]
        for j in range(i + 1, len(stmts)):
            b = stmts[j]
            if not _relevant(a, b):
                continue
            if not _pair_allowed(a, b, intra_document):
                continue
            if not _conditions_compatible(a.conditions, b.conditions):
                continue
            sentence_shared = a.words & b.words
            if _sibling_facts(a, b, sentence_shared):
                continue

            # numeric: same local subject, compatible unit, different value
            for va, ua, wa in a.numbers:
                for vb, ub, wb in b.numbers:
                    if ua and ub and ua != ub:
                        continue
                    shared = wa & wb
                    if len(shared) < _MIN_SHARED_WORDS or not _specific(shared):
                        continue
                    if not _values_disagree(va, vb):
                        continue
                    if _sibling_facts(a, b, shared):
                        continue
                    key = ("numeric", a.doc_id, b.doc_id, frozenset(shared))
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(((a.doc_id == b.doc_id, -len(shared)), _make_conflict("numeric", a, b, va, vb, shared)))

            # date: same subject, different calendar date
            if a.dates and b.dates:
                shared = a.words & b.words
                if len(_specific(shared)) >= _MIN_SHARED_WORDS:
                    for da in a.dates:
                        for db_ in b.dates:
                            if da == db_:
                                continue
                            key = ("date", a.doc_id, b.doc_id, frozenset(shared))
                            if key in seen:
                                continue
                            seen.add(key)
                            found.append(((a.doc_id == b.doc_id, -len(shared)),
                                          _make_conflict("date", a, b, da.isoformat(), db_.isoformat(), shared)))

            # semantic: same statement, opposite polarity *about the shared subject*
            shared = a.words & b.words
            if len(_specific(shared)) >= _MIN_SHARED_SEMANTIC and _jaccard(a.words, b.words) >= _MIN_JACCARD_SEMANTIC:
                neg_a, neg_b = a.negated_for(shared), b.negated_for(shared)
                if neg_a != neg_b:
                    key = ("semantic", a.doc_id, b.doc_id, frozenset(shared))
                    if key not in seen:
                        seen.add(key)
                        found.append(((a.doc_id == b.doc_id, -len(shared)),
                                      _make_conflict("semantic", a, b,
                                                     "negated" if neg_a else "affirmed",
                                                     "negated" if neg_b else "affirmed", shared)))

    found.sort(key=lambda t: t[0])
    return [c for _, c in found[: settings.RAG_CONFLICT_MAX_REPORTED]]


# ── Optional LLM confirmation of semantic candidates ─────────────────────────

_verify_cache: "OrderedDict[str, str]" = OrderedDict()
_VERIFY_CACHE_MAX = 512
_VERIFY_TTL = 7 * 24 * 3600


def _verify_key(a: str, b: str) -> str:
    first, second = sorted([a.strip(), b.strip()])
    return "conflict:v1:" + hashlib.sha256(f"{first}\n||\n{second}".encode("utf-8")).hexdigest()


async def _cached_verdict(key: str) -> Optional[str]:
    if key in _verify_cache:
        _verify_cache.move_to_end(key)
        return _verify_cache[key]
    from app.core.redis_client import get_redis
    redis = get_redis()
    if redis is not None:
        try:
            hit = await redis.get(key)
            if hit:
                return json.loads(hit)
        except Exception:
            pass
    return None


async def _store_verdict(key: str, verdict: str) -> None:
    _verify_cache[key] = verdict
    _verify_cache.move_to_end(key)
    if len(_verify_cache) > _VERIFY_CACHE_MAX:
        _verify_cache.popitem(last=False)
    from app.core.redis_client import get_redis
    redis = get_redis()
    if redis is not None:
        try:
            await redis.set(key, json.dumps(verdict), ex=_VERIFY_TTL)
        except Exception:
            pass


async def _llm_verdict(stmt_a: str, stmt_b: str) -> Optional[str]:
    """CONTRADICT / CONSISTENT / UNRELATED from the cheap classification
    model, or None when the call fails or returns something else."""
    from app.services.llm import router as llm_router

    prompt = (
        "Two statements come from a company's own documents.\n"
        f"STATEMENT A: {stmt_a[:500]}\n"
        f"STATEMENT B: {stmt_b[:500]}\n\n"
        "Do A and B make incompatible claims about the SAME subject under the SAME "
        "conditions? Answer with exactly one word: CONTRADICT, CONSISTENT, or UNRELATED."
    )
    try:
        result = await asyncio.wait_for(
            llm_router.classify(
                prompt, provider_name="gemini", model=settings.GEMINI_CLASSIFICATION_MODEL, max_tokens=5,
            ),
            timeout=8,
        )
        word = (result.text or "").strip().upper().split()[:1]
        verdict = word[0].strip(".,") if word else ""
        return verdict if verdict in {"CONTRADICT", "CONSISTENT", "UNRELATED"} else None
    except Exception as e:
        logger.info("semantic-conflict verification skipped: %s", e)
        return None


async def verify_semantic_conflicts(conflicts: List[dict]) -> List[dict]:
    """Confirm heuristic `semantic` candidates with the classification model
    (at most RAG_CONFLICT_LLM_MAX_PAIRS calls, cached). Confirmed →
    verified=True; judged consistent/unrelated → dropped; model unavailable
    → kept with verified=None so the answer still discloses a *possible*
    disagreement rather than hiding it. Numeric/date conflicts pass through."""
    if not settings.RAG_CONFLICT_LLM_VERIFY:
        return conflicts
    candidates = [c for c in conflicts if c["type"] == "semantic" and c.get("verified") is None]
    if not candidates:
        return conflicts
    budget = candidates[: settings.RAG_CONFLICT_LLM_MAX_PAIRS]

    async def _one(c: dict) -> None:
        key = _verify_key(c["statementA"], c["statementB"])
        verdict = await _cached_verdict(key)
        if verdict is None:
            verdict = await _llm_verdict(c["statementA"], c["statementB"])
            if verdict is not None:
                await _store_verdict(key, verdict)
        if verdict == "CONTRADICT":
            c["verified"] = True
        elif verdict in ("CONSISTENT", "UNRELATED"):
            c["verified"] = False
        # None → leave as heuristic

    await asyncio.gather(*(_one(c) for c in budget))
    return [c for c in conflicts if c.get("verified") is not False]


# ── Prompt rendering ──────────────────────────────────────────────────────────

def _fmt_value(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _describe(c: dict, name_a: str, name_b: str) -> str:
    if c["type"] == "semantic":
        return f'{name_a} says "{c["statementA"]}" but {name_b} says "{c["statementB"]}"'
    return (f"{name_a} says {_fmt_value(c['valueA'])} but {name_b} says "
            f"{_fmt_value(c['valueB'])} (about: {c['context']})")


def conflict_prompt_note(conflicts: List[dict], doc_names: Optional[dict], resolutions: Optional[List[dict]] = None) -> str:
    """Render detected conflicts (and, when given, their resolutions) as a
    STYLE-RULES instruction. Never placed inside the untrusted CONTEXT block.
    Empty string when there is nothing to say."""
    if not conflicts:
        return ""
    names = doc_names or {}
    resolved_lines, unresolved_lines = [], []
    for i, c in enumerate(conflicts):
        if c.get("scope") == "intra_document":
            # Kept in metrics for evaluation, but "X says 10 but X says 20"
            # is not an instruction the model can act on sensibly.
            continue
        name_a = names.get(c["documentA"]) or "one source"
        name_b = names.get(c["documentB"]) or "another source"
        res = (resolutions or [None] * len(conflicts))[i] if resolutions else None
        if res and res.get("status") == "resolved":
            winner = names.get(res["winner"]) or "the authoritative source"
            loser = names.get(res["loser"]) or "the outdated source"
            resolved_lines.append(
                f"{_describe(c, name_a, name_b)}. {winner} is the authoritative, current source "
                f"({res.get('reason')}); answer with {winner}'s statement and cite {winner}. "
                f"Mention {loser}'s figure only to say it is outdated, and only if the user asks."
            )
        else:
            unresolved_lines.append(_describe(c, name_a, name_b))
    parts = []
    if resolved_lines:
        parts.append("Some sources disagreed, and the workspace's verified metadata settles it: " + " ".join(resolved_lines))
    if unresolved_lines:
        parts.append(
            "The retrieved sources CONTRADICT each other and nothing settles which is current: "
            + "; ".join(unresolved_lines)
            + ". Do not silently pick one — briefly point out that the sources disagree, present "
            "both positions with their source names, and suggest confirming the current one."
        )
    return " ".join(parts)
