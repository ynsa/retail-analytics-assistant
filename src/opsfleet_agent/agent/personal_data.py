"""Words of a request for personal data (spec 30 A2.4, spec 50 round 1).

Code uses this test only to choose the text of a refusal, never to allow anything. The guard and
the masker of spec 30 keep personal data out of the queries and the results.
"""

from __future__ import annotations

import re

_PERSONAL_DATA = re.compile(
    r"\b(?:e-?mails|e-?mail\s+of|(?:street|home|e-?mail)\s+address(?:es)?|addresses"
    r"|phone(?:\s+numbers?)?"
    r"|(?:first|last|full|customer)\s+names?|latitude|longitude|coordinates"
    r"|(?:postal|zip)\s+codes?|raw\s+users\s+table)\b",
    re.IGNORECASE,
)


def asks_personal_data(text: str) -> bool:
    """True when the message names personal data: names, emails, addresses, phones, locations.
    "Approved by email" is not a request for personal data, so a single "email" does not count."""
    return bool(_PERSONAL_DATA.search(text or ""))


# A customer ID together with a demographic field (spec 30, A2.5): "the user_id, city and age".
_CUSTOMER_ID = re.compile(r"\b(?:user_?ids?|customer\s+ids?)\b", re.IGNORECASE)
_DEMOGRAPHIC = re.compile(
    r"\b(?:city|cities|state|states|country|countries|age|ages|gender|traffic\s+source)\b",
    re.IGNORECASE,
)


def asks_id_with_demographics(text: str) -> bool:
    """True when the message asks for customer IDs together with a demographic field."""
    text = text or ""
    return bool(_CUSTOMER_ID.search(text) and _DEMOGRAPHIC.search(text))
