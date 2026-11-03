"""Shared helpers for the Troostwijk finder."""
import json
import os
import re
import threading
import time
from urllib.parse import quote

from curl_cffi import requests

BASE = "https://www.troostwijkauctions.com"

# Sale types where the highest bidder wins
WIN_TERMS = {"GUARANTEED_SALE", "OPEN_RESERVE_PRICE_ACHIEVED"}

_local = threading.local()
_lock = threading.Lock()

# Counters for the status report: page/feed results by type
STATS = {}


def count(key, n=1):
    with _lock:
        STATS[key] = STATS.get(key, 0) + n


# Adaptive slow-down: every "blocked" answer (403/429) adds delay, every success
# removes a little. Shared by all threads in this process.
_extra = [0.0]


def _blocked():
    with _lock:
        _extra[0] = min(_extra[0] * 1.5 + 1.0, 8.0)


def _ok():
    with _lock:
        _extra[0] = max(0.0, _extra[0] * 0.92 - 0.02)


def _pause(base):
    time.sleep(base + _extra[0])


def _session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session(impersonate="chrome")
    return _local.s


def fetch(url, delay=1.0, tries=3):
    """Download a page politely. Returns the HTML, or None if it keeps failing."""
    for attempt in range(tries):
        try:
            _pause(delay)
            r = _session().get(url, timeout=30)
            count(f"page_{r.status_code}")
            if r.status_code == 200:
                _ok()
                return r.text
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429):
                _blocked()
                time.sleep(20 * (attempt + 1))  # rate limit: wait it out
                continue
            print(f"  status {r.status_code} for {url}")
        except Exception as e:
            count("page_error")
            print(f"  error {e} for {url}")
        time.sleep(5 * (attempt + 1))
    count("page_gave_up")
    return None


def next_data(html):
    """Pull the hidden page data out of a Troostwijk page."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


CATEGORIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "categories.json")


def get_categories():
    """
    All subcategories as a list of (main category, subcategory, path).
    A main category without subcategories is scanned as a whole.
    Falls back to the saved list if the homepage can't be loaded.
    """
    cats = _categories_from_homepage()
    if cats:
        return cats
    try:
        with open(CATEGORIES_FILE, encoding="utf-8") as f:
            saved = [tuple(c) for c in json.load(f)]
        print(f"Homepage failed - using saved list of {len(saved)} subcategories")
        count("categories_from_file")
        return saved
    except (OSError, ValueError):
        return []


def _categories_from_homepage():
    data = next_data(fetch(f"{BASE}/nl", tries=4))
    out, seen = [], set()

    def add(main, sub, slug):
        slug = (slug or "").split("?")[0]
        path = quote("/c/" + slug, safe="/-'")
        if re.search(r"/[0-9a-f-]{36}$", slug) and path not in seen:
            seen.add(path)
            out.append((main, sub, path))

    if data:
        for c in data["props"]["pageProps"].get("menuCategories", []):
            main = c.get("title", "")
            children = c.get("children") or []
            for ch in children:
                add(main, ch.get("title", main), ch.get("slug"))
            if not children:
                add(main, main, c.get("slug"))
    return out


# Sale type where a reserve exists but isn't reached yet. Kept only for valuable
# subcategories, because the reserve is often reached before closing.
RESERVE_NOT_MET = "OPEN_RESERVE_PRICE_NOT_ACHIEVED"


def valuable_subcategories():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "collections.json")
    try:
        with open(path, encoding="utf-8") as f:
            return {s for c in json.load(f) for s in c.get("subs", [])}
    except (OSError, ValueError):
        return set()


QTY_PATTERNS = [
    r"\((\d{1,4})\s*[x×]\)",                  # "(5x)"
    r"\b(\d{1,4})\s*[x×]\s",                   # "5x pallet"
    r"\b(\d{1,4})\s*(?:stuks|stuk|st\.|pcs|pieces|stück)\b",
    r"\bset\s+van\s+(\d{1,4})\b",
]


def quantity_from_title(title):
    t = (title or "").lower()
    for p in QTY_PATTERNS:
        m = re.search(p, t)
        if m:
            n = int(m.group(1))
            if 1 < n <= 5000:
                return n
    return 1


# ---------- Troostwijk's own data feed (the one their website uses) ----------

GRAPHQL = "https://storefront.tbauctions.com/storefront/graphql"
FEED_HEADERS = {
    "content-type": "application/json",
    "origin": "https://www.troostwijkauctions.com",
    "referer": "https://www.troostwijkauctions.com/",
}

LOT_BIDDING_QUERY = (
    "query LotBiddingData($lotDisplayId:String!$locale:String!$platform:Platform!){"
    "lotDetails(displayId:$lotDisplayId locale:$locale platform:$platform){"
    "estimatedFullPrice{saleTerm}"
    "lot{id displayId currentBidAmount{cents currency}initialAmount{cents currency}"
    "vat markupPercentage biddingStatus bidsCount endDate minimumBidAmountMet}}}"
)


def graphql(query, variables, tries=2):
    for attempt in range(tries):
        try:
            _pause(0.25)
            r = _session().post(GRAPHQL, json={"query": query, "variables": variables},
                                headers=FEED_HEADERS, timeout=30)
            count(f"feed_{r.status_code}")
            if r.status_code == 200:
                _ok()
                data = r.json()
                if data.get("data"):
                    return data["data"]
                count("feed_empty")
                return None
            if r.status_code in (403, 429):
                _blocked()
                time.sleep(20 * (attempt + 1))
                continue
        except Exception as e:
            count("feed_error")
            print(f"  feed error {e}")
        time.sleep(3 * (attempt + 1))
    count("feed_gave_up")
    return None


VOLATILE_QUERY = (
    "query VolatileLotDataByIds($request:VolatileLotDataInput!$platform:Platform!){"
    "volatileLotDataByIds(request:$request platform:$platform){"
    "lotId biddingStatus bidsCount currentPriceInCents endDate}}"
)


def volatile_batch(uuids):
    """Status, bids and price for many lots in one request. Returns {uuid: {...}}."""
    data = graphql(VOLATILE_QUERY, {"request": {"lotIds": list(uuids)}, "platform": "TWK"})
    out = {}
    for x in (data or {}).get("volatileLotDataByIds") or []:
        out[x["lotId"]] = {"status": x.get("biddingStatus"), "bids": x.get("bidsCount") or 0,
                           "amount": (x.get("currentPriceInCents") or 0) / 100, "end": x.get("endDate")}
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def lot_bidding(display_id):
    """Live or final bidding data for one lot, from the data feed."""
    data = graphql(LOT_BIDDING_QUERY, {"lotDisplayId": display_id, "locale": "nl", "platform": "TWK"})
    details = (data or {}).get("lotDetails") or {}
    lot = details.get("lot")
    if not lot:
        return None
    return {
        "status": lot.get("biddingStatus"),
        "term": ((details.get("estimatedFullPrice") or {}).get("saleTerm")) or "",
        "bids": lot.get("bidsCount") or 0,
        "amount": ((lot.get("currentBidAmount") or {}).get("cents") or 0) / 100,
        "start": ((lot.get("initialAmount") or {}).get("cents") or 0) / 100,
        "premium": _num(lot.get("markupPercentage")),
        "vat": _num(lot.get("vat")),
        "end": lot.get("endDate"),
    }


def sold_from(term, bids):
    """True = sold, False = not sold, None = still waiting for the seller."""
    t = (term or "").upper()
    if "PENDING" in t:
        return None
    if any(w in t for w in ("DECLINED", "UNSOLD", "NOT_ACHIEVED")):
        return False
    return bids > 0
