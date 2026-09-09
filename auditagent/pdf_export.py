"""
AuditAgent — PDF Export
Converts a self-contained HTML report to PDF using Playwright's built-in
print-to-PDF — no new dependency: Playwright + Chromium is already a
required install for crawler.py's actual crawling, so this reuses the
exact same browser binary rather than adding a second HTML-to-PDF
library just for this.

Sync API deliberately, not async: this gets called directly from a
synchronous FastAPI route handler for a quick, on-demand conversion, not
a long-running background job.
"""

from playwright.sync_api import sync_playwright


def html_to_pdf(html_path: str, pdf_path: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{html_path}")
        # Dark-themed reports need the background painted into the PDF
        # explicitly — browsers skip background colors on print by
        # default, which would otherwise produce a PDF with a plain white
        # background clashing with light-colored text.
        page.emulate_media(media="screen")
        page.pdf(path=pdf_path, format="A4", print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()
