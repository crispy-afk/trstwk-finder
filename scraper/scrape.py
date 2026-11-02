"""
Troostwijk finder - scraper.

Reads every Troostwijk category page, keeps lots that go to the highest
bidder (no reserve, or reserve already met) and that close within the
time window, and writes them to docs/lots.json for the dashboard.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from curl_cffi import requests

BASE = "https://www.troostwijkauctions.com"
HORIZON_HOURS = int(os.environ.get("HORIZON_HOURS", "72"))
MAX_PAGES_PER_CATEGORY = int(os.environ.get("MAX_PAGES", "60"))
DELAY_SECONDS = 1.2
OUTPUT = os.path.join(os.path.dirname(__file__), "..", "docs", "lots.json")

# Sale types where the highest bidder wins
WIN_TERMS = {"GUARANTEED_SALE", "OPEN_RESERVE_PRICE_ACHIEVED"}

session = requests.Session(impersonate="chrome")


def fetch(url):
    for attempt in range(3):
        try:
            time.sleep(DELAY_SECONDS)
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  status {r.status_code} for {url}")
        except Exception as e:
            print(f"  error {e} for {url}")
        time.sleep(5 * (attempt + 1))
    return None


def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    return json.loads(m.group(1)) if m else None


def get_categories():
    """Top-level categories as (name, path) pairs."""
    data = next_data(fetch(f"{BASE}/nl"))
    cats = []
    if data:
        for c in data["props"]["pageProps"].get("menuCategories", []):
            slug = c.get("slug", "").split("?")[0]
            path = "/c/" + slug
            if re.search(r"/[0-9a-f-]{36}$", slug) and path not in [p for _, p in cats]:
                cats.append((c.get("title", slug), path))
    return cats


def compact(lot, category):
    bid = (lot.get("currentBidAmount") or {}).get("cents") or 0
    loc = lot.get("location") or {}
    return {
        "id": lot["displayId"],
        "title": lot.get("title", ""),
        "slug": lot.get("urlSlug", ""),
        "img": (lot.get("image") or {}).get("url"),
        "bid": bid / 100,
        "bids": lot.get("bidsCount", 0),
        "end": lot.get("endDate"),
        "city": loc.get("city", ""),
        "cc": (loc.get("countryCode") or "").upper(),
        "term": lot.get("saleTerm"),
        "followers": lot.get("followersCount", 0),
        "cat": category,
    }


def scrape_category(name, path, now, lots):
    limit = now + HORIZON_HOURS * 3600
    kept = 0
    for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
        data = next_data(fetch(f"{BASE}/nl{path}?page={page}"))
        if not data:
            break
        ld = data["props"]["pageProps"].get("lotsData") or {}
        results = ld.get("results") or []
        if not results:
            break
        for lot in results:
            end = lot.get("endDate") or 0
            if (lot.get("biddingStatus") == "BIDDING_OPEN"
                    and lot.get("saleTerm") in WIN_TERMS
                    and now < end <= limit
                    and lot["displayId"] not in lots):
                lots[lot["displayId"]] = compact(lot, name)
                kept += 1
        # Lots come roughly in closing order: stop once a whole page is past the window
        if min((l.get("endDate") or 0) for l in results) > limit:
            break
        if page * len(results) >= (ld.get("totalSize") or 0):
            break
    print(f"{name}: {kept} lots kept ({page} pages)")


def main():
    now = int(time.time())
    cats = get_categories()
    print(f"{len(cats)} categories found")
    if not cats:
        sys.exit("No categories found - Troostwijk may have changed their site.")

    lots = {}
    for name, path in cats:
        scrape_category(name, path, now, lots)

    if not lots:
        sys.exit("Zero lots found - something is broken, keeping the old data.")

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizonHours": HORIZON_HOURS,
        "count": len(lots),
        "lots": sorted(lots.values(), key=lambda l: l["end"]),
    }
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Saved {len(lots)} lots to docs/lots.json")


if __name__ == "__main__":
    main()
