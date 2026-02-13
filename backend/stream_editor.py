"""Direct PDF content stream editing for font-preserving text replacement."""

from __future__ import annotations

import re


def _tokenize_stream(raw: bytes) -> list[bytes]:
    """Split a PDF content stream into tokens.

    Handles ``(...)`` literal strings (with escape sequences and nested parens),
    ``<...>`` hex strings, and ``[...]`` arrays as single tokens.  Everything
    else is split on whitespace.
    """
    tokens: list[bytes] = []
    i = 0
    n = len(raw)

    while i < n:
        ch = raw[i : i + 1]

        # Skip whitespace
        if ch in (b" ", b"\t", b"\r", b"\n", b"\x00", b"\x0c"):
            i += 1
            continue

        # PDF comment — skip to end of line
        if ch == b"%":
            while i < n and raw[i : i + 1] not in (b"\r", b"\n"):
                i += 1
            continue

        # Literal string (...)
        if ch == b"(":
            start = i
            i += 1
            depth = 1
            while i < n and depth > 0:
                c = raw[i : i + 1]
                if c == b"\\":
                    i += 2  # skip escaped char
                    continue
                if c == b"(":
                    depth += 1
                elif c == b")":
                    depth -= 1
                i += 1
            tokens.append(raw[start:i])
            continue

        # Hex string <...>
        if ch == b"<" and (i + 1 >= n or raw[i + 1 : i + 2] != b"<"):
            start = i
            i += 1
            while i < n and raw[i : i + 1] != b">":
                i += 1
            i += 1  # consume closing >
            tokens.append(raw[start:i])
            continue

        # Dict delimiters << and >>
        if ch == b"<" and i + 1 < n and raw[i + 1 : i + 2] == b"<":
            tokens.append(b"<<")
            i += 2
            continue
        if ch == b">" and i + 1 < n and raw[i + 1 : i + 2] == b">":
            tokens.append(b">>")
            i += 2
            continue

        # Array [...]
        if ch == b"[":
            start = i
            i += 1
            depth = 1
            while i < n and depth > 0:
                c = raw[i : i + 1]
                if c == b"(":
                    # skip nested literal string inside array
                    i += 1
                    str_depth = 1
                    while i < n and str_depth > 0:
                        sc = raw[i : i + 1]
                        if sc == b"\\":
                            i += 2
                            continue
                        if sc == b"(":
                            str_depth += 1
                        elif sc == b")":
                            str_depth -= 1
                        i += 1
                    continue
                if c == b"<" and (i + 1 >= n or raw[i + 1 : i + 2] != b"<"):
                    # skip hex string inside array
                    i += 1
                    while i < n and raw[i : i + 1] != b">":
                        i += 1
                    i += 1
                    continue
                if c == b"[":
                    depth += 1
                elif c == b"]":
                    depth -= 1
                i += 1
            tokens.append(raw[start:i])
            continue

        # Regular token (operator or number or name)
        start = i
        while i < n and raw[i : i + 1] not in (
            b" ",
            b"\t",
            b"\r",
            b"\n",
            b"\x00",
            b"\x0c",
            b"(",
            b")",
            b"<",
            b">",
            b"[",
            b"]",
            b"/",
            b"%",
        ):
            i += 1
        # Name objects start with /
        if i == start and ch == b"/":
            i += 1
            while i < n and raw[i : i + 1] not in (
                b" ",
                b"\t",
                b"\r",
                b"\n",
                b"\x00",
                b"\x0c",
                b"(",
                b")",
                b"<",
                b">",
                b"[",
                b"]",
                b"/",
                b"%",
            ):
                i += 1
        if i > start:
            tokens.append(raw[start:i])

    return tokens


def _get_font_encoding(doc, page, font_tag: str) -> str | None:
    """Look up a font's /Encoding from the page resources.

    Returns ``"WinAnsiEncoding"``, ``"MacRomanEncoding"``, or ``None``.
    """
    # Get the page's resource dict to find the font xref
    fonts = page.get_fonts()  # list of (xref, ext, type, basefont, name, encoding)
    for entry in fonts:
        # entry[4] is the font tag used in the content stream (e.g. "F1")
        tag = entry[4]
        if tag == font_tag:
            font_xref = entry[0]
            if font_xref == 0:
                return None
            obj_str = doc.xref_object(font_xref)
            # Look for /Encoding in the font object
            m = re.search(r"/Encoding\s*/(\w+)", obj_str)
            if m:
                return m.group(1)
            return None
    return None


def _is_cid_font(doc, page, font_tag: str) -> bool:
    """Return True if the font is a CID/composite font (Type0)."""
    fonts = page.get_fonts()
    for entry in fonts:
        if entry[4] == font_tag:
            font_xref = entry[0]
            if font_xref == 0:
                return False
            obj_str = doc.xref_object(font_xref)
            if "/Type0" in obj_str or "/CIDFont" in obj_str:
                return True
            # Check subtype
            m = re.search(r"/Subtype\s*/(\w+)", obj_str)
            return bool(m and m.group(1) == "Type0")
    return False


_OCTAL_RE = re.compile(rb"\\([0-7]{1,3})")
_ESCAPE_MAP = {
    ord("n"): ord("\n"),
    ord("r"): ord("\r"),
    ord("t"): ord("\t"),
    ord("b"): ord("\b"),
    ord("f"): ord("\f"),
    ord("\\"): ord("\\"),
    ord("("): ord("("),
    ord(")"): ord(")"),
}


def _decode_pdf_string(token: bytes, encoding: str | None) -> str:
    """Decode a PDF string token ``(...)`` or ``<...>`` to a Python str."""
    if token.startswith(b"(") and token.endswith(b")"):
        raw = token[1:-1]
        # Process escape sequences
        result = bytearray()
        i = 0
        while i < len(raw):
            b = raw[i]
            if b == 0x5C:  # backslash
                i += 1
                if i >= len(raw):
                    break
                nxt = raw[i]
                if nxt in _ESCAPE_MAP:
                    result.append(_ESCAPE_MAP[nxt])
                    i += 1
                elif 0x30 <= nxt <= 0x37:  # octal
                    octal = chr(nxt)
                    for _ in range(2):
                        if i + 1 < len(raw) and 0x30 <= raw[i + 1] <= 0x37:
                            i += 1
                            octal += chr(raw[i])
                        else:
                            break
                    result.append(int(octal, 8) & 0xFF)
                    i += 1
                elif nxt in (0x0D, 0x0A):  # line continuation
                    i += 1
                    if nxt == 0x0D and i < len(raw) and raw[i] == 0x0A:
                        i += 1
                else:
                    result.append(nxt)
                    i += 1
            else:
                result.append(b)
                i += 1
        raw_bytes = bytes(result)
    elif token.startswith(b"<") and token.endswith(b">"):
        hex_str = token[1:-1].replace(b" ", b"").replace(b"\n", b"").replace(b"\r", b"")
        if len(hex_str) % 2:
            hex_str += b"0"
        raw_bytes = bytes.fromhex(hex_str.decode("ascii"))
    else:
        return ""

    # Decode bytes to str using the font encoding
    codec = "latin-1"  # WinAnsiEncoding is close to latin-1
    if encoding == "MacRomanEncoding":
        codec = "mac-roman"
    try:
        return raw_bytes.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return raw_bytes.decode("latin-1")


def _encode_pdf_string(text: str, encoding: str | None) -> bytes:
    """Encode a Python str to a PDF literal string ``(...)``.

    Raises ``ValueError`` if the text contains characters that cannot be
    encoded in the target encoding.
    """
    codec = "latin-1"
    if encoding == "MacRomanEncoding":
        codec = "mac-roman"
    try:
        raw = text.encode(codec)
    except (UnicodeEncodeError, LookupError) as exc:
        raise ValueError(f"Cannot encode text in {codec}: {exc}") from exc

    # Escape special PDF characters
    escaped = bytearray()
    for b in raw:
        if b == 0x5C:  # backslash
            escaped.extend(b"\\\\")
        elif b == 0x28:  # (
            escaped.extend(b"\\(")
        elif b == 0x29:  # )
            escaped.extend(b"\\)")
        elif b == 0x0D:  # \r
            escaped.extend(b"\\r")
        elif b == 0x0A:  # \n
            escaped.extend(b"\\n")
        else:
            escaped.append(b)
    return b"(" + bytes(escaped) + b")"


def _find_and_replace_text(
    tokens: list[bytes],
    target_text: str,
    new_text: str,
    page,
    doc,
) -> bool:
    """Walk tokens, find text matching *target_text* and replace it.

    Two-pass approach:
      1. Try matching a single ``Tj`` or ``TJ`` operand directly.
      2. If that fails, match the concatenated text of an entire ``BT``/``ET``
         block (handles text split across multiple ``Tj`` operators).

    Tracks the current font via ``Tf`` operators to decode strings correctly.
    Skips CID font sections (rather than bailing out entirely) so non-CID
    text later in the stream can still be matched.

    Returns ``True`` if the replacement was made.
    """
    target_stripped = target_text.strip()
    n = len(tokens)

    # --- Pass 1: single Tj/TJ operand match ---
    current_encoding: str | None = None
    cid_active = False

    for i in range(n):
        tok = tokens[i]

        # Track font changes: /FontTag size Tf
        if tok == b"Tf" and i >= 2:
            font_tok = tokens[i - 2]
            if font_tok.startswith(b"/"):
                ft = font_tok[1:].decode("ascii", errors="replace")
                cid_active = _is_cid_font(doc, page, ft)
                if not cid_active:
                    current_encoding = _get_font_encoding(doc, page, ft)

        if cid_active:
            continue

        # Simple string: (text) Tj
        if tok == b"Tj" and i >= 1:
            str_tok = tokens[i - 1]
            if str_tok.startswith((b"(", b"<")):
                decoded = _decode_pdf_string(str_tok, current_encoding)
                if decoded.strip() == target_stripped:
                    try:
                        new_tok = _encode_pdf_string(new_text, current_encoding)
                    except ValueError:
                        return False
                    tokens[i - 1] = new_tok
                    return True

        # Array of strings: [...] TJ
        if tok == b"TJ" and i >= 1:
            arr_tok = tokens[i - 1]
            if arr_tok.startswith(b"[") and arr_tok.endswith(b"]"):
                parts = _extract_tj_strings(arr_tok)
                if not parts:
                    continue
                full_text = ""
                for part_bytes in parts:
                    full_text += _decode_pdf_string(part_bytes, current_encoding)
                if full_text.strip() == target_stripped:
                    try:
                        new_tok = _encode_pdf_string(new_text, current_encoding)
                    except ValueError:
                        return False
                    tokens[i - 1] = b"[" + new_tok + b"]"
                    return True

    # --- Pass 2: BT/ET block-level match ---
    # Concatenate all Tj/TJ text within one BT..ET block, then compare.
    # When matched, put the new text in the first operand and empty the rest.
    current_encoding = None
    cid_active = False
    in_bt = False
    block_text = ""
    block_encoding: str | None = None
    block_has_cid = False
    block_font_changes = 0
    # (operator_type, token_index) for each string operand in the block
    block_ops: list[tuple[str, int]] = []

    for i in range(n):
        tok = tokens[i]

        if tok == b"BT":
            in_bt = True
            block_text = ""
            block_encoding = current_encoding
            block_has_cid = cid_active
            block_font_changes = 0
            block_ops = []
            continue

        if not in_bt:
            # Track font state outside BT/ET so we have the right initial
            # encoding when a new block opens.
            if tok == b"Tf" and i >= 2:
                font_tok = tokens[i - 2]
                if font_tok.startswith(b"/"):
                    ft = font_tok[1:].decode("ascii", errors="replace")
                    cid_active = _is_cid_font(doc, page, ft)
                    if not cid_active:
                        current_encoding = _get_font_encoding(doc, page, ft)
            continue

        # --- inside BT/ET ---
        if tok == b"Tf" and i >= 2:
            font_tok = tokens[i - 2]
            if font_tok.startswith(b"/"):
                ft = font_tok[1:].decode("ascii", errors="replace")
                block_font_changes += 1
                if _is_cid_font(doc, page, ft):
                    block_has_cid = True
                else:
                    block_encoding = _get_font_encoding(doc, page, ft)

        if not block_has_cid:
            if tok == b"Tj" and i >= 1:
                str_tok = tokens[i - 1]
                if str_tok.startswith((b"(", b"<")):
                    block_text += _decode_pdf_string(str_tok, block_encoding)
                    block_ops.append(("Tj", i - 1))

            elif tok == b"TJ" and i >= 1:
                arr_tok = tokens[i - 1]
                if arr_tok.startswith(b"[") and arr_tok.endswith(b"]"):
                    for p in _extract_tj_strings(arr_tok):
                        block_text += _decode_pdf_string(p, block_encoding)
                    block_ops.append(("TJ", i - 1))

        if tok == b"ET":
            in_bt = False
            # Skip blocks with CID fonts or multiple font switches
            if block_has_cid or block_font_changes > 1:
                continue
            if not block_ops or not block_text.strip():
                continue
            if block_text.strip() == target_stripped:
                try:
                    new_tok = _encode_pdf_string(new_text, block_encoding)
                except ValueError:
                    return False
                # First operand gets the new text
                op_type, op_idx = block_ops[0]
                if op_type == "Tj":
                    tokens[op_idx] = new_tok
                else:
                    tokens[op_idx] = b"[" + new_tok + b"]"
                # Remaining operands become empty strings
                for j in range(1, len(block_ops)):
                    op_type, op_idx = block_ops[j]
                    if op_type == "Tj":
                        tokens[op_idx] = b"()"
                    else:
                        tokens[op_idx] = b"[()]"
                return True

    return False


def _extract_tj_strings(arr_token: bytes) -> list[bytes]:
    """Extract string tokens from a TJ array token like ``[(He) -10 (llo)]``."""
    inner = arr_token[1:-1]
    strings: list[bytes] = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i : i + 1]
        if ch == b"(":
            start = i
            i += 1
            depth = 1
            while i < n and depth > 0:
                c = inner[i : i + 1]
                if c == b"\\":
                    i += 2
                    continue
                if c == b"(":
                    depth += 1
                elif c == b")":
                    depth -= 1
                i += 1
            strings.append(inner[start:i])
        elif ch == b"<" and (i + 1 >= n or inner[i + 1 : i + 2] != b"<"):
            start = i
            i += 1
            while i < n and inner[i : i + 1] != b">":
                i += 1
            i += 1
            strings.append(inner[start:i])
        else:
            i += 1
    return strings


def try_direct_edit(doc, page, page_num: int, target_text: str, new_text: str) -> bool:
    """Attempt to edit text directly in the page content stream.

    Returns ``True`` if the edit succeeded.  Returns ``False`` if the text
    could not be located or replaced, signalling the caller to fall back to
    the redact-and-reinsert approach.
    """
    # Multi-line text spans multiple BT/ET blocks — too complex to replace
    if "\n" in target_text:
        return False
    try:
        page.clean_contents()
        xref_list = page.get_contents()
        if not xref_list:
            return False
        xref = xref_list[0]
        raw = doc.xref_stream(xref)
        if not raw:
            return False

        tokens = _tokenize_stream(raw)
        if not _find_and_replace_text(tokens, target_text, new_text, page, doc):
            return False

        new_stream = b" ".join(tokens)
        doc.update_stream(xref, new_stream)
        return True
    except Exception:
        return False
