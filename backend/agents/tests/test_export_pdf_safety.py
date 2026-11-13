"""PDFs must treat report text as text, including titles and figure captions."""
from __future__ import annotations

import base64
import io
import json

import pytest

pytest.importorskip("reportlab")
Image = pytest.importorskip("PIL.Image")

from shared.tools.export_pdf import pdf_code


def test_pdf_title_and_caption_do_not_load_external_resources(monkeypatch, capsys):
    from reportlab.lib import utils

    original = utils.open_for_read
    attempted = []

    def guard(name, *args, **kwargs):
        if isinstance(name, str) and (name.startswith("https:") or name.startswith("file:")):
            attempted.append(name)
            raise AssertionError("report text attempted to load an external resource")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(utils, "open_for_read", guard)
    image = io.BytesIO()
    Image.new("RGB", (2, 2)).save(image, format="PNG")
    caption = '<img src="https://attacker.invalid/caption"/>'
    title = '<img src="file:///tmp/private"/>'
    sections = [{"heading": "Results", "content": "Body", "charts": [caption]}]
    charts = {caption: base64.b64encode(image.getvalue()).decode("ascii")}
    exec(pdf_code(title, json.dumps(sections), json.dumps(charts)), {})
    assert capsys.readouterr().out.startswith("PDF_B64:")
    assert attempted == []
