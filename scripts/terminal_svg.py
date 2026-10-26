"""Turn a terminal session (plain text) into an SVG that looks like a terminal window, for the README.

    seodesk audit example.com > audit.txt
    python scripts/terminal_svg.py audit.txt "uvx seo-desk audit example.com" > docs/assets/audit.svg

Long lines wrap at 104 columns; a few words get colour (BLOCKED and MISSING red, ok green, the section names bold)."""
from __future__ import annotations

import re
import sys
import textwrap
from html import escape

COLS, CHAR_W, LINE_H, PAD, TOP = 104, 8.4, 20, 22, 46
COLOURS = [(re.compile(r"\b(?:BLOCKED|MISSING(?: \(\d+\))?|FAILED)\b"), "#ff7b72"),
           (re.compile(r"(?<=\s)ok\b"), "#7ee787"),
           (re.compile(r"\b(?:Overall|SEO|AEO|GEO) +\d+"), "#79c0ff")]
HEADINGS = {"AI crawlers", "Quick wins", "Issues", "Google ranks", "Notes"}


def wrap(lines):
    for line in lines:
        indent = len(line) - len(line.lstrip())
        hang = indent + (3 if re.match(r"\s*\d+\. ", line) else 2)
        yield from textwrap.wrap(line, COLS, subsequent_indent=" " * hang, break_on_hyphens=False) or [""]


def colour(line: str) -> str:
    spans, out, i = [], [], 0
    for rx, fill in COLOURS:
        spans += [(m.start(), m.end(), fill) for m in rx.finditer(line)]
    for start, end, fill in sorted(spans):
        if start < i:
            continue
        out.append(escape(line[i:start]))
        out.append(f'<tspan fill="{fill}">{escape(line[start:end])}</tspan>')
        i = end
    out.append(escape(line[i:]))
    return "".join(out)


def svg(text: str, command: str) -> str:
    lines = list(wrap(text.rstrip("\n").splitlines()))
    width = round(PAD * 2 + CHAR_W * COLS)
    height = TOP + PAD + LINE_H * (len(lines) + 1)
    rows = [f'<text x="{PAD}" y="{TOP + LINE_H}"><tspan fill="#7ee787">$</tspan> {escape(command)}</text>']
    for n, line in enumerate(lines, 2):
        weight = ' font-weight="700"' if line.strip() in HEADINGS or line.startswith("SEO Desk audit") else ""
        rows.append(f'<text x="{PAD}" y="{TOP + LINE_H * n}" xml:space="preserve"{weight}>{colour(line)}</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(command)}">
  <rect width="{width}" height="{height}" rx="12" fill="#0d1117"/>
  <rect width="{width}" height="34" rx="12" fill="#161b22"/><rect y="22" width="{width}" height="12" fill="#161b22"/>
  <circle cx="22" cy="17" r="6" fill="#ff5f57"/><circle cx="42" cy="17" r="6" fill="#febc2e"/><circle cx="62" cy="17" r="6" fill="#28c840"/>
  <g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace" font-size="14" fill="#c9d1d9">
    {chr(10).join("    " + r for r in rows).strip()}
  </g>
</svg>
'''


if __name__ == "__main__":
    sys.stdout.write(svg(open(sys.argv[1], encoding="utf-8").read(), sys.argv[2]))
