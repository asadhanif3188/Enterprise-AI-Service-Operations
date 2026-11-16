"""RFC 8785 (JSON Canonicalization Scheme) and the ADR-0004 request fingerprint.

Pure functions; no I/O and no SQL. Used by the authorized case operations to
fingerprint a keyed create request (fingerprint format version 1):

    SHA-256 over the RFC 8785 serialization of exactly
    {"organization_id": <lowercase hyphenated UUID>, "title": <exact text>,
     "description": <exact text>}

stored as 64 lowercase hexadecimal characters. Title and description are
used exactly as validated and persisted: no trimming, case folding, or
Unicode normalization. Any change to the field set, the value rules, or the
algorithm requires a new ``FINGERPRINT_VERSION``.

The serializer is a minimal RFC 8785 implementation, verified against the
RFC's examples in the tests. It supports what I-JSON (RFC 7493) data can
contain: objects, arrays, strings, booleans, null, and numbers serialized with
the ECMAScript Number-to-string rules RFC 8785 requires. It rejects lone
surrogates, non-finite numbers, integers beyond the IEEE 754 exact range, and
non-string object keys.
"""

from __future__ import annotations

import hashlib
import math
from uuid import UUID

FINGERPRINT_VERSION = 1

_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r",
            0x22: '\\"', 0x5C: "\\\\"}
_MAX_EXACT_INTEGER = 2 ** 53


def _serialize_string(value: str) -> str:
    """ECMAScript JSON.stringify string quoting (RFC 8785 section 3.2.2.2)."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError("lone surrogate is not valid I-JSON")
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize_number(value: float) -> str:
    """ECMAScript Number::toString for finite doubles (RFC 8785 section 3.2.2.3)."""
    if not math.isfinite(value):
        raise ValueError("NaN and Infinity are not valid JSON")
    if value == 0:
        return "0"  # also -0
    sign = "-" if value < 0 else ""
    # repr() gives the shortest round-tripping decimal digits, as ECMAScript requires.
    text = repr(abs(value))
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part).lstrip("0")
    # n: position of the decimal point relative to the start of the digits.
    n = len(int_part.lstrip("0")) + exponent if int_part.strip("0") else \
        exponent - (len(frac_part) - len(frac_part.lstrip("0")))
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        body = (digits if k == 1 else digits[0] + "." + digits[1:]) + "e" + exp
    return sign + body


def canonical_json(value: object) -> str:
    """Serialize I-JSON data per RFC 8785."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, int):
        if abs(value) > _MAX_EXACT_INTEGER:
            raise ValueError("integer outside the IEEE 754 exact range")
        return _serialize_number(float(value))
    if isinstance(value, float):
        return _serialize_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("object keys must be strings")
        # Sort by UTF-16 code units (RFC 8785 section 3.2.3).
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_serialize_string(key) + ":" + canonical_json(value[key])
                              for key in keys) + "}"
    raise ValueError("value is not representable in I-JSON")


def case_create_fingerprint(organization_id: UUID, title: str, description: str) -> str:
    """Fingerprint format version 1 for a keyed ``case:create`` request."""
    if not isinstance(organization_id, UUID):
        raise TypeError("organization_id must be a UUID")
    if not isinstance(title, str) or not isinstance(description, str):
        raise TypeError("title and description must be text")
    payload = {"organization_id": str(organization_id),  # lowercase, hyphenated
               "title": title, "description": description}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
