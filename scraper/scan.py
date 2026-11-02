"""
Scan part of Troostwijk's subcategories (one "shard") and save the open lots
where the highest bid wins. For valuable subcategories (see docs/collections.json)
it also keeps lots whose reserve price isn't reached yet.

Usage: python scraper/scan.py SHARD TOTAL_SHARDS
"""
import json
import os
import sys
import time

from common import (BASE, RESERVE_NOT_MET, WIN_TERMS, fetch, get_categories,
                    next_data, valuable_subcategories)

HORIZON_HOURS = int(os.environ.get("HORIZON_HOURS", "72"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "80"))
VALUABLE = valuable_subcategories()


def compact(lot, main, sub):
    loc = lot.get("location") or {}
    return {
        "id": lot["displayId"],
        "title": lot.get("title", ""),
        "slug": lot.get("urlSlug", ""),
        "img": (lot.get("image") or {}).get("url"),
        "bid": ((lot.get("currentBidAmount") or {}).get("cents") or 0) / 100,
        "bids": lot.get("bidsCount", 0),
        "end": lot.get("endDate"),
        "city": loc.get("city", ""),
        "cc": (loc.get("countryCode") or "").upper(),
        "term": lot.get("saleTerm"),
        "followers": lot.get("followersCount", 0),
        "cat": main,
        "sub": sub,
    }


def wanted(lot, sub):
    term = lot.get("saleTerm")
    return term in WIN_TERMS or (term == RESERVE_NOT_MET and sub in VALUABLE)


def scan_category(main, sub, path, now, lots):
    limit = now + HORIZON_HOURS * 3600
    kept, page = 0, 0
    for page in range(1, MAX_PAGES + 1):
        data = next_data(fetch(f"{BASE}/nl{path}?page={page}"))
        if not data:
            break
        ld = data["props"]["pageProps"].get("lotsData") or {}
        results = ld.get("results") or []
        if not results:
            break
        for lot in results:
            end = lot.get("endDate") or 0
            if (lot.get("biddingStatus") == "BIDDING_OPEN" and wanted(lot, sub)
                    and now < end <= limit and lot["displayId"] not in lots):
                lots[lot["displayId"]] = compact(lot, main, sub)
                kept += 1
        # Lots come roughly in closing order: stop once a whole page is past the window
        if min((l.get("endDate") or 0) for l in results) > limit:
            break
        if page * len(results) >= (ld.get("totalSize") or 0):
            break
    if kept:
        print(f"{main} > {sub}: {kept} lots ({page} pages)")
    return page


def main():
    shard, total = int(sys.argv[1]), int(sys.argv[2])
    now = int(time.time())
    cats = get_categories()
    if not cats:
        sys.exit("No categories found - Troostwijk may have changed their site.")
    mine = [c for i, c in enumerate(cats) if i % total == shard]
    print(f"Shard {shard + 1}/{total}: {len(mine)} of {len(cats)} subcategories")

    lots, pages = {}, 0
    for main_cat, sub, path in mine:
        pages += scan_category(main_cat, sub, path, now, lots)

    os.makedirs("out", exist_ok=True)
    with open(f"out/shard-{shard}.json", "w", encoding="utf-8") as f:
        json.dump({"scannedAt": now, "pages": pages, "categories": len(mine),
                   "lots": list(lots.values())}, f, ensure_ascii=False)
    print(f"Saved {len(lots)} lots from {pages} pages")


if __name__ == "__main__":
    main()
