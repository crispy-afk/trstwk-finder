"""
Open lot pages in a real (headless) browser and record every data response
the page loads, to find where Troostwijk keeps final prices of closed lots.

Usage: python scraper/inspect_lots.py "slug1,slug2,..."
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://www.troostwijkauctions.com/nl/l/"
OUT = "inspect"


def main():
    slugs = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    os.makedirs(OUT, exist_ok=True)
    report = ["## Lot inspection", ""]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="nl-NL", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
        for i, slug in enumerate(slugs):
            page = ctx.new_page()
            captured = []

            def on_response(resp, captured=captured):
                ct = resp.headers.get("content-type", "")
                if "json" in ct or "graphql" in resp.url:
                    try:
                        body = resp.text()
                    except Exception:
                        return
                    captured.append({"url": resp.url, "status": resp.status,
                                     "method": resp.request.method,
                                     "request": resp.request.post_data,
                                     "body": body[:200000]})

            page.on("response", on_response)
            try:
                page.goto(BASE + slug, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(4000)
            except Exception as e:
                report.append(f"- `{slug}`: load error {e}")
            html = page.content()
            text = page.inner_text("body") if page.query_selector("body") else ""
            with open(f"{OUT}/{i}-page.html", "w", encoding="utf-8") as f:
                f.write(html)
            with open(f"{OUT}/{i}-text.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open(f"{OUT}/{i}-responses.json", "w", encoding="utf-8") as f:
                json.dump(captured, f, ensure_ascii=False, indent=1)
            money = re.findall(r"€\s?[\d.]+(?:,\d{2})?", text)[:8]
            with_bid = [c["url"][:100] for c in captured if re.search(r"bid", c["body"], re.I)]
            report.append(f"- **{slug}**: {len(captured)} data responses, "
                          f"{len(with_bid)} mention bids. Prices on page: {', '.join(money) or 'none'}")
            for u in with_bid[:5]:
                report.append(f"  - `{u}`")
            page.close()
        browser.close()
    report += ["", "Download the zip under Artifacts and send it to Claude."]
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
