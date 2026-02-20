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
    Also checks ``/BaseEncoding`` inside encoding dictionaries.
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
            # Direct encoding name: /Encoding /WinAnsiEncoding
            m = re.search(r"/Encoding\s*/(\w+)", obj_str)
            if m:
                return m.group(1)
            # Encoding dict with BaseEncoding: /BaseEncoding /WinAnsiEncoding
            m = re.search(r"/BaseEncoding\s*/(\w+)", obj_str)
            if m:
                return m.group(1)
            return None
    return None


def _has_custom_encoding(doc, page, font_tag: str) -> bool:
    """Return True if the font has a ``/Differences`` array in its encoding.

    Fonts with ``/Differences`` remap individual character codes to different
    glyphs, making byte-level stream editing unreliable — the standard
    encoding (WinAnsi / MacRoman / latin-1) no longer accurately maps byte
    values to the correct glyphs.
    """
    fonts = page.get_fonts()
    for entry in fonts:
        if entry[4] == font_tag:
            font_xref = entry[0]
            if font_xref == 0:
                return False
            obj_str = doc.xref_object(font_xref)
            return "/Differences" in obj_str
    return False


def _is_subset_font(doc, page, font_tag: str) -> bool:
    """Return True if the font is a subset-embedded font (name has '+' prefix)."""
    fonts = page.get_fonts()
    for entry in fonts:
        if entry[4] == font_tag:
            basefont = entry[3]  # e.g. "ABCDEF+Arial"
            return "+" in basefont
    return False


def _parse_tounicode_cmap(cmap_bytes: bytes) -> tuple[dict[int, str], int]:
    """Parse a ToUnicode CMap stream into a forward map {byte_code → unicode_str}.

    Handles ``beginbfchar``/``endbfchar`` individual mappings and
    ``beginbfrange``/``endbfrange`` sequential ranges (both simple and array form).

    Returns ``(forward_map, bytes_per_code)`` where *bytes_per_code* is 1 for
    simple fonts and 2 for CID fonts (determined from codespace ranges or the
    hex length of source codes).
    """
    text = cmap_bytes.decode("latin-1", errors="replace")
    forward: dict[int, str] = {}
    src_hex_lengths: list[int] = []

    def _hex_to_unicode(h: str) -> str:
        return bytes.fromhex(h).decode("utf-16-be")

    # Individual char mappings: <src> <dst>
    for section in re.findall(r"beginbfchar\s*(.*?)\s*endbfchar", text, re.DOTALL):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section):
            src_hex = m.group(1)
            src_hex_lengths.append(len(src_hex))
            code = int(src_hex, 16)
            try:
                forward[code] = _hex_to_unicode(m.group(2))
            except Exception:
                continue

    # Range mappings: <start> <end> <unicode_start>  OR  <start> <end> [<u1> <u2> ...]
    _ARRAY_RANGE_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[([^\]]+)\]")
    _SIMPLE_RANGE_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
    for section in re.findall(r"beginbfrange\s*(.*?)\s*endbfrange", text, re.DOTALL):
        # Array form first: <start> <end> [<u1> <u2> ...]
        # Strip matched array ranges so the simple regex can't match inside them.
        remaining = section
        for m in _ARRAY_RANGE_RE.finditer(section):
            src_hex_lengths.append(len(m.group(1)))
            start = int(m.group(1), 16)
            end = int(m.group(2), 16)
            vals = re.findall(r"<([0-9A-Fa-f]+)>", m.group(3))
            for offset, val_hex in enumerate(vals):
                code = start + offset
                if code > end:
                    break
                try:
                    forward[code] = _hex_to_unicode(val_hex)
                except Exception:
                    continue
        remaining = _ARRAY_RANGE_RE.sub("", remaining)

        # Simple form on remaining text: <start> <end> <unicode_start>
        for m in _SIMPLE_RANGE_RE.finditer(remaining):
            src_hex_lengths.append(len(m.group(1)))
            start = int(m.group(1), 16)
            end = int(m.group(2), 16)
            try:
                uni_start_bytes = bytes.fromhex(m.group(3))
                # Decode the starting unicode codepoint
                uni_start_str = uni_start_bytes.decode("utf-16-be")
                if len(uni_start_str) != 1:
                    continue
                uni_start = ord(uni_start_str)
            except Exception:
                continue
            for code in range(start, end + 1):
                forward[code] = chr(uni_start + (code - start))

    # Determine bytes_per_code from source hex lengths (2 hex chars = 1 byte)
    if src_hex_lengths:
        max_hex_len = max(src_hex_lengths)
        bytes_per_code = max(1, (max_hex_len + 1) // 2)
    else:
        bytes_per_code = 1

    return forward, bytes_per_code


def _build_reverse_cmap(forward_map: dict[int, str]) -> dict[str, int]:
    """Invert a forward CMap: single-char unicode → byte code."""
    reverse: dict[str, int] = {}
    for code, uni in forward_map.items():
        if len(uni) == 1 and uni not in reverse:
            reverse[uni] = code
    return reverse


def _get_tounicode_maps(
    doc, page, font_tag: str
) -> tuple[dict[int, str] | None, dict[str, int] | None, int]:
    """Load and parse the ToUnicode CMap for the given font tag.

    Returns ``(forward_map, reverse_map, bytes_per_code)`` or
    ``(None, None, 1)`` on failure.
    """
    fonts = page.get_fonts()
    for entry in fonts:
        if entry[4] != font_tag:
            continue
        font_xref = entry[0]
        if font_xref == 0:
            return None, None, 1
        try:
            obj_str = doc.xref_object(font_xref)
        except Exception:
            return None, None, 1

        # Find /ToUnicode reference  (e.g. "/ToUnicode 42 0 R")
        m = re.search(r"/ToUnicode\s+(\d+)\s+\d+\s+R", obj_str)
        if not m:
            return None, None, 1
        tounicode_xref = int(m.group(1))
        try:
            cmap_bytes = doc.xref_stream(tounicode_xref)
        except Exception:
            return None, None, 1
        if not cmap_bytes:
            return None, None, 1

        forward, bytes_per_code = _parse_tounicode_cmap(cmap_bytes)
        if not forward:
            return None, None, 1
        reverse = _build_reverse_cmap(forward)
        if not reverse:
            return None, None, 1
        return forward, reverse, bytes_per_code
    return None, None, 1


def _decode_with_cmap(token: bytes, forward_map: dict[int, str], bytes_per_code: int = 1) -> str:
    """Decode a PDF string token using a CMap forward map.

    *bytes_per_code* is 1 for simple fonts and 2 for CID fonts.
    """
    if token.startswith(b"(") and token.endswith(b")"):
        raw = token[1:-1]
        # Process escape sequences to get raw bytes
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
                elif nxt in (0x0D, 0x0A):
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

    # Map character codes through the CMap
    chars = []
    i = 0
    while i + bytes_per_code <= len(raw_bytes):
        if bytes_per_code == 1:
            code = raw_bytes[i]
        else:
            code = int.from_bytes(raw_bytes[i : i + bytes_per_code], "big")
        uni = forward_map.get(code)
        if uni is not None:
            chars.append(uni)
        elif bytes_per_code == 1:
            chars.append(chr(code))
        i += bytes_per_code
    return "".join(chars)


_CHAR_EQUIVALENTS: dict[str, list[str]] = {
    " ": ["\xa0"],  # SPACE ↔ NO-BREAK SPACE
    "\xa0": [" "],
    "\u2018": ["'"],  # LEFT SINGLE QUOTE → APOSTROPHE
    "\u2019": ["'"],  # RIGHT SINGLE QUOTE → APOSTROPHE
    "'": ["\u2019", "\u2018"],
    "\u201c": ['"'],  # LEFT DOUBLE QUOTE → QUOTATION MARK
    "\u201d": ['"'],  # RIGHT DOUBLE QUOTE → QUOTATION MARK
    '"': ["\u201d", "\u201c"],
    "\u2013": ["-"],  # EN DASH → HYPHEN-MINUS
    "\u2014": ["-"],  # EM DASH → HYPHEN-MINUS
    "-": ["\u2013", "\u2014"],
}


def _encode_with_cmap(text: str, reverse_map: dict[str, int], bytes_per_code: int = 1) -> bytes:
    """Encode text using a CMap reverse map into a PDF string token.

    For single-byte fonts produces a literal string ``(...)``.
    For multi-byte (CID) fonts produces a hex string ``<...>``.

    When a character is not directly in the reverse map, common visual
    equivalents are tried (e.g. regular space ↔ non-breaking space).

    Raises ``ValueError`` if any character cannot be mapped.
    """
    raw = bytearray()
    for ch in text:
        code = reverse_map.get(ch)
        if code is None:
            # Try common visual equivalents
            for alt in _CHAR_EQUIVALENTS.get(ch, ()):
                code = reverse_map.get(alt)
                if code is not None:
                    break
        if code is None:
            raise ValueError(f"Character {ch!r} not in CMap reverse map")
        raw.extend(code.to_bytes(bytes_per_code, "big"))

    if bytes_per_code > 1:
        # CID fonts: produce hex string <AABBCCDD...>
        return b"<" + raw.hex().upper().encode("ascii") + b">"

    # Simple fonts: produce escaped literal string (...)
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
    Skips subset fonts, CID font sections, and fonts with ``/Differences``
    encoding arrays (rather than bailing out entirely) so safe text later in
    the stream can still be matched.

    Returns ``True`` if the replacement was made.
    """
    target_stripped = target_text.strip()
    n = len(tokens)

    # CMap cache: font_tag → (forward_map, reverse_map, bytes_per_code) or None
    cmap_cache: dict[str, tuple[dict, dict, int] | None] = {}

    def _resolve_font(ft: str):
        """Determine font handling mode for font tag *ft*.

        Returns ``(skip_font, use_cmap, encoding, cmap_fwd, cmap_rev, bpc)``.
        *bpc* (bytes_per_code) is 1 for simple fonts, 2 for CID fonts.
        """
        is_cid = _is_cid_font(doc, page, ft)
        is_subset = _is_subset_font(doc, page, ft)
        has_diff = _has_custom_encoding(doc, page, ft)

        if is_cid or is_subset or has_diff:
            # Try CMap-based handling for any "unsafe" font
            if ft not in cmap_cache:
                fwd, rev, bpc = _get_tounicode_maps(doc, page, ft)
                cmap_cache[ft] = (fwd, rev, bpc) if fwd and rev else None
            cached = cmap_cache[ft]
            if cached is not None:
                return False, True, None, cached[0], cached[1], cached[2]
            # No CMap available — skip this font
            return True, False, None, None, None, 1

        enc = _get_font_encoding(doc, page, ft)
        return False, False, enc, None, None, 1

    # --- Pass 1: single Tj/TJ operand match ---
    current_encoding: str | None = None
    skip_font = False
    use_cmap = False
    cmap_forward: dict[int, str] | None = None
    cmap_reverse: dict[str, int] | None = None
    cmap_bpc = 1

    for i in range(n):
        tok = tokens[i]

        # Track font changes: /FontTag size Tf
        if tok == b"Tf" and i >= 2:
            font_tok = tokens[i - 2]
            if font_tok.startswith(b"/"):
                ft = font_tok[1:].decode("ascii", errors="replace")
                (skip_font, use_cmap, current_encoding, cmap_forward, cmap_reverse, cmap_bpc) = (
                    _resolve_font(ft)
                )

        if skip_font:
            continue

        # Simple string: (text) Tj
        if tok == b"Tj" and i >= 1:
            str_tok = tokens[i - 1]
            if str_tok.startswith((b"(", b"<")):
                if use_cmap:
                    decoded = _decode_with_cmap(str_tok, cmap_forward, cmap_bpc)
                else:
                    decoded = _decode_pdf_string(str_tok, current_encoding)
                if decoded.strip() == target_stripped:
                    try:
                        if use_cmap:
                            new_tok = _encode_with_cmap(new_text, cmap_reverse, cmap_bpc)
                        else:
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
                    if use_cmap:
                        full_text += _decode_with_cmap(part_bytes, cmap_forward, cmap_bpc)
                    else:
                        full_text += _decode_pdf_string(part_bytes, current_encoding)
                if full_text.strip() == target_stripped:
                    try:
                        if use_cmap:
                            new_tok = _encode_with_cmap(new_text, cmap_reverse, cmap_bpc)
                        else:
                            new_tok = _encode_pdf_string(new_text, current_encoding)
                    except ValueError:
                        return False
                    tokens[i - 1] = b"[" + new_tok + b"]"
                    return True

    # --- Pass 2: BT/ET block-level match ---
    current_encoding = None
    skip_font = False
    use_cmap = False
    cmap_forward = None
    cmap_reverse = None
    cmap_bpc = 1
    in_bt = False
    block_text = ""
    block_encoding: str | None = None
    block_use_cmap = False
    block_cmap_forward: dict[int, str] | None = None
    block_cmap_reverse: dict[str, int] | None = None
    block_cmap_bpc = 1
    block_has_unsafe_font = False
    block_font_changes = 0
    block_ops: list[tuple[str, int]] = []

    for i in range(n):
        tok = tokens[i]

        if tok == b"BT":
            in_bt = True
            block_text = ""
            block_encoding = current_encoding
            block_use_cmap = use_cmap
            block_cmap_forward = cmap_forward
            block_cmap_reverse = cmap_reverse
            block_cmap_bpc = cmap_bpc
            block_has_unsafe_font = skip_font
            block_font_changes = 0
            block_ops = []
            continue

        if not in_bt:
            if tok == b"Tf" and i >= 2:
                font_tok = tokens[i - 2]
                if font_tok.startswith(b"/"):
                    ft = font_tok[1:].decode("ascii", errors="replace")
                    (
                        skip_font,
                        use_cmap,
                        current_encoding,
                        cmap_forward,
                        cmap_reverse,
                        cmap_bpc,
                    ) = _resolve_font(ft)
            continue

        # --- inside BT/ET ---
        if tok == b"Tf" and i >= 2:
            font_tok = tokens[i - 2]
            if font_tok.startswith(b"/"):
                ft = font_tok[1:].decode("ascii", errors="replace")
                block_font_changes += 1
                sf, uc, enc, cf, cr, bpc = _resolve_font(ft)
                if sf:
                    block_has_unsafe_font = True
                else:
                    block_encoding = enc
                    block_use_cmap = uc
                    block_cmap_forward = cf
                    block_cmap_reverse = cr
                    block_cmap_bpc = bpc

        if not block_has_unsafe_font:
            if tok == b"Tj" and i >= 1:
                str_tok = tokens[i - 1]
                if str_tok.startswith((b"(", b"<")):
                    if block_use_cmap:
                        block_text += _decode_with_cmap(str_tok, block_cmap_forward, block_cmap_bpc)
                    else:
                        block_text += _decode_pdf_string(str_tok, block_encoding)
                    block_ops.append(("Tj", i - 1))

            elif tok == b"TJ" and i >= 1:
                arr_tok = tokens[i - 1]
                if arr_tok.startswith(b"[") and arr_tok.endswith(b"]"):
                    for p in _extract_tj_strings(arr_tok):
                        if block_use_cmap:
                            block_text += _decode_with_cmap(p, block_cmap_forward, block_cmap_bpc)
                        else:
                            block_text += _decode_pdf_string(p, block_encoding)
                    block_ops.append(("TJ", i - 1))

        if tok == b"ET":
            in_bt = False
            if block_has_unsafe_font or block_font_changes > 1:
                continue
            if not block_ops or not block_text.strip():
                continue
            if block_text.strip() == target_stripped:
                try:
                    if block_use_cmap:
                        new_tok = _encode_with_cmap(new_text, block_cmap_reverse, block_cmap_bpc)
                    else:
                        new_tok = _encode_pdf_string(new_text, block_encoding)
                except ValueError:
                    return False
                op_type, op_idx = block_ops[0]
                if op_type == "Tj":
                    tokens[op_idx] = new_tok
                else:
                    tokens[op_idx] = b"[" + new_tok + b"]"
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
