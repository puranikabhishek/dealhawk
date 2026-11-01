#!/usr/bin/env python3
"""
DealHawk 🦅 — free deal sniper that pings your phone on Telegram.

Pulls free deal feeds -> filters by your categories, stores, discount and
community score -> verifies the link is live and not expired/sold out ->
tracks its own price history -> sends a Telegram alert with a Buy button.

Usage:
  python dealhawk.py               # normal run (sends to Telegram)
  python dealhawk.py --dry-run     # print alerts, send nothing, save nothing
  python dealhawk.py --test        # send one test message to Telegram
  python dealhawk.py --chat-id     # print your Telegram chat id (message your bot first)
  python dealhawk.py --feed FILE   # parse a local RSS file instead of the web (testing)
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
PRICES_FILE = DATA / "prices.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 DealHawk/1.0")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

DEAD_MARKERS = ["this deal has expired", "deal expired", "out of stock", "sold out",
                "currently unavailable", "no longer available", "item is unavailable",
                "page not found", "this item is no longer"]
BOT_BLOCKERS = ("amazon.", "walmart.", "costco.", "homedepot.", "target.")  # often 403/503 bots


# ───────────────────────── helpers ─────────────────────────
def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json(p, obj):
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, sort_keys=True))


def money(s):
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def parse_prices(text):
    """Return (price, regular_price, pct_off) best-effort from a deal title/summary."""
    t = text.replace(" ", " ")
    price = reg = pct = None
    m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", t)
    if m:
        price = money(m.group(1))
    m = re.search(r"(?:reg\.?|was|list|msrp|orig\.?)\s*:?\s*\$\s?([\d,]+(?:\.\d{2})?)", t, re.I)
    if m:
        reg = money(m.group(1))
    m = re.search(r"(\d{1,2})\s?%\s?off", t, re.I)
    if m:
        pct = int(m.group(1))
    elif price and reg and reg > price:
        pct = round(100 * (1 - price / reg))
    return price, reg, pct


def thumbs(text):
    m = re.search(r"thumbs?\s*(?:up|score)?\s*:?\s*\+?(-?\d+)", text, re.I)
    return int(m.group(1)) if m else None


def store_of(title, summary):
    m = re.search(r"\b(?:at|@|from)\s+([A-Z&][\w&'.\- ]{1,25}?)(?:\s*[\(\[|,]|$)", title)
    if m:
        return m.group(1).strip()
    m = re.search(r"https?://(?:www\.)?([a-z0-9\-]+)\.(?:com|net|co)", summary)
    return m.group(1) if m else ""


def product_key(title):
    """Normalized key for price history: strip prices, store, noise."""
    t = re.sub(r"\$[\d,.]+|\(.*?\)|\[.*?\]|\b(at|@|from)\s+\w.*$", " ", title, flags=re.I)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    words = [w for w in t.split() if w not in {"free", "shipping", "deal", "new", "w", "with", "and"}]
    return " ".join(words[:10])


def has(word, text):
    """Whole-word, case-insensitive keyword match ("ring" won't hit "string")."""
    return re.search(r"(?<![a-z0-9])" + re.escape(str(word).lower()) + r"(?![a-z0-9])", text) is not None


def extract_links(summary):
    return re.findall(r'href="([^"]+)"', summary or "")


# ───────────────────────── fetch ─────────────────────────
def fetch_feed(url, local=None):
    if local:
        return feedparser.parse(local).entries
    # Reddit rate-limits GitHub's servers; old.reddit + a retry usually gets through
    urls = [url, url.replace("www.reddit.com", "old.reddit.com")] if "reddit.com" in url else [url]
    for attempt, u in enumerate(urls * 2):
        try:
            r = HTTP.get(u, timeout=20)
            r.raise_for_status()
            return feedparser.parse(r.content).entries
        except Exception as e:
            print(f"  ! feed try {attempt + 1} failed {u}: {e}", file=sys.stderr)
            time.sleep(4)
    return []


def to_deal(entry, source):
    title = html.unescape(entry.get("title", "")).strip()
    summary = entry.get("summary", "") or ""
    if entry.get("content"):
        summary += " " + " ".join(c.get("value", "") for c in entry["content"])
    link = entry.get("link", "")
    buy = link
    # Reddit: the "[link]" anchor is the retailer URL
    if "reddit.com" in link:
        for l in extract_links(summary):
            if l.startswith("http") and "reddit.com" not in l and "redd.it" not in l:
                buy = l
                break
    plain = re.sub(r"<[^>]+>", " ", html.unescape(summary))
    price, reg, pct = parse_prices(title + " " + plain)
    return {
        "id": hashlib.sha1((entry.get("id") or link or title).encode()).hexdigest()[:16],
        "title": title, "source": source, "thread": link, "buy": buy,
        "price": price, "reg": reg, "pct": pct,
        "thumbs": thumbs(plain), "store": store_of(title, summary),
        "text": (title + " " + plain).lower(),
    }


# ───────────────────────── filter ─────────────────────────
def categorize(deal, cfg):
    for name, c in cfg["categories"].items():
        if any(has(k, deal["text"]) for k in c["keywords"]):
            return name, c.get("emoji", "🔥")
    return None, None


def watch_hit(deal, cfg):
    for w in cfg.get("watchlist", []):
        if all(has(k, deal["text"]) for k in w["keywords"]):
            if deal["price"] and deal["price"] <= w["max_price"]:
                return w["name"]
    return None


def passes(deal, cfg):
    t = deal["text"]
    if any(has(x, t) for x in cfg.get("exclude_keywords", [])):
        return False, "excluded word"
    stores = cfg.get("trusted_stores") or []
    if stores and not any(has(s, t) for s in stores):
        return False, "untrusted store"
    if deal["pct"] is not None and deal["pct"] < cfg["min_discount_pct"]:
        return False, f"only {deal['pct']}% off"
    if deal["thumbs"] is not None and deal["thumbs"] < cfg["min_thumbs"]:
        return False, f"low thumbs {deal['thumbs']}"
    return True, ""


# ───────────────────────── verify ─────────────────────────
def verify(url):
    """Return 'ok' | 'dead' | 'unverified'. Follows redirects to the final page."""
    """Only drop on hard proof (404/410, or the page TITLE says gone).
    Anything fuzzy (bot blocks, 'unavailable' text buried in page JS) -> unverified,
    so you still get the alert with a ⚠️ and decide yourself."""
    if not url:
        return "dead", url, "no link"
    try:
        r = HTTP.get(url, timeout=20, allow_redirects=True)
        final = r.url
        if r.status_code in (404, 410):
            return "dead", final, f"HTTP {r.status_code}"
        if r.status_code >= 400:
            return "unverified", final, f"HTTP {r.status_code}"
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text[:200_000], re.I | re.S)
        title = (m.group(1) if m else "").lower()
        if any(k in title for k in DEAD_MARKERS):
            return "dead", final, f"title: {title.strip()[:60]}"
        return "ok", final, ""
    except Exception as e:
        return "unverified", url, type(e).__name__


def slickdeals_buy_link(thread_url):
    """Pull the retailer click-through link out of a Slickdeals thread page."""
    try:
        body = HTTP.get(thread_url, timeout=20).text
        m = re.search(r'href="(https://slickdeals\.net/click\?[^"]+)"', body)
        return html.unescape(m.group(1)) if m else thread_url
    except Exception:
        return thread_url


# ───────────────────────── price history ─────────────────────────
def history_note(deal, prices):
    if not deal["price"]:
        return ""
    key = product_key(deal["title"])
    hist = prices.setdefault(key, [])
    note = ""
    if hist:
        low = min(p for _, p in hist)
        if deal["price"] < low:
            note = f"📉 Lowest DealHawk has seen (prev low ${low:,.2f})"
        elif deal["price"] == low:
            note = "🟰 Matches DealHawk's lowest seen"
        else:
            note = f"↗️ Seen cheaper before (${low:,.2f})"
    hist.append([datetime.now(timezone.utc).date().isoformat(), deal["price"]])
    prices[key] = hist[-50:]
    return note


# ───────────────────────── telegram ─────────────────────────
def tg(method, **payload):
    if not TG_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN not set")
    r = HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=20)
    if not r.ok:
        print(f"  ! telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
    return r


def format_alert(d):
    e = html.escape
    lines = [f"{d['emoji']} <b>{e(d['title'])}</b>"]
    price_bits = []
    if d["price"]:
        price_bits.append(f"💵 <b>${d['price']:,.2f}</b>")
    if d["reg"]:
        price_bits.append(f"<s>${d['reg']:,.2f}</s>")
    if d["pct"]:
        price_bits.append(f"({d['pct']}% off)")
    if price_bits:
        lines.append(" ".join(price_bits))
    if d.get("watch"):
        lines.append(f"🎯 Watchlist hit: {e(d['watch'])}")
    if d.get("hist"):
        lines.append(d["hist"])
    meta = [f"📡 {e(d['source'])}"]
    if d["thumbs"] is not None:
        meta.append(f"👍 {d['thumbs']}")
    meta.append("✅ Link verified" if d["status"] == "ok" else "⚠️ Store blocks bots — check price")
    lines.append(" · ".join(meta))
    return "\n".join(lines)


def send(d, dry):
    if not str(d["buy"]).startswith("http"):
        d["buy"] = d["thread"]
    text = format_alert(d)
    buttons = [[{"text": "🛒 Buy", "url": d["buy"]}]]
    if d["thread"] and d["thread"] != d["buy"]:
        buttons[0].append({"text": "💬 Thread", "url": d["thread"]})
    if dry:
        print("\n" + re.sub(r"<[^>]+>", "", text) + f"\n  BUY: {d['buy']}")
        return True
    r = tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML",
           disable_web_page_preview=False, reply_markup={"inline_keyboard": buttons})
    return r.ok


# ───────────────────────── main ─────────────────────────
def in_quiet_hours(cfg):
    start, end = cfg.get("quiet_hours", [23, 7])
    h = datetime.now(ZoneInfo(cfg.get("timezone", "America/Chicago"))).hour
    return h >= start or h < end if start > end else start <= h < end


def run(args):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    seen = load_json(SEEN_FILE, {})
    prices = load_json(PRICES_FILE, {})

    if in_quiet_hours(cfg) and not args.dry_run:
        print("Quiet hours — skipping.")
        return

    feeds = [{"name": "local", "url": None}] if args.feed else cfg["feeds"]
    candidates = []
    for f in feeds:
        entries = fetch_feed(f["url"], local=args.feed)
        time.sleep(0 if args.feed else 2)
        print(f"{f['name']}: {len(entries)} items")
        for e in entries:
            d = to_deal(e, f["name"])
            if d["id"] in seen:
                continue
            d["watch"] = watch_hit(d, cfg)
            cat, emoji = categorize(d, cfg)
            ok, why = passes(d, cfg)
            if d["watch"] or (cat and ok):
                d["category"], d["emoji"] = cat or "watchlist", emoji or "🎯"
                candidates.append(d)
            elif args.verbose:
                print(f"  skip: {d['title'][:70]} — {why or 'no category'}")

    # watchlist first, then biggest discount, then thumbs
    candidates.sort(key=lambda d: (not d["watch"], -(d["pct"] or 0), -(d["thumbs"] or 0)))

    sent = 0
    for d in candidates:
        if sent >= cfg["max_alerts_per_run"]:
            break
        if not args.feed:
            if "slickdeals.net" in d["buy"]:
                d["buy"] = slickdeals_buy_link(d["thread"])
            d["status"], final, why = verify(d["buy"])
            if why:
                print(f"  {d['status']} ({why}): {d['title'][:60]}")
            if d["status"] == "dead":
                seen[d["id"]] = time.time()
                continue
            time.sleep(1)  # be polite
        else:
            d["status"] = "ok"
        d["hist"] = history_note(d, prices)
        if send(d, args.dry_run):
            seen[d["id"]] = time.time()
            sent += 1

    print(f"Sent {sent} alert(s) from {len(candidates)} candidate(s).")
    if not args.dry_run:
        cutoff = time.time() - 30 * 86400
        save_json(SEEN_FILE, {k: v for k, v in seen.items() if v > cutoff})
        save_json(PRICES_FILE, prices)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--chat-id", action="store_true")
    ap.add_argument("--feed", help="local RSS file for testing")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.chat_id:
        for u in tg("getUpdates").json().get("result", []):
            chat = (u.get("message") or {}).get("chat", {})
            print(f"chat_id = {chat.get('id')}  ({chat.get('first_name', '')})")
        return
    if a.test:
        r = tg("sendMessage", chat_id=TG_CHAT, parse_mode="HTML",
               text="🦅 <b>DealHawk is live.</b> You'll get deals here.")
        print("OK" if r.ok else "FAILED")
        return
    run(a)


if __name__ == "__main__":
    main()
