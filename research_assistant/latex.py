"""LaTeX/APA export: render a finished task as a research-paper .tex (natbib +
apalike author-year citations) or compile it to PDF via tectonic.

The .tex is SELF-CONTAINED: the bibliography rides inside a `filecontents*`
block (written to \\jobname.bib at compile time), so one file drops into
Overleaf or `tectonic paper.tex` with zero companions.

APA metadata comes from the source dicts (authors/year — arXiv fills them, web
search can't). Sources without an author fall back to APA's no-author rule:
the title takes the author slot and the year becomes "n.d.".

reporting.render() dispatches the "tex" and "paper" formats here (lazy import,
so reporting stays importable without this module's regexes compiled).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

# order matters: backslash first would double-escape the replacements, so we
# translate char-by-char instead of chained str.replace.
_ESCAPES = {
    "\\": r"\textbackslash{}",
    "%": r"\%",
    "&": r"\&",
    "_": r"\_",
    "#": r"\#",
    "$": r"\$",
    "{": r"\{",
    "}": r"\}",
    "^": r"\^{}",
    "~": r"\~{}",
}

_CITE = re.compile(r"\[(\d+)\]")


def tex_escape(text: str) -> str:
    """Escape LaTeX special characters in plain prose."""
    return "".join(_ESCAPES.get(c, c) for c in text)


def citep(text: str) -> str:
    """Rewrite the pipeline's numeric [n] citations to natbib \\citep{srcN}."""
    return _CITE.sub(lambda m: rf"\citep{{src{m.group(1)}}}", text)


def _tex_prose(text: str) -> str:
    return citep(tex_escape(text))


def bib_entry(index: int, source: dict) -> str:
    """One BibTeX entry keyed src<index> (matching the [n] numbering).

    Academic source with authors -> @article. Anything else -> @misc with the
    APA no-author fallback: double-braced title in the author slot, year n.d.
    URLs stay unescaped — they live inside \\url{} which takes them raw."""
    title = tex_escape(source.get("title", "") or "Untitled")
    url = source.get("url", "")
    authors = source.get("authors") or []
    year = source.get("year")

    if authors and source.get("source_type") == "academic":
        fields = [
            f"author = {{{' and '.join(tex_escape(a) for a in authors)}}}",
            f"title = {{{{{title}}}}}",
            f"year = {{{year if year else 'n.d.'}}}",
            f"note = {{\\url{{{url}}}}}",
        ]
        kind = "article"
    else:
        # APA no-author fallback: the title takes the author slot. apalike prints
        # that slot in every in-text citation, so shorten long titles to their
        # first words (per APA) and keep the full title in the title field for
        # the References entry. Short titles skip the title field entirely, or
        # apalike would print the same text twice ("Title (n.d.). Title. url").
        words = title.split()
        if len(words) > 6:
            fields = [
                f"author = {{{{{' '.join(words[:6])} ...}}}}",
                f"title = {{{{{title}}}}}",
            ]
        else:
            fields = [f"author = {{{{{title}}}}}"]
        fields += [
            f"year = {{{year if year else 'n.d.'}}}",
            f"howpublished = {{\\url{{{url}}}}}",
        ]
        kind = "misc"
    body = ",\n  ".join(fields)
    return f"@{kind}{{src{index},\n  {body}\n}}"


def _body(blocks: list[tuple[str, str]]) -> str:
    """Blocks (from reporting._blocks) -> LaTeX body. h1/h2 -> \\section,
    h3 -> \\subsection, consecutive li -> one itemize, p -> paragraph."""
    out: list[str] = []
    items: list[str] = []

    def flush_items() -> None:
        if items:
            inner = "\n".join(rf"\item {i}" for i in items)
            out.append(f"\\begin{{itemize}}\n{inner}\n\\end{{itemize}}")
            items.clear()

    for kind, text in blocks:
        prose = _tex_prose(text)
        if kind == "li":
            items.append(prose)
            continue
        flush_items()
        if kind in ("h1", "h2"):
            out.append(rf"\section{{{prose}}}")
        elif kind == "h3":
            out.append(rf"\subsection{{{prose}}}")
        else:  # p
            out.append(prose)
    flush_items()
    return "\n\n".join(out)


def to_tex(task: dict) -> str:
    """The full self-contained LaTeX document for a finished task."""
    from research_assistant.reporting import _blocks

    blocks = _blocks(task.get("final_report", ""))
    # leading paragraphs before the first heading = the executive summary the
    # Synthesizer opens with -> the paper's abstract.
    split = next((i for i, (k, _) in enumerate(blocks) if k.startswith("h")), len(blocks))
    abstract = [t for k, t in blocks[:split] if k == "p"]
    sources = task.get("sources") or []
    bib = "\n\n".join(bib_entry(i, s) for i, s in enumerate(sources, 1))

    parts = [
        f"% Researchy research report — task {str(task.get('id', ''))[:8]}",
        f"% generated {datetime.now():%Y-%m-%d %H:%M}",
        r"\documentclass{article}",
        r"\usepackage{geometry}",
        r"\geometry{letterpaper}",
        r"\usepackage{url}",
        r"\usepackage{natbib}",
        "",
        r"\begin{filecontents*}[overwrite]{\jobname.bib}",
        bib,
        r"\end{filecontents*}",
        "",
        f"\\title{{{_tex_prose(task.get('query', ''))}}}",
        r"\author{Researchy — multi-agent research assistant}",
        r"\date{\today}",
        "",
        r"\begin{document}",
        r"\maketitle",
    ]
    if abstract:
        parts += [r"\begin{abstract}", "\n\n".join(_tex_prose(p) for p in abstract),
                  r"\end{abstract}"]
    parts += [
        r"\tableofcontents",
        r"\pagebreak",
        "",
        _body(blocks[split:]),
        "",
        r"\bibliographystyle{apalike}",
        r"\bibliography{\jobname}",
        r"\end{document}",
        "",
    ]
    return "\n".join(parts)


def to_pdf(task: dict) -> bytes:
    """Compile to_tex() with tectonic (single-binary LaTeX; handles the
    bibtex/rerun dance itself). Raises RuntimeError with an install hint when
    tectonic isn't on PATH — callers surface it as a friendly message."""
    if shutil.which("tectonic") is None:
        raise RuntimeError(
            "paper export needs the 'tectonic' LaTeX engine on PATH "
            "(https://tectonic-typesetting.github.io — e.g. `winget install tectonic` "
            "or `cargo install tectonic`). Alternatively use the 'tex' format and "
            "compile it yourself (Overleaf works)."
        )
    with tempfile.TemporaryDirectory() as tmp:
        tex_path = Path(tmp) / "paper.tex"
        tex_path.write_text(to_tex(task), encoding="utf-8")
        proc = subprocess.run(
            ["tectonic", str(tex_path), "--outdir", tmp],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise RuntimeError(f"tectonic failed to compile the report:\n{tail}")
        return (Path(tmp) / "paper.pdf").read_bytes()
