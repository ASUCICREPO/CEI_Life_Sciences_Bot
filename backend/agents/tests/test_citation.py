"""B44: the web search connector has no author/year metadata, so Citation relaxes those two
fields for SourceType.WEB only -- every other source keeps them required."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shared.schemas.citation import Citation, SourceType


def _base(**overrides):
    fields = dict(
        source_id="https://example.com/a",
        source_type=SourceType.WEB,
        url="https://example.com/a",
        title="An article",
        relevant_passage="",
        retrieved_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return fields


def test_web_citation_needs_no_authors_or_year():
    citation = Citation(**_base())
    assert citation.authors is None
    assert citation.year is None
    assert citation.source_type is SourceType.WEB


def test_web_citation_may_carry_a_snippet():
    citation = Citation(**_base(snippet="the cited sentence"))
    assert citation.snippet == "the cited sentence"


def test_non_web_citation_still_requires_authors_and_year():
    with pytest.raises(ValidationError, match="authors and year are required"):
        Citation(**_base(source_type=SourceType.PUBMED, source_id="PMID:1"))


def test_non_web_citation_with_authors_and_year_is_unaffected():
    citation = Citation(**_base(source_type=SourceType.PUBMED, source_id="PMID:1", authors=["A. Researcher"], year=2026))
    assert citation.authors == ["A. Researcher"]
    assert citation.year == 2026
    assert citation.snippet is None


def test_attachments_citation_needs_no_authors_or_year():
    """B110: attachment chunks carry no bibliographic metadata, same relaxation as web (B44)."""
    citation = Citation(**_base(source_type=SourceType.ATTACHMENTS, source_id="attachment:f1:0", url="", title="notes.pdf"))
    assert citation.authors is None
    assert citation.year is None
    assert citation.source_type is SourceType.ATTACHMENTS
