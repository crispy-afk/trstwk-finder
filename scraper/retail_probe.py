"""
Test which shop / price-comparison sites GitHub can read, so the finder
can look up new retail prices automatically.

Usage: python scraper/retail_probe.py "makita dhp484"
"""
import os
import re
import sys
import time
from urllib.parse import quote_plus

from curl_cffi import requests

SOURCES = {
    "Tweakers Pricewatch": "https://tweakers.net/zoeken/?keyword={q}&type=pricewatch",
    "Kieskeurig": "https://www.kieskeurig.nl/search?q={q}",
    "Beslist": "https://www.beslist.nl/products/r/{q}/",
    "bol.com": "https://www.bol.com/nl/nl/s/?searchtext={q}",
    "Amazon.nl": "https://www.amazon.nl/s?k={q}",
    "idealo.de": "https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={q}",
    "Google Shopping": "https://www.google.com/search?tbm=shop&hl=nl&gl=nl&q={q}",
    "DuckDuckGo": "https://html.duckduckgo.com/html/?q={q}+prijs",
}

BLOCK_SIGNS = ["myprivacy.dpgmedia", "captcha", "just a moment", "cf-chl", "robot check",
               "consent.google", "unusual traffic", "toestemming", "access denied"]
PRICE = re.compile(r"(?:€|&euro;|EUR)\s?(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2}|,-)?|\d+(?:,\d{2})?)")


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "makita dhp484"
    os.makedirs("probe", exist_ok=True)
    lines = [f"## Retail probe for: `{query}`", "",
             "| Site | Result | Status | Prices found | Example |", "|---|---|---|---|---|"]
    s = requests.Session(impersonate="chrome")
    for name, url in SOURCES.items():
        time.sleep(2)
        try:
            r = s.get(url.format(q=quote_plus(query)), timeout=30, allow_redirects=True)
            html, status, final = r.text, r.status_code, str(r.url)
        except Exception as e:
            lines.append(f"| {name} | ERROR | - | - | {str(e)[:60]} |")
            continue
        low = (html + final).lower()
        prices = PRICE.findall(html)
        blocked = status in (403, 429, 503) or any(b in low for b in BLOCK_SIGNS)
        verdict = "BLOCKED" if blocked else ("OK" if len(prices) >= 3 else "NO PRICES")
        safe = re.sub(r"[^a-z0-9]+", "-", name.lower())
        with open(f"probe/{safe}.html", "w", encoding="utf-8") as f:
            f.write(html)
        example = ", ".join("€" + p for p in prices[:3])
        lines.append(f"| {name} | **{verdict}** | {status} | {len(prices)} | {example} |")
        print(name, verdict, status, len(prices), final[:80])

    lines += ["", "Send Claude this table and the zip under Artifacts."]
    report = "\n".join(lines)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
