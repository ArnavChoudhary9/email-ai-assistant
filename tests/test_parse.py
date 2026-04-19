from __future__ import annotations

from email_intel.pipeline.parse import clean_body, truncate_for_llm


def test_prefers_plain_text(email_factory):
    email = email_factory(body="Plain body here", body_html="<p>IGNORED</p>")
    assert clean_body(email) == "Plain body here"


def test_falls_back_to_html(email_factory):
    html = "<html><head><style>x</style></head><body><p>Hello <b>world</b></p></body></html>"
    email = email_factory(body="", body_html=html)
    out = clean_body(email)
    assert "Hello" in out
    assert "world" in out
    assert "style" not in out.lower()


def test_collapses_blank_lines(email_factory):
    email = email_factory(body="line1\n\n\n\nline2")
    assert clean_body(email) == "line1\n\nline2"


def test_truncate_keeps_head_and_tail():
    text = "A" * 5000 + "B" * 5000 + "C" * 5000
    out = truncate_for_llm(text, max_chars=1000)
    assert out.startswith("A")
    assert out.endswith("C")
    assert "truncated" in out
    assert len(out) < 1500


def test_truncate_passthrough_when_small():
    assert truncate_for_llm("short", max_chars=1000) == "short"
