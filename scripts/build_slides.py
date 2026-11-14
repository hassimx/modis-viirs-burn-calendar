"""Render submission/04_slides.md to submission/slides.pdf (16:9 pages)."""
import base64
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "submission" / "04_slides.md"
HTML = ROOT / "submission" / "slides.html"


def inline_images(html: str) -> str:
    def rep(m):
        p = (SRC.parent / m.group(1)).resolve()
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f'src="data:image/png;base64,{b64}"'
    return re.sub(r'src="([^"]+\.png)"', rep, html)


def main(url_text: str = "") -> None:
    text = SRC.read_text(encoding="utf-8")
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    if url_text:
        text = text.replace("LIVE_URL", url_text)
    slides = [s.strip() for s in text.split("\n---\n") if s.strip()]
    parts = []
    for s in slides:
        body = markdown.markdown(s, extensions=["tables"])
        has_img = "<img" in body
        parts.append(f'<section class="slide{" img" if has_img else ""}">{body}</section>')
    css = """
    @page { size: 1280px 720px; margin: 0; }
    body { margin: 0; font-family: 'Helvetica Neue', Arial, sans-serif; color: #1d1f22; }
    .slide { width: 1280px; height: 720px; box-sizing: border-box; padding: 48px 64px; page-break-after: always;
             background: #f4f2ee; position: relative; overflow: hidden; display: flex; flex-direction: column; }
    .slide h1 { font-size: 40px; margin: 0 0 18px; color: #c2410c; }
    .slide ul, .slide ol { font-size: 21px; line-height: 1.45; margin: 0 0 12px; }
    .slide p { font-size: 20px; margin: 6px 0; }
    .slide img { max-height: 330px; max-width: 100%; object-fit: contain; align-self: center; margin-top: auto;
                 border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,.08); background: #fff; }
    table { border-collapse: collapse; font-size: 18px; margin: 4px 0 8px; }
    th, td { padding: 5px 14px; border-bottom: 1px solid #ddd6cb; text-align: right; }
    th:first-child, td:first-child { text-align: left; }
    .slide:first-child { background: #1d1f22; color: #f4f2ee; }
    .slide:first-child h1 { font-size: 64px; color: #fbbf24; }
    """
    HTML.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>"
                    + inline_images("".join(parts)) + "</body></html>", encoding="utf-8")
    print(f"{len(slides)} slides -> {HTML}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
