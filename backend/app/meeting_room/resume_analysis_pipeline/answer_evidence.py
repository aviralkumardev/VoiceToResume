"""The answer grader may report that one answer also
covered OTHER targets (`also_covered`). Left unchecked that is a silent failure
in both directions -- a wrong entry writes bad data AND suppresses a question
that should have been asked. Requiring the grader to quote the candidate back
verbatim, and verifying that quote actually appears in the answer, turns an
unfalsifiable claim into a checkable one.
"""

import re
from typing import List

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+", re.UNICODE)


def normalize(text: str) -> str:
    """Casefold, strip punctuation to spaces, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    return _WS.sub(" ", _PUNCT.sub(" ", text)).strip().casefold()


def evidence_tokens(text: str) -> List[str]:
    normalized = normalize(text)
    return normalized.split(" ") if normalized else []


def evidence_matches(evidence: str, answer_text: str, *, min_tokens: int = 3) -> bool:
    """True when `evidence` is plausibly a verbatim span of `answer_text`.

    Three rules, cheapest first:
    1. Reject anything shorter than `min_tokens` words -- a one-word "quote"
       proves nothing and would match almost any answer.
    2. Accept an exact normalized substring. This is the common case.
    3. Otherwise accept only if every evidence token appears in the answer
       IN ORDER, inside a window of 3x the evidence length.

    Rule 3 exists because `answer_text` is raw Sarvam STT output, disfluencies
    and all. Any model asked to quote it will silently drop an "um" or a
    repeated word. Exact-only matching would therefore fail CLOSED -- dropping
    genuine coverage and re-asking questions the candidate already answered,
    which is the exact redundancy this feature exists to prevent.

    Deliberately NOT edit-distance/difflib: a ratio threshold is untunable
    without a corpus, and a high-ratio false positive is a *silent* bad
    settlement. The ordered-window rule is deterministic and explainable in a
    log line, which matters for something that sits on a trust boundary.
    """
    tokens = evidence_tokens(evidence)
    if len(tokens) < min_tokens:
        return False

    answer_norm = normalize(answer_text)
    if not answer_norm:
        return False

    if " ".join(tokens) in answer_norm:
        return True

    answer_tokens = answer_norm.split(" ")
    window = 3 * len(tokens)

    # Try each possible start so a late-but-contiguous run still matches.
    for start in range(len(answer_tokens)):
        if answer_tokens[start] != tokens[0]:
            continue
        matched = 1
        for pos in range(start + 1, min(start + window, len(answer_tokens))):
            if answer_tokens[pos] == tokens[matched]:
                matched += 1
                if matched == len(tokens):
                    return True
    return False
