"""Numeric grounding of structured response blocks against evidence text."""

from __future__ import annotations

import re

from shared.schemas.response_blocks import (
    ArtifactBlock,
    Block,
    CalloutBlock,
    ChartBlock,
    MetricItem,
    MetricsBlock,
    ResponseDocument,
    ResponseSection,
    TableBlock,
    Verification,
)

_THOUSANDS_SEPARATOR_RE = re.compile(r"(?<=\d),(?=\d{3})")
_WHITESPACE_RE = re.compile(r"\s+")
_UNICODE_MINUS = "−"
_NUMERIC_STRIP_CHARS = "%$,"


def build_corpus(*texts: str) -> str:
    """Normalize evidence texts into one lowercase, comma-free, whitespace-collapsed corpus."""
    joined = " ".join(text for text in texts if text)
    normalized = joined.lower().replace(_UNICODE_MINUS, "-")
    normalized = _THOUSANDS_SEPARATOR_RE.sub("", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _number_candidates(value: float) -> list[str]:
    """Plausible textual forms of ``value``: general, integral, 1dp, 2dp, and negative parens."""
    candidates: list[str] = []

    def add(text: str) -> None:
        if text and text not in candidates:
            candidates.append(text)

    add(f"{value:g}")
    if value == int(value):
        add(str(int(value)))
    for decimals in (1, 2):
        add(f"{value:.{decimals}f}".rstrip("0").rstrip("."))
    if value < 0:
        # ADR: also accept accounting-style negatives | tables sometimes use "(5.2)" instead of "-5.2"
        for text in list(candidates):
            add(f"({text.lstrip('-')})")
    return candidates


def is_grounded(value: float, corpus: str) -> bool:
    """Return True when ``value`` appears in ``corpus`` at a number boundary."""
    for candidate in _number_candidates(float(value)):
        pattern = rf"(?<![\d.]){re.escape(candidate)}(?!\d)"
        if re.search(pattern, corpus):
            return True
    return False


def _cell_numeric_value(cell: object) -> float | None:
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    if isinstance(cell, str):
        cleaned = cell.translate(str.maketrans("", "", _NUMERIC_STRIP_CHARS)).strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _flag_numeric_rows(rows: list[list[object]], corpus: str) -> list[tuple[int, int]]:
    flagged: list[tuple[int, int]] = []
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            numeric = _cell_numeric_value(cell)
            if numeric is not None and not is_grounded(numeric, corpus):
                flagged.append((row_index, column_index))
    return flagged


def _ground_metric(metric: MetricItem, corpus: str, notes: list[str]) -> Verification:
    if is_grounded(metric.value, corpus):
        return "verified"
    if metric.basis == "estimate":
        return "estimate"
    notes.append(f"metric '{metric.label}' marked unverified (value not found in evidence)")
    return "unverified"


def _ground_chart(block: ChartBlock, corpus: str, heading: str, notes: list[str]) -> ChartBlock | None:
    if block.basis == "estimate":
        block.verification = "estimate"
        return block
    ungrounded = any(
        not is_grounded(value, corpus) for series in block.series for value in series.values
    )
    if ungrounded:
        notes.append(f"dropped chart '{block.title}' from section '{heading}' (values not grounded)")
        return None
    block.verification = "verified"
    return block


def _ground_table(block: TableBlock, corpus: str, notes: list[str]) -> TableBlock:
    if block.basis == "estimate":
        block.verification = "estimate"
        return block
    flagged = _flag_numeric_rows(block.rows, corpus)
    block.unverified_cells = flagged
    if flagged:
        block.verification = "unverified"
        notes.append(f"flagged {len(flagged)} unverified cell(s) in table '{block.title or 'table'}'")
    else:
        block.verification = "verified"
    return block


def _ground_artifact(block: ArtifactBlock, corpus: str, notes: list[str]) -> ArtifactBlock:
    if block.basis == "estimate":
        block.verification = "estimate"
        return block
    if block.data is None:
        block.verification = "verified"
        return block
    flagged = _flag_numeric_rows(block.data.rows, corpus)
    block.unverified_cells = flagged
    if flagged:
        block.verification = "unverified"
        notes.append(f"flagged {len(flagged)} unverified data cell(s) in artifact '{block.title}'")
    else:
        block.verification = "verified"
    return block


def _ground_block(block: Block, corpus: str, heading: str, notes: list[str]) -> Block | None:
    if isinstance(block, ChartBlock):
        return _ground_chart(block, corpus, heading, notes)
    if isinstance(block, MetricsBlock):
        for metric in block.metrics:
            metric.verification = _ground_metric(metric, corpus, notes)
        return block
    if isinstance(block, TableBlock):
        return _ground_table(block, corpus, notes)
    if isinstance(block, ArtifactBlock):
        return _ground_artifact(block, corpus, notes)
    return block


def ground_document(doc: ResponseDocument, corpus: str) -> tuple[ResponseDocument, list[str]]:
    """Annotate verification fields, drop ungrounded sourced charts, return trace notes."""
    grounded = doc.model_copy(deep=True)
    notes: list[str] = []

    kept_key_metrics: list[MetricItem] = []
    for metric in grounded.key_metrics:
        metric.verification = _ground_metric(metric, corpus, notes)
        if metric.verification == "unverified":
            notes.append(f"removed key metric '{metric.label}' from the header (not grounded)")
            continue
        kept_key_metrics.append(metric)
    grounded.key_metrics = kept_key_metrics

    kept_sections: list[ResponseSection] = []
    for section in grounded.sections:
        kept_blocks = [
            block
            for block in (
                _ground_block(block, corpus, section.heading, notes) for block in section.blocks
            )
            if block is not None
        ]
        if kept_blocks:
            section.blocks = kept_blocks
            kept_sections.append(section)
        else:
            notes.append(f"section '{section.heading}' removed (no groundable content left)")

    if kept_sections:
        grounded.sections = kept_sections
    else:
        notes.append("all sections were removed; added a data-omitted callout")
        grounded.sections = [
            ResponseSection(
                heading="Data unavailable",
                blocks=[
                    CalloutBlock(
                        kind="callout",
                        tone="warning",
                        title="Data omitted",
                        markdown=(
                            "The figures for this request could not be verified against "
                            "retrieved evidence, so they were omitted."
                        ),
                    )
                ],
            )
        ]
    return grounded, notes
