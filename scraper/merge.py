"""
Merge the shard scans, keep price history, and build the dashboard data.

Steps:
 1. Merge open lots from all shards and record a price snapshot per lot.
 2. Fetch the lot page once for new lots (brand, quantity, VAT type, pickup).
 3. Fetch the lot page for lots that just closed and save the final price.
 4. Learn how much prices rise before closing, and find similar sold lots.
 5. Look up map coordinates for cities (for distance and travel cost).
 6. Write _site/ for GitHub Pages.
"""
import glob
import gzip
import json
import os
import re
import shutil
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from curl_cffi import requests as http

from common import BASE, fetch, next_data, quantity_from_title

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STATE = os.path.join(ROOT, "state", "state.json")
HISTORY = os.path.join(ROOT, "data", "history")
GEOCACHE = os.path.join(ROOT, "data", "geocache.json")
SITE = os.path.join(ROOT, "_site")

ENRICH_CAP = int(os.environ.get("ENRICH_CAP", "700"))
CLOSE_CAP = int(os.environ.get("CLOSE_CAP", "900"))
GEO_CAP = int(os.environ.get("GEO_CAP", "150"))
WORKERS = 4
HISTORY_DAYS = 120
SHOW_IF_SEEN_WITHIN = 3 * 3600

NOW = int(time.time())


# ---------- files ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


# ---------- lot pages ----------

BRAND_NAMES = {"merk", "brand", "marke", "marque", "fabrikant", "manufacturer", "hersteller", "marca"}
MODEL_NAMES = {"type", "model", "modell", "modèle", "modelo", "typ", "typenummer"}


def lot_page(slug):
    data = next_data(fetch(f"{BASE}/nl/l/{slug}", delay=0.4, tries=2))
    if not data:
        return None
    return data.get("props", {}).get("pageProps")


def parse_details(pp):
    lot = pp.get("lot") or {}
    brand = model = None
    for a in lot.get("attributes") or []:
        name = (a.get("name") or "").strip().lower()
        value = (a.get("value") or "").strip()
        if not value:
            continue
        if name in BRAND_NAMES and not brand:
            brand = value
        elif name in MODEL_NAMES and not model:
            model = value
    pickup = pp.get("collectionDays") or {}
    days = pickup.get("days") or [{}]
    first = days[0] if days else {}
    qty = lot.get("quantity") or 1
    return {
        "brand": brand,
        "model": model,
        "qty": qty if isinstance(qty, int) and qty > 0 else 1,
        "margin": bool(lot.get("marginGood")),
        "condition": lot.get("condition"),
        "pickupCity": pickup.get("city"),
        "pickupCc": (pickup.get("countryCode") or "").upper(),
        "pickupStart": first.get("startDate"),
        "pickupEnd": first.get("endDate"),
        "appointment": bool(first.get("isByAppointment")),
        "start": ((lot.get("initialAmount") or {}).get("cents") or 0) / 100,
    }


def parse_close(pp):
    lot = pp.get("lot") or {}
    return {
        "status": lot.get("biddingStatus"),
        "term": lot.get("saleTerm") or "",
        "bids": lot.get("bidsCount") or 0,
        "amount": ((lot.get("currentBidAmount") or {}).get("cents") or 0) / 100,
    }


def run_parallel(func, items):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(func, items))


# ---------- step 1: merge ----------

def merge_shards(tracked):
    files = sorted(glob.glob(os.path.join(ROOT, "out", "shard-*.json")))
    total = 0
    for path in files:
        shard = load_json(path, {})
        for lot in shard.get("lots", []):
            total += 1
            t = tracked.get(lot["id"]) or {"snaps": [], "details": None, "tries": 0}
            t.update(lot)
            snaps = t["snaps"]
            if not snaps or NOW - snaps[-1][0] > 1800:
                snaps.append([NOW, lot["bid"], lot["bids"], lot["followers"]])
                del snaps[:-100]
            t["seen"] = NOW
            tracked[lot["id"]] = t
    print(f"Merged {total} lots from {len(files)} shards")
    return total, len(files)


# ---------- step 2: details for new lots ----------

def enrich(tracked):
    todo = [t for t in tracked.values()
            if t.get("details") is None and t.get("tries", 0) < 3 and t["end"] > NOW + 600]
    todo.sort(key=lambda t: t["end"])
    todo = todo[:ENRICH_CAP]

    def work(t):
        pp = lot_page(t["slug"])
        return parse_details(pp) if pp else None

    results = run_parallel(work, todo)
    ok = 0
    for t, d in zip(todo, results):
        if d:
            t["details"] = d
            ok += 1
        else:
            t["tries"] = t.get("tries", 0) + 1
    print(f"Details: {ok}/{len(todo)} new lots")


# ---------- step 3: final prices ----------

def close_lots(tracked):
    due = [t for t in tracked.values() if t["end"] < NOW - 300]
    due.sort(key=lambda t: t["end"])
    due = due[:CLOSE_CAP]

    def work(t):
        pp = lot_page(t["slug"])
        return parse_close(pp) if pp else None

    results = run_parallel(work, due)
    records, still_open, failed = [], 0, 0
    for t, c in zip(due, results):
        if c and c["status"] == "BIDDING_OPEN":
            still_open += 1
            t["closeTries"] = t.get("closeTries", 0) + 1
            if t["closeTries"] < 6:
                continue
        elif not c:
            failed += 1
            t["closeTries"] = t.get("closeTries", 0) + 1
            if t["closeTries"] < 4 and t["end"] > NOW - 3 * 86400:
                continue
        tracked.pop(t["id"], None)
        if not c or c["status"] == "BIDDING_OPEN":
            continue
        term = c["term"].upper()
        if "PENDING" in term:
            sold = None
        else:
            sold = c["bids"] > 0 and "DECLINED" not in term and "UNSOLD" not in term
        d = t.get("details") or {}
        records.append({
            "id": t["id"], "title": t["title"], "slug": t["slug"], "cat": t["cat"], "sub": t.get("sub"),
            "cc": t["cc"], "end": t["end"], "term": c["term"], "sold": sold,
            "final": c["amount"] if sold else None, "bids": c["bids"],
            "followers": t["followers"], "qty": d.get("qty") or quantity_from_title(t["title"]),
            "brand": d.get("brand"), "model": d.get("model"), "snaps": t["snaps"],
        })
    print(f"Closed: {len(records)} saved, {still_open} still open, {failed} failed")
    return records


def write_history(records):
    if not records:
        return
    hour = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y/%m/%d/%H")
    path = os.path.join(HISTORY, hour + ".json.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = {}
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            existing = {r["id"]: r for r in json.load(f)}
    existing.update({r["id"]: r for r in records})
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(list(existing.values()), f, ensure_ascii=False, separators=(",", ":"))


def load_history():
    cutoff = NOW - HISTORY_DAYS * 86400
    out = []
    for path in glob.glob(os.path.join(HISTORY, "**", "*.json.gz"), recursive=True):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                out.extend(r for r in json.load(f) if r.get("end", 0) >= cutoff)
        except (OSError, ValueError):
            print(f"  skipped unreadable history file {path}")
    return out


# ---------- step 4a: how much do prices rise? ----------

HOUR_BINS = [1, 3, 6, 12, 24, 48, 72, 10 ** 9]
FOLLOWER_BINS = [5, 20, 50, 10 ** 9]
MIN_SAMPLES = {"s": 15, "c": 15, "*": 15, "all": 10}


def bin_of(value, bins):
    for i, edge in enumerate(bins):
        if value < edge:
            return i
    return len(bins) - 1


def build_model(history):
    groups = defaultdict(list)
    for r in history:
        if not r.get("sold") or not r.get("final"):
            continue
        per_bin = {}
        for t, bid, bids, followers in r.get("snaps") or []:
            if bid <= 0 or t >= r["end"]:
                continue
            hb = bin_of((r["end"] - t) / 3600, HOUR_BINS)
            per_bin[hb] = (bid, followers)  # latest snapshot per time bin
        for hb, (bid, followers) in per_bin.items():
            ratio = r["final"] / bid
            if ratio > 50:
                continue
            fb = bin_of(followers, FOLLOWER_BINS)
            if r.get("sub"):
                groups[(f"s:{r['sub']}", hb, fb)].append(ratio)
            groups[(f"c:{r['cat']}", hb, fb)].append(ratio)
            groups[("*", hb, fb)].append(ratio)
            groups[("*", hb, "*")].append(ratio)
    model = {}
    for key, ratios in groups.items():
        level = "all" if key[2] == "*" else key[0][:1]
        if len(ratios) >= MIN_SAMPLES[level]:
            ratios.sort()
            model["|".join(map(str, key))] = [
                round(statistics.median(ratios), 3),
                round(ratios[int(len(ratios) * 0.75)], 3),
                len(ratios),
            ]
    return model


def predict(lot, model):
    if lot["bid"] <= 0:
        return None
    hb = bin_of((lot["end"] - NOW) / 3600, HOUR_BINS)
    fb = bin_of(lot["followers"], FOLLOWER_BINS)
    for key in (f"s:{lot.get('sub')}|{hb}|{fb}", f"c:{lot['cat']}|{hb}|{fb}", f"*|{hb}|{fb}", f"*|{hb}|*"):
        if key in model:
            med, high, n = model[key]
            return {"mid": round(lot["bid"] * max(med, 1), 2),
                    "high": round(lot["bid"] * max(high, 1), 2), "n": n}
    return None


# ---------- step 4b: similar sold lots ----------

STOP = set("""de het een en van voor met op in te aan of bij uit zonder incl inclusief ca circa
the a an and of for with on in to at or without approx
der die das und mit für von zu ohne le la les et avec pour sans
diverse divers diversen various mixed partij lot kavel stuks stuk st pcs set x""".split())


def tokens(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = {w for w in words if w not in STOP and (len(w) > 2 or any(c.isdigit() for c in w))}
    # "dhp484z" also counts as "dhp484" (shops add letter suffixes to model numbers)
    out |= {w[:-1] for w in out if re.fullmatch(r"[a-z]+\d{2,}[a-z]", w)}
    return out


class Comparables:
    def __init__(self, history):
        self.items = []
        self.index = defaultdict(list)
        for r in history:
            if r.get("sold") and r.get("final"):
                tok = tokens(f"{r['title']} {r.get('brand') or ''} {r.get('model') or ''}")
                if len(tok) < 2:
                    continue
                i = len(self.items)
                self.items.append((tok, r["final"] / max(r.get("qty") or 1, 1), r))
                for w in tok:
                    self.index[w].append(i)

    def find(self, lot, qty):
        d = lot.get("details") or {}
        tok = tokens(f"{lot['title']} {d.get('brand') or ''} {d.get('model') or ''}")
        if len(tok) < 2:
            return None
        counts = defaultdict(int)
        for w in tok:
            hits = self.index.get(w, [])
            if len(hits) <= 300:  # ignore very common words
                for i in hits:
                    counts[i] += 1
        matches = []
        for i, shared in counts.items():
            if shared < 2:
                continue
            other, unit, r = self.items[i]
            score = shared / len(tok | other)
            if score >= 0.5:
                matches.append((score, unit, r))
        if not matches:
            return None
        matches.sort(key=lambda m: -m[0])
        top = matches[:12]
        unit_prices = [m[1] for m in top]
        return {
            "price": round(statistics.median(unit_prices) * qty, 2),
            "n": len(top),
            "examples": [{"title": r["title"], "final": r["final"], "slug": r["slug"],
                          "end": r["end"]} for _, _, r in top[:3]],
        }


# ---------- step 5: map coordinates ----------

def geocode(places, cache):
    todo = [p for p in places if p not in cache][:GEO_CAP]
    repo = os.environ.get("GITHUB_REPOSITORY", "troostwijk-finder")
    headers = {"User-Agent": f"troostwijk-finder/2 (https://github.com/{repo})"}
    found = 0
    for key in todo:
        city, cc = key.split("|")
        try:
            r = http.get("https://nominatim.openstreetmap.org/search",
                         params={"city": city, "countrycodes": cc.lower(), "format": "json", "limit": 1},
                         headers=headers, timeout=20)
            res = r.json() if r.status_code == 200 else None
            cache[key] = [round(float(res[0]["lat"]), 4), round(float(res[0]["lon"]), 4)] if res else None
            found += bool(res)
        except Exception as e:
            print(f"  geocode error {key}: {e}")
        time.sleep(1.1)  # Nominatim rule: max 1 request per second
    print(f"Geocoded {found}/{len(todo)} new places ({len(cache)} cached)")


# ---------- step 6: dashboard data ----------

def build_site(tracked, model, comps, geo, history, run):
    lots = []
    for t in tracked.values():
        if t["end"] <= NOW or NOW - t.get("seen", 0) > SHOW_IF_SEEN_WITHIN:
            continue
        d = t.get("details") or {}
        qty = d.get("qty") if d.get("qty", 1) > 1 else quantity_from_title(t["title"])
        city, cc = (d.get("pickupCity") or t["city"]), (d.get("pickupCc") or t["cc"])
        out = {k: t.get(k) for k in ("id", "title", "slug", "img", "bid", "bids", "end",
                                     "city", "cc", "term", "followers", "cat", "sub")}
        out.update({
            "qty": qty,
            "brand": d.get("brand"), "model": d.get("model"),
            "margin": d.get("margin", False), "condition": d.get("condition"),
            "pickupStart": d.get("pickupStart"), "appointment": d.get("appointment"),
            "geo": geo.get(f"{city.strip().upper()}|{cc}") if city else None,
            "pred": predict(t, model),
            "comps": comps.find(t, qty),
            "trend": [s[1] for s in t["snaps"][-12:]],
        })
        lots.append(out)
    lots.sort(key=lambda l: l["end"])

    sold = sum(1 for r in history if r.get("sold"))
    meta = {
        "generatedAt": datetime.fromtimestamp(NOW, timezone.utc).isoformat(timespec="seconds"),
        "count": len(lots),
        "historyLots": len(history),
        "historySold": sold,
        "modelGroups": len(model),
        "run": run,
    }
    os.makedirs(SITE, exist_ok=True)
    for name in ("index.html", "collections.json"):
        shutil.copy(os.path.join(ROOT, "docs", name), os.path.join(SITE, name))
    save_json(os.path.join(SITE, "lots.json"), {"meta": meta, "lots": lots})
    save_json(os.path.join(SITE, "model.json"), {"hourBins": HOUR_BINS, "followerBins": FOLLOWER_BINS,
                                                 "groups": model})
    print(f"Dashboard: {len(lots)} lots, history {len(history)} ({sold} sold), {len(model)} model groups")


def main():
    started = time.time()
    state = load_json(STATE, {"tracked": {}})
    tracked = state["tracked"]
    print(f"State: {len(tracked)} tracked lots")

    merged, shards = merge_shards(tracked)
    if merged == 0:
        sys.exit("Zero lots from all shards - something is broken. Keeping old data.")

    enrich(tracked)
    records = close_lots(tracked)
    write_history(records)

    history = load_history()
    model = build_model(history)
    comps = Comparables(history)

    geo = load_json(GEOCACHE, {})
    places = set()
    for t in tracked.values():
        d = t.get("details") or {}
        city, cc = (d.get("pickupCity") or t["city"]), (d.get("pickupCc") or t["cc"])
        if city and cc:
            places.add(f"{city.strip().upper()}|{cc}")
    geocode(sorted(places), geo)
    save_json(GEOCACHE, geo)

    run = {"shards": shards, "merged": merged, "closedSaved": len(records),
           "tracked": len(tracked), "seconds": round(time.time() - started)}
    build_site(tracked, model, comps, geo, history, run)
    save_json(STATE, state)


if __name__ == "__main__":
    main()
