"""Responsible-AI moderation for natural-language search queries.

HomeCam deliberately cannot answer "who was that?". The detector reports
*what* it saw (person, vehicle, package, animal), never *who*, and identity
is only ever attached by a human (see ``app.services.persons``). Search is
the one place a user can phrase an arbitrary question, so it is also the
one place that has to say no.

Two kinds of pattern, which is what makes the difference between a refusal
and a quiet strip:

* **intent** patterns - the query is *asking* for a forbidden inference
  ("who is this", "what gender", "how old", "read the plate"). Answering a
  reduced version of that question would be answering the wrong question,
  so the whole query is refused with an explanation.
* **attribute** patterns - the query merely *describes* a subject with a
  forbidden attribute ("man in the driveway last night"). The attribute is
  stripped, the remainder is searched, and the user is told what was
  ignored - because the answerable part of that question is genuinely
  useful.

Nothing here consults a model: the rules are a fixed list, so the same
query always gets the same answer and a provider outage cannot silently
turn the guardrail off.

Forbidden categories (see docs/ai-features.md):

``identity``  face/facial recognition, "who is", naming a stranger
``gender``    gender or sex inference
``ethnicity`` ethnicity, race, skin colour, nationality
``age``       age or age-group inference
``plate``     licence-plate / number-plate reading (out of scope in this batch)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Asking for the inference itself. Always refused.
INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "identity": (
        r"facial recognition",
        r"face recognition",
        r"recogni[sz]e (?:the |their |his |her )?face",
        r"\bidentify\b",
        r"who(?:'s| is| was| are| were)\b",
        r"\bwhose\b",
        r"what(?:'s| is| was)? (?:their|his|her) name",
        r"name of the (?:person|man|woman|visitor|stranger)",
        r"\bidentity\b",
        r"\bfacial\b",
        r"\bfaces?\b",
    ),
    "gender": (
        r"\bgenders?\b",
        r"\bsex of\b",
    ),
    "ethnicity": (
        r"\bethnicit(?:y|ies)\b",
        r"\bethnic\b",
        r"\bracial\b",
        r"\brace\b",
        r"skin colou?r",
        r"\bnationalit(?:y|ies)\b",
    ),
    "age": (
        r"how old",
        r"\bages?\b",
        r"\baged\b",
    ),
    "plate": (
        r"licen[cs]e plates?",
        r"number plates?",
        r"plate numbers?",
        r"\bregistration (?:plate|number)\b",
        r"\banpr\b",
        r"\blpr\b",
        r"\bplates?\b",
    ),
}

#: Describing a subject by a forbidden attribute. Stripped, then searched.
ATTRIBUTE_PATTERNS: dict[str, tuple[str, ...]] = {
    "gender": (
        r"\bmale\b",
        r"\bfemale\b",
        r"\bmen\b",
        r"\bwomen\b",
        r"\bman\b",
        r"\bwoman\b",
        r"\bguy\b",
        r"\blady\b",
    ),
    "ethnicity": (r"\bblack or white\b",),
    "age": (
        r"\bteenagers?\b",
        r"\belderly\b",
        r"\bpensioners?\b",
        r"\bminors?\b",
    ),
}

#: Human-readable explanation per category, used in refusals and notices.
CATEGORY_REASONS: dict[str, str] = {
    "identity": "HomeCam does not do face recognition and cannot tell you who someone is. "
    "Names only ever come from a person you have labelled yourself.",
    "gender": "HomeCam does not infer gender.",
    "ethnicity": "HomeCam does not infer ethnicity, race, skin colour or nationality.",
    "age": "HomeCam does not infer age.",
    "plate": "HomeCam does not read licence plates.",
}

_ORDER = ("identity", "gender", "ethnicity", "age", "plate")
_WORD_RE = re.compile(r"[a-z0-9]+")
#: Words that carry no search meaning on their own, so a query left with
#: only these after stripping is treated as empty rather than searched.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "at", "be", "by", "did", "do", "for", "from",
        "had", "has", "have", "in", "is", "it", "me", "my", "of", "on", "or", "our",
        "show", "that", "the", "their", "them", "there", "they", "this", "to", "was",
        "were", "what", "when", "which", "with", "you", "your",
    }
)

_SUGGESTION = (
    'Try searching for what happened instead, for example "person at the front door last night".'
)


@dataclass(frozen=True)
class ModerationResult:
    """Outcome of moderating one search query."""

    allowed: bool
    #: The query actually searched (forbidden phrases removed). Empty when
    #: the query was refused.
    query: str
    #: Forbidden categories that were matched, in a stable order.
    categories: tuple[str, ...]
    #: Explanation for the user; ``None`` when nothing was matched.
    message: str | None

    @property
    def refused(self) -> bool:
        return not self.allowed


def _explain(categories: tuple[str, ...]) -> str:
    return " ".join(CATEGORY_REASONS[category] for category in categories)


def _order(categories: set[str]) -> tuple[str, ...]:
    return tuple(name for name in _ORDER if name in categories)


def _meaningful(text: str) -> bool:
    return any(word not in _STOPWORDS for word in _WORD_RE.findall(text.lower()))


def moderate_query(raw: str) -> ModerationResult:
    """Strip or refuse the identity-seeking parts of a search query."""
    text = (raw or "").strip()
    if not text:
        return ModerationResult(False, "", (), "Type something to search for.")

    intents = {
        category
        for category, patterns in INTENT_PATTERNS.items()
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)
    }
    if intents:
        categories = _order(intents)
        return ModerationResult(False, "", categories, f"{_explain(categories)} {_SUGGESTION}")

    cleaned = text
    matched: set[str] = set()
    for category, patterns in ATTRIBUTE_PATTERNS.items():
        for pattern in patterns:
            cleaned, count = re.subn(pattern, " ", cleaned, flags=re.IGNORECASE)
            if count:
                matched.add(category)

    if not matched:
        return ModerationResult(True, text, (), None)

    categories = _order(matched)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.,!-")
    reason = _explain(categories)
    if not _meaningful(cleaned):
        return ModerationResult(False, "", categories, f"{reason} {_SUGGESTION}")
    return ModerationResult(True, cleaned, categories, f"{reason} Searched for '{cleaned}' instead.")
