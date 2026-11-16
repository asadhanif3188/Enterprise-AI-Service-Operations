"""Minimal RFC 9651 (Structured Field Values) parser for Item fields.

Implements the Item parsing algorithms of RFC 9651 section 4.2 (4.2.3 Item,
4.2.3.1 Bare Item, 4.2.3.2 Parameters, 4.2.3.3 Key, 4.2.4 Integer or Decimal,
4.2.5 String, 4.2.6 Token, 4.2.7 Byte Sequence, 4.2.8 Boolean, 4.2.9 Date,
4.2.10 Display String). Any failure raises ``StructuredFieldError``; this
module never guesses. Lists and Dictionaries are not needed and not parsed.

Used only to read ``Idempotency-Key``, which ADR-0004 defines as an Item whose
value must be a String.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIGITS = "0123456789"
_LCALPHA = "abcdefghijklmnopqrstuvwxyz"
_ALPHA = _LCALPHA + _LCALPHA.upper()
_TCHAR = "!#$%&'*+-.^_`|~" + _DIGITS + _ALPHA
_KEY_CHARS = _LCALPHA + _DIGITS + "_-.*"
_BASE64_CHARS = _ALPHA + _DIGITS + "+/="
_LOWER_HEX = "0123456789abcdef"
_BASE64_SHAPE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_BASE64_CORE = re.compile(r"[A-Za-z0-9+/]*")


class StructuredFieldError(ValueError):
    """The field value is not a valid RFC 9651 Item."""


@dataclass(frozen=True)
class Token:
    value: str


@dataclass(frozen=True)
class DisplayString:
    value: str


@dataclass(frozen=True)
class ByteSequence:
    """Validated base64 text (not decoded: this module only needs to classify items)."""

    base64_text: str


@dataclass(frozen=True)
class Date:
    value: int


@dataclass(frozen=True)
class Item:
    value: object            # str, Token, int, float, ByteSequence, bool, Date, DisplayString
    parameters: tuple        # ((key, value), ...) in order, later duplicates win


class _Input:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        ch = self.peek()
        self.pos += 1
        return ch

    def at_end(self) -> bool:
        return self.pos >= len(self.text)

    def skip_sp(self) -> None:
        while self.peek() == " ":
            self.pos += 1


def parse_item(field_value: str) -> Item:
    """RFC 9651 section 4.2: parse a field value as an Item, or raise."""
    if not isinstance(field_value, str):
        raise StructuredFieldError("field value must be text")
    try:
        field_value.encode("ascii")
    except UnicodeEncodeError:
        raise StructuredFieldError("field value must be ASCII") from None
    data = _Input(field_value)
    data.skip_sp()
    value = _bare_item(data)
    parameters = _parameters(data)
    data.skip_sp()
    if not data.at_end():
        raise StructuredFieldError("unexpected characters after the item")
    return Item(value, parameters)


def parse_string_item(field_value: str) -> str:
    """Parse an Item that must be a String; parameters are ignored."""
    item = parse_item(field_value)
    if not isinstance(item.value, str):
        raise StructuredFieldError("item is not a String")
    return item.value


def _bare_item(data: _Input) -> object:
    ch = data.peek()
    if ch == "-" or (ch and ch in _DIGITS):
        return _integer_or_decimal(data)
    if ch == '"':
        return _string(data)
    if ch == "*" or (ch and ch in _ALPHA):
        return _token(data)
    if ch == ":":
        return _byte_sequence(data)
    if ch == "?":
        return _boolean(data)
    if ch == "@":
        return _date(data)
    if ch == "%":
        return _display_string(data)
    raise StructuredFieldError("unrecognized item type")


def _parameters(data: _Input) -> tuple:
    params: dict[str, object] = {}
    while data.peek() == ";":
        data.take()
        data.skip_sp()
        key = _key(data)
        value: object = True
        if data.peek() == "=":
            data.take()
            value = _bare_item(data)
        params[key] = value  # a later duplicate overwrites, keeping first position
    return tuple(params.items())


def _key(data: _Input) -> str:
    ch = data.peek()
    if not ch or (ch not in _LCALPHA and ch != "*"):
        raise StructuredFieldError("invalid parameter key")
    out = []
    while data.peek() and data.peek() in _KEY_CHARS:
        out.append(data.take())
    return "".join(out)


def _integer_or_decimal(data: _Input) -> object:
    sign = 1
    if data.peek() == "-":
        data.take()
        sign = -1
    if not data.peek() or data.peek() not in _DIGITS:
        raise StructuredFieldError("number must start with a digit")
    out = ""
    is_decimal = False
    while not data.at_end():
        ch = data.peek()
        if ch in _DIGITS:
            out += data.take()
        elif not is_decimal and ch == ".":
            if len(out) > 12:
                raise StructuredFieldError("decimal integer part too long")
            out += data.take()
            is_decimal = True
        else:
            break
        if not is_decimal and len(out) > 15:
            raise StructuredFieldError("integer too long")
        if is_decimal and len(out) > 16:
            raise StructuredFieldError("decimal too long")
    if not is_decimal:
        return sign * int(out)
    if out.endswith("."):
        raise StructuredFieldError("decimal must have a fraction")
    if len(out) - out.index(".") - 1 > 3:
        raise StructuredFieldError("decimal fraction too long")
    return sign * float(out)


def _string(data: _Input) -> str:
    data.take()  # opening DQUOTE
    out = []
    while not data.at_end():
        ch = data.take()
        if ch == "\\":
            if data.at_end():
                raise StructuredFieldError("string ends inside an escape")
            nxt = data.take()
            if nxt not in ('"', "\\"):
                raise StructuredFieldError("invalid escape in string")
            out.append(nxt)
        elif ch == '"':
            return "".join(out)
        elif ord(ch) < 0x20 or ord(ch) > 0x7E:
            raise StructuredFieldError("invalid character in string")
        else:
            out.append(ch)
    raise StructuredFieldError("unterminated string")


def _token(data: _Input) -> Token:
    out = [data.take()]
    while data.peek() and (data.peek() in _TCHAR or data.peek() in ":/"):
        out.append(data.take())
    return Token("".join(out))


def _byte_sequence(data: _Input) -> ByteSequence:
    data.take()  # opening ':'
    out = []
    while True:
        if data.at_end():
            raise StructuredFieldError("unterminated byte sequence")
        ch = data.take()
        if ch == ":":
            break
        if ch not in _BASE64_CHARS:
            raise StructuredFieldError("invalid byte sequence character")
        out.append(ch)
    text = "".join(out)
    # Base64 syntax check equivalent to decoding with synthesized padding:
    # '=' only at the end (at most two), and a data length that is not 1 mod 4.
    core = text.rstrip("=")
    if (_BASE64_SHAPE.fullmatch(text) is None or _BASE64_CORE.fullmatch(core) is None
            or len(core) % 4 == 1 or (text != core and len(text) % 4 != 0)):
        raise StructuredFieldError("invalid base64")
    return ByteSequence(text)


def _boolean(data: _Input) -> bool:
    data.take()  # '?'
    ch = data.take()
    if ch == "1":
        return True
    if ch == "0":
        return False
    raise StructuredFieldError("invalid boolean")


def _date(data: _Input) -> Date:
    data.take()  # '@'
    value = _integer_or_decimal(data)
    if not isinstance(value, int):
        raise StructuredFieldError("date must be an integer")
    return Date(value)


def _display_string(data: _Input) -> DisplayString:
    data.take()  # '%'
    if data.take() != '"':
        raise StructuredFieldError("display string must start with %\"")
    raw = bytearray()
    while not data.at_end():
        ch = data.take()
        if ord(ch) < 0x20 or ord(ch) > 0x7E:
            raise StructuredFieldError("invalid character in display string")
        if ch == "%":
            hex_digits = data.take() + data.take()
            if len(hex_digits) != 2 or any(c not in _LOWER_HEX for c in hex_digits):
                raise StructuredFieldError("invalid percent-encoding in display string")
            raw.append(int(hex_digits, 16))
        elif ch == '"':
            try:
                return DisplayString(raw.decode("utf-8"))
            except UnicodeDecodeError:
                raise StructuredFieldError("display string is not valid UTF-8") from None
        else:
            raw.append(ord(ch))
    raise StructuredFieldError("unterminated display string")
