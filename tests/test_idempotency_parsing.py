"""Offline conformance tests for keyed case creation (ADR-0004).

* ``src/api/structured_fields.py``: RFC 9651 Item parsing, using the RFC's own
  examples and cases in the style of the HTTP WG structured-field test suite.
* ``src/request_fingerprint.py``: RFC 8785 canonical JSON, using the RFC's
  examples, and the fingerprint format version 1 rules.
* The authorized layer's own key validation, before any database access.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from uuid import UUID, uuid4

import pytest

from src.api.structured_fields import (
    ByteSequence,
    Date,
    DisplayString,
    StructuredFieldError,
    Token,
    parse_item,
    parse_string_item,
)
from src.authorized_case_operations import AuthorizedCaseOperations
from src.case_authorization import InvalidCaseRequest
from src.request_fingerprint import (
    FINGERPRINT_VERSION,
    canonical_json,
    case_create_fingerprint,
)

# =============================================================================
# RFC 9651 Item parsing
# =============================================================================

# Examples given in RFC 9651 section 3 (Items, Parameters, and each bare-item type).
RFC_9651_EXAMPLES = [
    ("42", 42),
    ("-42", -42),
    ("4.5", 4.5),
    ('"hello world"', "hello world"),
    ("foo123/456", Token("foo123/456")),
    (":cHJldGVuZCB0aGlzIGlzIGJpbmFyeSBjb250ZW50Lg==:",
     ByteSequence("cHJldGVuZCB0aGlzIGlzIGJpbmFyeSBjb250ZW50Lg==")),
    ("?1", True),
    ("@1659578233", Date(1659578233)),
    ('%"This is intended for display to %c3%bcsers."',
     DisplayString("This is intended for display to \u00fcsers.")),
]


@pytest.mark.parametrize("text,value", RFC_9651_EXAMPLES)
def test_rfc_9651_examples_parse_to_the_documented_values(text, value):
    assert parse_item(text).value == value


def test_rfc_9651_item_parameters_are_parsed_and_kept_separate():
    item = parse_item("5; foo=bar")
    assert item.value == 5 and item.parameters == (("foo", Token("bar")),)
    item = parse_item('"k"; a=1; b="x"; c; a=2')
    assert item.value == "k" and dict(item.parameters) == {"a": 2, "b": "x", "c": True}


# String cases in the style of the HTTP WG structured-field-tests (string.json).
@pytest.mark.parametrize("text,expected", [
    ('"foo bar"', "foo bar"),
    ('""', ""),
    ('"' + "x" * 262 + '"', "x" * 262),
    ('"   "', "   "),
    ('"foo \\"bar\\""', 'foo "bar"'),
    ('"foo \\\\bar"', "foo \\bar"),
    ('"foo \\""', 'foo "'),
    ('  "padded"  ', "padded"),
])
def test_valid_strings(text, expected):
    assert parse_string_item(text) == expected


@pytest.mark.parametrize("text", [
    '"f\u00fc\u00fc"',          # non-ASCII
    '"\t"',                     # tab inside a string
    '" \n "',                   # newline inside a string
    "'foo'",                    # single quotes
    '"foo',                     # unbalanced
    '"foo \\,"',                # bad escape
    '"foo \\"',                 # escape swallows the closing quote
    '"foo \\',                  # ends inside an escape
    '"a" "b"',                  # two items
    '"a", "b"',                 # a list, not an item
    "",                         # empty
    "\t\"k\"",                  # leading tab (only SP may be discarded)
    '"k" ;a=1',                 # whitespace before parameters
    '"k";A=1',                  # uppercase parameter key
    '"k"; a=',                  # parameter without a value
    '"k";',                     # dangling separator
])
def test_invalid_items_are_rejected(text):
    with pytest.raises(StructuredFieldError):
        parse_item(text)


@pytest.mark.parametrize("text,value", [
    ("0", 0), ("-0", 0), ("0002", 2), ("123456789012345", 123456789012345),
    ("123456789012.123", 123456789012.123), ("1.5", 1.5), ("?0", False),
    ("*foo", Token("*foo")), ("a_b-c.d3:f%00/*", Token("a_b-c.d3:f%00/*")),
    (":aGVsbG8=:", ByteSequence("aGVsbG8=")), (":aGVsbG8:", ByteSequence("aGVsbG8")),
    (":YQ==:", ByteSequence("YQ==")), ('%"f%c3%bc%c3%bc"', DisplayString("f\u00fc\u00fc")),
])
def test_non_string_items_parse_but_are_not_strings(text, value):
    assert parse_item(text).value == value
    with pytest.raises(StructuredFieldError):
        parse_string_item(text)


@pytest.mark.parametrize("text", [
    "-", "--1", "1.", "1.1234", "1234567890123456", "1234567890123.1", "1.5.4",
    "?2", "?", "@1.5", ":a:", ":aGVs=bG8=:", ":a$b:", ":aGVsbG8=", '%"%C3%BC"', '%"%c3%28"',
    '%"unterminated', "%foo", "&", "(1 2)",
])
def test_malformed_non_string_items_are_rejected(text):
    with pytest.raises(StructuredFieldError):
        parse_item(text)


def test_parameters_are_ignored_for_the_string_value():
    key = "8e03978e-40d5-43e8-bc93-6894a57f9324"
    assert parse_string_item(f'"{key}"') == key
    assert parse_string_item(f'"{key}"; ttl=60; v="2"') == key


def test_non_ascii_field_values_are_rejected():
    with pytest.raises(StructuredFieldError):
        parse_item('"caf\u00e9-key-0000000000"')


# =============================================================================
# RFC 8785 canonical JSON
# =============================================================================

def test_rfc_8785_main_example():
    # RFC 8785 section 3.2.2 input and its canonical form (section 3.2.4).
    source = ('{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],'
              ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
              ' "literals": [null, true, false]}')
    expected = ('{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
                '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}')
    assert canonical_json(json.loads(source)) == expected


def test_rfc_8785_property_sorting_example():
    # RFC 8785 section 3.2.3: sorting by UTF-16 code units.
    source = json.loads('{"\\u20ac": "Euro Sign", "\\r": "Carriage Return", '
                        '"\\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One", '
                        '"\\ud83d\\ude00": "Emoji: Grinning Face", "\\u0080": "Control", '
                        '"\\u00f6": "Latin Small Letter O With Diaeresis"}')
    expected = ('{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
                '"\u00f6":"Latin Small Letter O With Diaeresis","\u20ac":"Euro Sign",'
                '"\U0001F600":"Emoji: Grinning Face","\ufb33":"Hebrew Letter Dalet With Dagesh"}')
    assert canonical_json(source) == expected


@pytest.mark.parametrize("number,expected", [
    # RFC 8785 Appendix B samples and ECMAScript boundary cases.
    (0.0, "0"), (-0.0, "0"), (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
    (9007199254740992, "9007199254740992"), (295147905179352830000.0, "295147905179352830000"),
    (1e21, "1e+21"), (1e20, "100000000000000000000"), (0.000001, "0.000001"), (1e-7, "1e-7"),
    (1e16, "10000000000000000"), (0.00001, "0.00001"), (-1.5, "-1.5"), (100.0, "100"), (2.5e-5, "0.000025"),
])
def test_rfc_8785_number_serialization(number, expected):
    assert canonical_json(number) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2 ** 53 + 1, "\ud800", {"a": "\udfff"},
                                 {1: "x"}, b"bytes", object()])
def test_values_outside_i_json_are_rejected(bad):
    with pytest.raises(ValueError):
        canonical_json(bad)


def test_string_escaping_follows_ecmascript():
    assert canonical_json("\u001f\u007f\u2028\b\t\n\f\r\"\\/") == '"\\u001f\u007f\u2028\\b\\t\\n\\f\\r\\"\\\\/"'


# =============================================================================
# Fingerprint format version 1
# =============================================================================

ORG = UUID("11111111-1111-4111-8111-111111111111")


def test_fingerprint_golden_vector():
    canonical = ('{"description":"Fictional detail","organization_id":'
                 '"11111111-1111-4111-8111-111111111111","title":"VPN down"}')
    assert canonical_json({"title": "VPN down", "organization_id": str(ORG),
                           "description": "Fictional detail"}) == canonical
    assert case_create_fingerprint(ORG, "VPN down", "Fictional detail") == \
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert FINGERPRINT_VERSION == 1


def test_fingerprint_equivalences():
    base = case_create_fingerprint(ORG, "Caf\u00e9", "d")
    # Escaped and literal JSON spellings decode to the same text.
    assert json.loads('"Caf\\u00e9"') == json.loads('"Caf\u00e9"')
    assert case_create_fingerprint(ORG, json.loads('"Caf\\u00e9"'), "d") == base
    # The organization is always the lowercase hyphenated form.
    assert case_create_fingerprint(UUID(str(ORG).upper()), "Caf\u00e9", "d") == base


@pytest.mark.parametrize("title", [
    unicodedata.normalize("NFD", "Caf\u00e9"),  # NFD differs from NFC
    "Caf\u00e9 ",                               # trailing space
    "caf\u00e9",                                # case
    "CAF\u00c9",
])
def test_fingerprint_keeps_distinct_text_distinct(title):
    assert case_create_fingerprint(ORG, title, "d") != case_create_fingerprint(ORG, "Caf\u00e9", "d")


def test_fingerprint_rejects_lone_surrogates_and_wrong_types():
    with pytest.raises(ValueError):
        case_create_fingerprint(ORG, "\ud800", "d")
    with pytest.raises(TypeError):
        case_create_fingerprint(str(ORG), "t", "d")  # type: ignore[arg-type]


# =============================================================================
# Layer key validation (before any database access)
# =============================================================================

UNREACHABLE_DSN = "postgresql://nobody:unused@127.0.0.1:1/eaiso_test"


class StaticResolver:
    def __init__(self, actor):
        self.actor = actor

    def resolve(self, credential):
        from src.credential_auth import VerifiedActor

        return VerifiedActor(self.actor, "0000000000000000")


class SneakyStr(str):
    pass


@pytest.mark.parametrize("key", [None, "", "short", "x" * 129, "has space 0000000000", "caf\u00e9-key-0000000000",
                                 '"quoted-key-000000000"', 12345678901234567, SneakyStr("valid-key-000000000")])
def test_layer_rejects_malformed_keys_before_database_offline(key):
    ops = AuthorizedCaseOperations(StaticResolver(uuid4()), UNREACHABLE_DSN)
    with pytest.raises(InvalidCaseRequest) as info:
        ops.create_case_keyed("cred", str(uuid4()), "t", "d", key)
    assert info.value.reason == "idempotency_key_malformed"


def test_layer_validates_text_before_fingerprinting_offline():
    ops = AuthorizedCaseOperations(StaticResolver(uuid4()), UNREACHABLE_DSN)
    with pytest.raises(InvalidCaseRequest) as info:
        ops.create_case_keyed("cred", str(uuid4()), "lone \ud800", "d", "valid-key-000000000")
    assert info.value.reason == "title_invalid_character"


def test_idempotency_wait_must_be_bounded_below_statement_timeout_offline():
    for bad in (0, 10_000, 20_000):
        with pytest.raises(ValueError):
            AuthorizedCaseOperations(StaticResolver(uuid4()), UNREACHABLE_DSN, idempotency_wait_ms=bad)
