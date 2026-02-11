#!/usr/bin/env python3
"""Convert MM_BOT_PLAN.md to HTML."""
import markdown, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "docs"
md_text = (ROOT / "MM_BOT_PLAN.md").read_text(encoding="utf-8")
html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "toc"])

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
       max-width: 1000px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #24292f; }
h1 { border-bottom: 2px solid #d0d7de; padding-bottom: 8px; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 6px; margin-top: 32px; }
h3 { margin-top: 24px; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; }
th { background-color: #f6f8fa; font-weight: 600; }
tr:nth-child(even) { background-color: #f6f8fa; }
code { background-color: #f6f8fa; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }
pre { background-color: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
strong { color: #1a7f37; }
@media print { body { max-width: 100%; } }
"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BotMM - Market Making Bot Plan</title>
<style>{CSS}</style>
</head>
<body>
{html_body}
</body>
</html>"""

out = ROOT / "MM_BOT_PLAN.html"
out.write_text(html, encoding="utf-8")
print(f"Generated {out} ({len(html):,} chars)")
