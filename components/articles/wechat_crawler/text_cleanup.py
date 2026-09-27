from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup


_HEX_ESCAPE_RE = re.compile(r"\\+x([0-9a-fA-F]{2})")
_EMBEDDED_HTML_RE = re.compile(
    r"<(?:a|br|div|li|ol|p|section|span|ul)\b",
    flags=re.IGNORECASE,
)


def clean_wechat_embedded_text(value: str) -> str:
    """Decode the escaped rich text used by some public short-content pages."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    had_hex_escapes = bool(_HEX_ESCAPE_RE.search(text))
    if had_hex_escapes:
        text = _HEX_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), text)

    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded

    if had_hex_escapes and _EMBEDDED_HTML_RE.search(text):
        soup = BeautifulSoup(text, "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for block in soup.find_all(["p", "section", "div", "li"]):
            block.insert_after("\n\n")
        text = soup.get_text("")

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
