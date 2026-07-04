"""arXiv feed parsing carries APA metadata (authors, year) into ToolResult,
and researcher sources expose it to the report renderers."""

from __future__ import annotations

from research_assistant.agents.nodes import _as_source
from research_assistant.tools.arxiv import _parse_feed
from research_assistant.tools.base import ToolResult

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Test Paper</title>
    <link href="http://arxiv.org/abs/1234.5678"/>
    <summary>A short abstract.</summary>
    <published>2017-06-12T17:57:34Z</published>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
</feed>"""


def test_parse_feed_extracts_authors_and_year():
    (r,) = _parse_feed(ATOM)
    assert r.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert r.year == 2017


def test_parse_feed_tolerates_missing_metadata():
    atom = ATOM.replace("<published>2017-06-12T17:57:34Z</published>", "").replace(
        "<author><name>Ashish Vaswani</name></author>", ""
    ).replace("<author><name>Noam Shazeer</name></author>", "")
    (r,) = _parse_feed(atom)
    assert r.authors == []
    assert r.year is None


def test_as_source_carries_authors_and_year():
    r = ToolResult(
        title="T",
        url="u",
        snippet="s",
        source_type="academic",
        authors=["A"],
        year=2020,
    )
    src = _as_source(r)
    assert src["authors"] == ["A"]
    assert src["year"] == 2020
