from __future__ import annotations

import html2text
from bs4 import BeautifulSoup

from email_intel.models import Email

_h2t = html2text.HTML2Text()
_h2t.ignore_images = True
_h2t.ignore_links = False
_h2t.body_width = 0


def clean_body(email: Email) -> str:
    """Return the best plain-text representation of the email body.

    Prefers text/plain; falls back to stripped-and-converted HTML.
    """
    if email.body_text:
        return _normalize_whitespace(email.body_text)

    if email.body_html:
        soup = BeautifulSoup(email.body_html, "html.parser")
        for tag in soup(["script", "style", "head", "meta", "link"]):
            tag.decompose()
        text = _h2t.handle(str(soup))
        return _normalize_whitespace(text)

    return ""


def _normalize_whitespace(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    # Collapse >2 consecutive blank lines.
    cleaned: list[str] = []
    blank = 0
    for line in lines:
        if not line.strip():
            blank += 1
            if blank <= 1:
                cleaned.append("")
        else:
            blank = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def truncate_for_llm(text: str, max_chars: int = 12_000) -> str:
    """Cap body size before sending to an LLM. Keeps head + tail; drops the middle."""
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head - 40
    return f"{text[:head]}\n\n[... {len(text) - head - tail} chars truncated ...]\n\n{text[-tail:]}"
