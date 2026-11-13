"""Short IDs that users type: `R-7K2PQ` for a report, `D-4F8KQ` for a delete batch.

Five characters of Crockford base32. Letter case does not matter, and the letters O, I and L
read as 0, 1 and 1 (spec 32, B1).
"""

from __future__ import annotations

import re
import secrets

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ID_LENGTH = 5
_LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})
_SHAPE = re.compile(rf"^([A-Z])-?([{CROCKFORD}]{{{ID_LENGTH}}})$")


def new_id(prefix: str) -> str:
    return f"{prefix}-" + "".join(secrets.choice(CROCKFORD) for _ in range(ID_LENGTH))


def normalize_id(text: str, prefix: str) -> str | None:
    """The canonical ID, or None when the text is not an ID with this prefix."""
    raw = re.sub(r"\s+", "", text or "").upper()
    if raw.startswith(prefix):
        raw = prefix + raw[len(prefix) :].translate(_LOOKALIKES)
    match = _SHAPE.match(raw)
    if not match or match.group(1) != prefix:
        return None
    return f"{prefix}-{match.group(2)}"


def is_report_id(text: str) -> bool:
    return normalize_id(text, "R") == text
