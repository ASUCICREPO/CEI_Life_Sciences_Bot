from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from shared.tools.chart import chart_filename, place_charts


class LatexTemplate(StrEnum):
    ARTICLE = "article"
    REPORT  = "report"
    BEAMER  = "beamer"
    GRANT   = "grant"
    JOURNAL = "journal"


# ── Document type → LaTeX template mapping ────────────────────────────────────
_DOC_TYPE_TEMPLATE: dict[str, LatexTemplate] = {
    "grant":      LatexTemplate.GRANT,
    "journal":    LatexTemplate.JOURNAL,
    "whitepaper": LatexTemplate.REPORT,
    "vc_pitch":   LatexTemplate.BEAMER,
    "latex":      LatexTemplate.ARTICLE,
}

# ── Pre-built Overleaf component library ─────────────────────────────────────
# Each entry is a LaTeX macro/environment definition included in the preamble.
_COMPONENT_LIBRARY = r"""
% ── CEI Life Sciences LaTeX Component Library ────────────────────────────────
\usepackage{xcolor}
\usepackage{tcolorbox}
\usepackage{booktabs}
\usepackage{graphicx}
\usepackage{amsmath,amssymb}
\usepackage{hyperref}
\usepackage{caption}
\usepackage{enumitem}
\usepackage{geometry}
\usepackage{setspace}
\usepackage{fancyhdr}
\usepackage{natbib}
\usepackage{listings}

% Colour palette
\definecolor{ceiNavy}{HTML}{1a3a5c}
\definecolor{ceiBlue}{HTML}{2563a8}
\definecolor{ceiTeal}{HTML}{0e7490}
\definecolor{ceiSlate}{HTML}{475569}
\definecolor{ceiLight}{HTML}{f0f4f8}

% Highlighted box (key findings, important notes)
\tcbuselibrary{skins,breakable}
\newtcolorbox{keybox}[1][Key Finding]{
  enhanced, breakable,
  colback=ceiLight, colframe=ceiTeal,
  fonttitle=\bfseries\color{ceiNavy},
  title=#1, left=6pt, right=6pt, top=4pt, bottom=4pt
}

% Warning/caution box
\newtcolorbox{cautionbox}[1][Caution]{
  enhanced, breakable,
  colback=yellow!8, colframe=orange!70!black,
  fonttitle=\bfseries, title=#1
}

% Citation-grounded claim block
\newenvironment{evidenceblock}{%
  \begin{tcolorbox}[colback=blue!4, colframe=ceiBlue, breakable,
    title=Evidence Summary, fonttitle=\bfseries\color{white},
    colbacktitle=ceiBlue]
}{%
  \end{tcolorbox}
}

% Structured abstract (science mode)
\newenvironment{structuredabstract}{%
  \section*{Abstract}
  \begin{description}[leftmargin=2cm, labelwidth=1.8cm, font=\bfseries]
}{%
  \end{description}
}

% Custom code/data block
\lstset{
  basicstyle=\ttfamily\small,
  backgroundcolor=\color{ceiLight},
  frame=single, framesep=4pt,
  breaklines=true, breakatwhitespace=true,
  captionpos=b
}

% Figure helper
\newcommand{\ceiimage}[3]{%  {filename}{width fraction}{caption}
  \begin{figure}[htbp]
    \centering
    \includegraphics[width=#2\textwidth]{#1}
    \caption{#3}
    \label{fig:#1}
  \end{figure}
}
"""

# ── Template preambles ────────────────────────────────────────────────────────
_PREAMBLES: dict[LatexTemplate, str] = {
    LatexTemplate.ARTICLE: r"""\documentclass[11pt,a4paper]{article}
\geometry{margin=1in}
""" + _COMPONENT_LIBRARY,

    LatexTemplate.REPORT: r"""\documentclass[12pt,a4paper]{report}
\geometry{margin=1in}
\setstretch{1.15}
""" + _COMPONENT_LIBRARY,

    LatexTemplate.JOURNAL: r"""\documentclass[12pt]{article}
\geometry{left=1.25in,right=1.25in,top=1in,bottom=1in}
\setstretch{2}
% Journal submission: double-spaced, line numbers
\usepackage{lineno}
\linenumbers
""" + _COMPONENT_LIBRARY,

    LatexTemplate.GRANT: r"""\documentclass[11pt,letterpaper]{article}
\geometry{margin=0.5in}
\setstretch{1.0}
% NIH/NSF grant: tight margins, 11pt, Arial-like
\usepackage{helvet}
\renewcommand{\familydefault}{\sfdefault}
""" + _COMPONENT_LIBRARY,

    LatexTemplate.BEAMER: r"""\documentclass[aspectratio=169,10pt]{beamer}
\usetheme{Madrid}
\usecolortheme[named=ceiNavy]{structure}
\setbeamercolor{frametitle}{bg=ceiNavy,fg=white}
\setbeamercolor{title}{fg=ceiNavy}
\usepackage{xcolor}
\definecolor{ceiNavy}{HTML}{1a3a5c}
\definecolor{ceiBlue}{HTML}{2563a8}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{amsmath}
""",
}

# ── Markdown → LaTeX inline conversion ───────────────────────────────────────

def _md_inline(text: str) -> str:
    """Convert inline markdown to LaTeX commands."""
    # Apply markdown patterns BEFORE escaping so ~~ and __ are not clobbered
    # Bold-italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: r"\textbf{\textit{" + m.group(1) + "}}", text)
    text = re.sub(r"___(.+?)___",        lambda m: r"\textbf{\textit{" + m.group(1) + "}}", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: r"\textbf{" + m.group(1) + "}", text)
    text = re.sub(r"__(.+?)__",      lambda m: r"\textbf{" + m.group(1) + "}", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", lambda m: r"\textit{" + m.group(1) + "}", text)
    text = re.sub(r"_(.+?)_",    lambda m: r"\textit{" + m.group(1) + "}", text)
    # Underline (~~text~~)
    text = re.sub(r"~~(.+?)~~", lambda m: r"\underline{" + m.group(1) + "}", text)
    # Inline code — wrap before escaping so content is preserved verbatim
    text = re.sub(r"`(.+?)`", lambda m: r"\texttt{" + m.group(1) + "}", text)
    # Now escape remaining LaTeX special chars in plain text (not inside \cmd{...})
    # Simple approach: escape chars that appear outside braces
    result = []
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            # Already a LaTeX command — pass through
            result.append(ch)
            i += 1
            continue
        if ch == "{":
            depth += 1
            result.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            result.append(ch)
        elif depth == 0:
            # Escape special chars only at depth 0 (plain text)
            result.append({
                "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                "^": r"\^{}", "~": r"{\textasciitilde}",
                "<": r"\textless{}", ">": r"\textgreater{}",
            }.get(ch, ch))
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _md_to_latex_lines(content: str, template: LatexTemplate) -> list[str]:
    """Convert markdown content lines to LaTeX lines."""
    out: list[str] = []
    in_itemize = False
    in_enumerate = False

    def close_lists() -> None:
        nonlocal in_itemize, in_enumerate
        if in_itemize:
            out.append(r"\end{itemize}")
            in_itemize = False
        if in_enumerate:
            out.append(r"\end{enumerate}")
            in_enumerate = False

    heading_cmd = {
        1: r"\section",
        2: r"\subsection",
        3: r"\subsubsection",
        4: r"\paragraph",
    }
    # Beamer uses frames instead of sections
    beamer_cmd = {
        1: r"\section",
        2: r"\frametitle",
        3: r"\framesubtitle",
    }

    HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")

    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            close_lists()
            out.append("")
            i += 1
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            close_lists()
            out.append(r"\noindent\rule{\textwidth}{0.4pt}")
            i += 1
            continue

        # Custom LaTeX block pass-through: ```latex ... ```
        if stripped.startswith("```latex"):
            close_lists()
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            out.extend(block)
            i += 1
            continue

        # Code block (non-latex)
        if stripped.startswith("```"):
            close_lists()
            lang = stripped[3:].strip() or "text"
            out.append(rf"\begin{{lstlisting}}[language={lang}]")
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                out.append(lines[i])
                i += 1
            out.append(r"\end{lstlisting}")
            i += 1
            continue

        # Markdown heading
        m = HEADING_RE.match(stripped)
        if m:
            close_lists()
            level = len(m.group(1))
            text = _md_inline(m.group(2))
            if template == LatexTemplate.BEAMER and level == 2:
                # Open a new frame
                out.append(r"\begin{frame}")
                out.append(rf"\frametitle{{{text}}}")
            else:
                cmd = heading_cmd.get(level, r"\paragraph")
                out.append(rf"{cmd}{{{text}}}")
            i += 1
            continue

        # Bullet list
        if re.match(r"^[-*+]\s+", stripped):
            if in_enumerate:
                close_lists()
            if not in_itemize:
                out.append(r"\begin{itemize}")
                in_itemize = True
            out.append(rf"\item {_md_inline(stripped[2:].strip())}")
            i += 1
            continue

        # Numbered list
        if re.match(r"^\d+[.]\s+", stripped):
            if in_itemize:
                close_lists()
            if not in_enumerate:
                out.append(r"\begin{enumerate}")
                in_enumerate = True
            out.append(rf"\item {_md_inline(re.sub(r'^\d+[.]\s+', '', stripped))}")
            i += 1
            continue

        # Markdown table → LaTeX booktabs table
        if "|" in stripped and not stripped.startswith("|---"):
            close_lists()
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and not lines[i].strip().startswith("|---"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            # Skip separator row
            if i < len(lines) and lines[i].strip().startswith("|---"):
                i += 1
            if rows:
                ncols = len(rows[0])
                col_spec = "l" + " c" * (ncols - 1)
                out.append(r"\begin{table}[htbp]")
                out.append(r"\centering")
                out.append(rf"\begin{{tabular}}{{{col_spec}}}")
                out.append(r"\toprule")
                out.append(" & ".join(_md_inline(c) for c in rows[0]) + r" \\")
                out.append(r"\midrule")
                for row in rows[1:]:
                    out.append(" & ".join(_md_inline(c) for c in row) + r" \\")
                out.append(r"\bottomrule")
                out.append(r"\end{tabular}")
                out.append(r"\end{table}")
            continue

        # Normal paragraph
        close_lists()
        out.append(_md_inline(stripped))
        i += 1

    close_lists()
    return out


# ── Main export function ──────────────────────────────────────────────────────

def latex_export(
    title: str,
    sections: list[dict[str, Any]],
    charts: dict[str, bytes],
    doc_type: str,
    output_dir: Path,
    chart_sections: dict[str, str] | None = None,
) -> tuple[str, dict[str, bytes]]:
    """
    Generate a .tex source file and a dict of image files to write alongside it.

    Returns:
        tex_source: complete LaTeX document as a string
        image_files: {filename: bytes} for chart PNGs (write next to the .tex file)
    """
    template = _DOC_TYPE_TEMPLATE.get(doc_type, LatexTemplate.ARTICLE)
    preamble = _PREAMBLES[template]

    # Prepare chart image files
    image_files: dict[str, bytes] = {}
    chart_filename_map: dict[str, str] = {}
    for chart_title, png_bytes in charts.items():
        fname = chart_filename(chart_title)
        image_files[fname] = png_bytes
        chart_filename_map[chart_title] = fname
    placed, unmatched = place_charts(sections, list(charts), chart_sections)

    # Build document body
    body_lines: list[str] = []

    if template == LatexTemplate.BEAMER:
        # Beamer: title slide + frames
        body_lines += [
            rf"\title{{{_md_inline(title)}}}",
            r"\date{\today}",
            r"\begin{document}",
            r"\maketitle",
        ]
    else:
        body_lines += [
            rf"\title{{\textbf{{\color{{ceiNavy}}{_md_inline(title)}}}}}",
            r"\date{\today}",
            r"\begin{document}",
            r"\maketitle",
        ]

    for sec in sorted(sections, key=lambda s: s.get("order", 0)):
        heading = sec.get("heading", "")
        content = sec.get("content", "")

        if heading:
            if template == LatexTemplate.BEAMER:
                body_lines.append(r"\begin{frame}")
                body_lines.append(rf"\frametitle{{{_md_inline(heading)}}}")
            else:
                body_lines.append(rf"\subsection{{{_md_inline(heading)}}}")

        body_lines.extend(_md_to_latex_lines(content, template))

        for chart_title in placed.get(sec.get("order", 0), []):
            body_lines.append(
                rf"\ceiimage{{{chart_filename_map[chart_title]}}}{{0.85}}{{{_md_inline(chart_title)}}}"
            )

        if template == LatexTemplate.BEAMER and heading:
            body_lines.append(r"\end{frame}")

    # Charts with no matching section heading go into a trailing Figures section
    if unmatched:
        if template != LatexTemplate.BEAMER:
            body_lines.append(r"\subsection{Figures}")
        for chart_title in unmatched:
            fig = rf"\ceiimage{{{chart_filename_map[chart_title]}}}{{0.85}}{{{_md_inline(chart_title)}}}"
            if template == LatexTemplate.BEAMER:
                body_lines += [r"\begin{frame}", rf"\frametitle{{{_md_inline(chart_title)}}}", fig, r"\end{frame}"]
            else:
                body_lines.append(fig)

    body_lines.append(r"\end{document}")

    tex_source = preamble + "\n" + "\n".join(body_lines)
    return tex_source, image_files
