"""Render the metric table to deterministic Markdown and LaTeX.

Determinism rules enforced here: fixed float precision, ``—`` for missing
values, sorted arms (from :func:`build_table`), and NO timestamps — run metadata
comes from the injected :class:`ReportMeta`. Two runs over the same table yield
byte-identical strings.
"""

from __future__ import annotations

import math

from fallow_bench.analysis.models import MetricTable, ReportMeta

_MISSING_MD = "—"
_MISSING_TEX = "--"


def fmt_value(value: float | None, precision: int, missing: str) -> str:
    """Fixed-precision float, or the missing marker for ``None``/non-finite."""
    if value is None or math.isnan(value):
        return missing
    return f"{value:.{precision}f}"


def _tex_escape(text: str) -> str:
    return text.replace("%", r"\%").replace("&", r"\&").replace("_", r"\_")


def render_markdown(table: MetricTable, meta: ReportMeta, precision: int) -> str:
    lines = [f"# {meta.title}", ""]
    if meta.git_sha is not None:
        lines.append(f"Commit: `{meta.git_sha}`")
    if meta.notes is not None:
        lines.append(f"Notes: {meta.notes}")
    if meta.git_sha is not None or meta.notes is not None:
        lines.append("")
    header = ["Metric", *table.arms]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    for row in table.rows:
        cells = [fmt_value(v, precision, _MISSING_MD) for v in row.values]
        lines.append("| " + " | ".join([row.label, *cells]) + " |")
    return "\n".join(lines) + "\n"


def render_latex(table: MetricTable, meta: ReportMeta, precision: int) -> str:
    col_spec = "l" + "r" * len(table.arms)
    lines = [
        "\\begin{table}[t]",
        "\\centering",
        f"\\caption{{{_tex_escape(meta.title)}}}",
        f"\\begin{{tabular}}{{{col_spec}}}",
        "\\hline",
        " & ".join(["Metric", *[_tex_escape(a) for a in table.arms]]) + " \\\\",
        "\\hline",
    ]
    for row in table.rows:
        cells = [fmt_value(v, precision, _MISSING_TEX) for v in row.values]
        lines.append(" & ".join([_tex_escape(row.label), *cells]) + " \\\\")
    lines += ["\\hline", "\\end{tabular}", "\\end{table}", ""]
    return "\n".join(lines)
