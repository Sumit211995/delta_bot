"""Build docs/Delta_Bot_Guide.pdf from docs/GUIDE.md.

    pip install markdown            # one-time, only needed to rebuild the PDF
    python docs/build_pdf.py

Converts the Markdown to a styled HTML page, then prints it to PDF with headless Google Chrome (Mac path by default;
set CHROME=/path/to/chrome elsewhere).
"""
import html
import os
import re
import subprocess
import sys
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
SRC, OUT_HTML, OUT_PDF = DOCS / "GUIDE.md", DOCS / "guide.html", DOCS / "Delta_Bot_Guide.pdf"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm;
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 8.5pt Inter, sans-serif; color: #8a8f98; }
  @bottom-left { content: "Delta Bot: Complete Guide"; font: 8.5pt Inter, sans-serif; color: #8a8f98; } }
@page :first { @bottom-right { content: none; } @bottom-left { content: none; } }
:root { --ink: #16181d; --ink2: #3d424b; --muted: #6b717c; --line: #e4e6ea; --soft: #f5f6f8; --accent: #e8590c; --up: #0f9d58; --down: #d93025; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: Inter, -apple-system, "Segoe UI", sans-serif; color: var(--ink); font-size: 10pt; line-height: 1.55; margin: 0; background: #fff; }
.cover { height: 255mm; display: flex; flex-direction: column; justify-content: center; page-break-after: always; padding: 0 6mm; }
.cover .logo { width: 22mm; height: 22mm; border-radius: 6mm; background: linear-gradient(135deg, #ff8a3d, #e8590c);
  display: flex; align-items: center; justify-content: center; color: #fff; font-size: 30pt; font-weight: 800; margin-bottom: 12mm; }
.cover h1 { font-size: 34pt; line-height: 1.1; margin: 0 0 4mm; border: 0; letter-spacing: -0.5pt; }
.cover .sub { font-size: 14pt; color: var(--ink2); margin-bottom: 14mm; }
.cover .meta { font-size: 10pt; color: var(--muted); border-top: 1px solid var(--line); padding-top: 5mm; }
.cover .badge { display: inline-block; margin-top: 10mm; padding: 3mm 5mm; border-radius: 3mm; background: #e8f5ee; color: var(--up);
  font-weight: 700; font-size: 10.5pt; }
h1 { font-size: 20pt; margin: 0 0 4mm; }
h2 { font-size: 15pt; margin: 9mm 0 3mm; padding-bottom: 2mm; border-bottom: 2px solid var(--line); break-after: avoid; }
h2.chap { break-before: page; margin-top: 0; }
h3 { font-size: 12pt; margin: 6mm 0 2mm; break-after: avoid; }
h4 { font-size: 10.5pt; margin: 4mm 0 1mm; break-after: avoid; }
p { margin: 0 0 2.6mm; } ul, ol { margin: 0 0 3mm; padding-left: 6mm; } li { margin: 0.6mm 0; }
a { color: var(--accent); text-decoration: none; }
strong { color: var(--ink); }
code { font-family: "JetBrains Mono", Menlo, monospace; font-size: 8.6pt; background: var(--soft); padding: 0.3mm 1.2mm; border-radius: 1mm; }
pre { background: #0f1115; color: #e6e8eb; border-radius: 2.5mm; padding: 3.5mm 4mm; font-size: 8pt; line-height: 1.45;
  overflow: hidden; white-space: pre-wrap; break-inside: avoid; margin: 0 0 3.5mm; }
pre code { background: none; padding: 0; color: inherit; font-size: inherit; }
table { width: 100%; border-collapse: collapse; margin: 0 0 4mm; font-size: 8.8pt; break-inside: auto; }
tr { break-inside: avoid; }
th { background: var(--soft); text-align: left; font-weight: 700; color: var(--ink2); }
th, td { border: 1px solid var(--line); padding: 1.6mm 2.2mm; vertical-align: top; }
blockquote { margin: 0 0 4mm; padding: 3mm 4mm; border-left: 1.2mm solid var(--accent); background: #fff4ec; border-radius: 0 2mm 2mm 0; }
blockquote p { margin: 0; }
hr { display: none; }
li.cb { list-style: none; margin-left: -5mm; }
.check { font-family: "JetBrains Mono", monospace; color: var(--muted); margin-right: 1.5mm; }
.toc ol { columns: 2; column-gap: 10mm; padding-left: 8mm; } .toc li { break-inside: avoid; }
em { color: var(--muted); }
"""


def loose(prev: str) -> bool:
    return bool(prev.strip()) and not re.match(r"\s*(\d+\.|[-*]) |\s{2,}|\|", prev)


def loosen_lists(md: str) -> str:
    """Python-Markdown needs a blank line before a list (GitHub does not). Add one where missing, outside code."""
    out, fence = [], False
    for line in md.splitlines():
        if line.startswith("```"):
            fence = not fence
        elif not fence and re.match(r"(\d+\.|[-*]) ", line) and out and loose(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def build():
    md = SRC.read_text(encoding="utf-8")
    lines = md.splitlines()
    title = lines[0].lstrip("# ").strip()
    tagline = lines[2].strip("* ").strip()
    version = lines[4].strip()
    body_md = "\n".join(lines[5:])
    body_md = loosen_lists(body_md)
    body_md = re.sub(r"^- \[ \] ", "- <span class='check'>☐</span> ", body_md, flags=re.M)
    body = markdown.markdown(body_md, extensions=["tables", "fenced_code", "sane_lists"])
    body = body.replace("<li><span class='check'>", "<li class='cb'><span class='check'>")
    body = re.sub(r"<h2>(\d+\. )", r'<h2 class="chap">\1', body)              # each numbered chapter on a new page
    body = body.replace("<h2>Contents</h2>", '<div class="toc"><h2>Contents</h2>', 1)
    body = re.sub(r'(<div class="toc"><h2>Contents</h2>\s*<ol>.*?</ol>)', r"\1</div>", body, count=1, flags=re.S)
    cover = f"""<section class="cover"><div class="logo">▲</div><h1>{html.escape(title)}</h1>
      <div class="sub">{html.escape(tagline)}</div>
      <div class="meta">{html.escape(version)}<br>Project: github.com/Sumit211995/delta_bot</div>
      <div><span class="badge">● Current mode: PAPER TRADING ONLY. No real orders.</span></div></section>"""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>{cover}{body}</body></html>"""
    OUT_HTML.write_text(page, encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=8000",
                    f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"wrote {OUT_PDF} ({OUT_PDF.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    sys.exit(build())
