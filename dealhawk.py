#!/usr/bin/env python3
"""
DealHawk 🦅 — free deal sniper that pings your phone on Telegram. Uses zero AI tokens.

Flow: free deal feeds -> filter (categories, stores, discount, score, excludes)
      -> DOUBLE VERIFY (1: deal not expired at source, 2: store page says in stock)
      -> price memory -> compact Telegram alert with Buy button + product preview.

Usage:
  python dealhawk.py               # normal run
  python dealhawk.py --dry-run -v  # print alerts + why things were skipped; send/save nothing
  python dealhawk.py --check URL   # run the stock check on one product page
  python dealhawk.py --test        # send a test message
  python dealhawk.py --feed FILE   # parse a local RSS file (offline testing)
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
PRICES_FILE = DATA / "prices.json"

DAY = 86400
SENT_TTL = 30 * DAY        # don't resend a deal for 30 days
OOS_RECHECK = DAY // 2     # sold-out / unverified deals get re-checked after 12h

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=16))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
FORCE = os.getenv("RECHECK") == "true"  # manual run option: re-check deals held as sold-out/unverified

# ── stock signals ──
IN_STOCK = {"instock", "limitedavailability", "onlineonly", "preorder", "presale", "in stock"}
OUT_STOCK = {"outofstock", "soldout", "discontinued", "out of stock", "oos", "backorder"}
OOS_TEXT = re.compile(  # an element whose whole visible text says it's gone
    r'>\s*(out of stock|sold out|temporarily out of stock|currently unavailable|'
    r'no longer available|not available for delivery|delivery unavailable|unavailable)\s*<'
    r'|value="(out of stock|sold out)"'
    r'|"(?:inventoryStatus|stockStatus|buttonState|availabilityStatus)"\s*:\s*"(out_of_stock|sold_out|oos|unavailable|not_available)"',
    re.I)
BLOCK_TEXT = re.compile(r"robot check|captcha|access denied|are you a human|pardon our interruption|"
                        r"request blocked|enable javascript and cookies", re.I)
SD_EXPIRED = re.compile(r'"isExpired"\s*:\s*true|this deal (?:has|is) expired|deal expired|'
                        r'class="[^"]*\bexpired\b', re.I)

STATUS_LABEL = {"in_stock": "✅ In stock (verified)",
                "unknown": "⚠️ Stock not confirmed — check before buying"}


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
    """(price, regular_price, pct_off), best-effort from deal text."""
    t = text.replace(" ", " ")
    price = reg = pct = None
    if m := re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", t):
        price = money(m.group(1))
    if m := re.search(r"(?:reg\.?|was|list|msrp|orig\.?)\s*:?\s*\$\s?([\d,]+(?:\.\d{2})?)", t, re.I):
        reg = money(m.group(1))
    if m := re.search(r"(\d{1,2})\s?%\s?off", t, re.I):
        pct = int(m.group(1))
    elif price and reg and reg > price:
        pct = round(100 * (1 - price / reg))
    return price, reg, pct


def thumbs(text):
    m = re.search(r"thumbs?\s*(?:up|score)?\s*:?\s*\+?(-?\d+)", text, re.I)
    return int(m.group(1)) if m else None


def store_of(title):
    m = re.search(r"\b(?:at|@)\s+([A-Z&][\w&'.\- ]{1,25}?)(?:\s*[\(\[|,]|$)", title)
    return m.group(1).strip() if m else ""


def product_key(title):
    t = re.sub(r"\$[\d,.]+|\(.*?\)|\[.*?\]|\b(at|@)\s+\w.*$", " ", title, flags=re.I)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return " ".join([w for w in t.split() if w not in {"free", "shipping", "deal", "new", "w", "with", "and"}][:10])


def has(word, text):
    """Whole-word match ('ring' won't hit 'string', 'tv' won't hit 'tvoc')."""
    return re.search(r"(?<![a-z0-9])" + re.escape(str(word).lower()) + r"(?![a-z0-9])", text) is not None


def log(msg):
    print(msg, flush=True)


# ───────────────────────── fetch ─────────────────────────
def fetch_feed(url):
    urls = [url, url.replace("www.reddit.com", "old.reddit.com")] if "reddit.com" in url else [url]
    for i, u in enumerate(urls * 2):
        try:
            r = HTTP.get(u, timeout=20)
            r.raise_for_status()
            return feedparser.parse(r.content).entries
        except Exception as e:
            log(f"  ! feed try {i + 1} {u[:70]}: {e.__class__.__name__}")
            time.sleep(4)
    return []


def all_feeds(cfg):
    feeds = list(cfg.get("feeds", []))
    for q in cfg.get("slickdeals_searches", []):
        feeds.append({"name": f"Slickdeals · {q}",
                      "url": f"https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1&q={quote_plus(q)}"})
    reddit = [f for f in feeds if "reddit.com" in f["url"]]
    other = [f for f in feeds if f not in reddit]
    out = []
    with ThreadPoolExecutor(6) as ex:  # non-Reddit feeds in parallel
        for f, entries in zip(other, ex.map(lambda f: fetch_feed(f["url"]), other)):
            out.append((f["name"], entries))
    for f in reddit:  # Reddit one at a time (rate limits)
        out.append((f["name"], fetch_feed(f["url"])))
        time.sleep(2)
    return out


def to_deal(entry, source):
    title = html.unescape(entry.get("title", "")).strip()
    summary = entry.get("summary", "") or ""
    if entry.get("content"):
        summary += " " + " ".join(c.get("value", "") for c in entry["content"])
    link = entry.get("link", "")
    buy = link
    if "reddit.com" in link:
        for l in re.findall(r'href="([^"]+)"', summary):
            if l.startswith("http") and "reddit.com" not in l and "redd.it" not in l:
                buy = html.unescape(l)
                break
    plain = re.sub(r"<[^>]+>", " ", html.unescape(summary))
    price, reg, pct = parse_prices(title + " " + plain)
    return {"id": hashlib.sha1((entry.get("id") or link or title).encode()).hexdigest()[:16],
            "title": title, "source": source, "thread": link, "buy": buy,
            "price": price, "reg": reg, "pct": pct, "thumbs": thumbs(plain),
            "store": store_of(title), "text": (title + " " + plain).lower()}


# ───────────────────────── filter ─────────────────────────
def categorize(d, cfg):
    for name, c in cfg["categories"].items():
        if any(has(k, d["text"]) for k in c["keywords"]):
            return name, c.get("emoji", "🔥")
    return None, None


def watch_hit(d, cfg):
    for w in cfg.get("watchlist") or []:
        if all(has(k, d["text"]) for k in w["keywords"]) and d["price"] and d["price"] <= w["max_price"]:
            return w["name"]
    return None


def passes(d, cfg):
    t = d["title"].lower()  # excludes check the TITLE so a passing mention in comments doesn't kill a deal
    if hit := next((x for x in cfg.get("exclude_keywords", []) if has(x, t)), None):
        return False, f"excluded '{hit}'"
    stores = cfg.get("trusted_stores") or []
    if stores and not any(has(s, d["text"]) for s in stores):
        return False, "untrusted store"
    if d["pct"] is not None and d["pct"] < cfg["min_discount_pct"]:
        return False, f"only {d['pct']}% off"
    if d["thumbs"] is not None and d["thumbs"] < cfg["min_thumbs"]:
        return False, f"thumbs {d['thumbs']}"
    return True, ""


# ───────────────────────── double verification ─────────────────────────
def availability(body):
    """Read the store's own stock signal. Returns in_stock | oos | None."""
    # 1) schema.org JSON-LD / microdata — what Google Shopping reads, most reliable
    found = [a.lower() for a in re.findall(
        r'"?availability"?\s*[:=]\s*"(?:https?://schema\.org/)?([A-Za-z ]+)"', body)]
    found += [a.lower() for a in re.findall(
        r'<meta[^>]+(?:product:availability|og:availability)[^>]+content="([^"]+)"', body)]
    if found:
        if any(a.replace(" ", "") in OUT_STOCK or a in OUT_STOCK for a in found[:3]):
            return "oos"
        if any(a.replace(" ", "") in IN_STOCK or a in IN_STOCK for a in found[:3]):
            return "in_stock"
    # 2) Amazon's availability box
    if m := re.search(r'id="availability"[^>]*>(.{0,600}?)</div>', body, re.S):
        txt = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).lower()
        if "in stock" in txt or "only" in txt and "left" in txt:
            return "in_stock"
        if "unavailable" in txt or "out of stock" in txt:
            return "oos"
    # 3) a button/label whose entire text is "Out of Stock" / "Sold Out" etc.
    if OOS_TEXT.search(body):
        return "oos"
    return None


def check_store(url):
    """Check 2: the store page itself. -> (in_stock|oos|dead|unknown, reason)"""
    try:
        r = HTTP.get(url, timeout=20, allow_redirects=True)
    except Exception as e:
        return "unknown", e.__class__.__name__
    if r.status_code in (404, 410):
        return "dead", f"HTTP {r.status_code}"
    body = r.text[:1_500_000]
    host = urlparse(r.url).netloc.replace("www.", "")
    if r.status_code >= 400 or BLOCK_TEXT.search(body[:20000]):
        return "unknown", f"{host} blocked bot (HTTP {r.status_code})"
    a = availability(body)
    if a:
        return a, f"{host} says " + ("in stock" if a == "in_stock" else "OUT OF STOCK")
    return "unknown", f"{host}: no stock data on page"


def direct_url(click):
    """Slickdeals click links carry the real store URL in u2= — use it, skip the bot-blocking redirect."""
    q = parse_qs(urlparse(click).query)
    for k in ("u2", "u", "url"):
        if q.get(k) and q[k][0].startswith("http"):
            return q[k][0]
    return click


def resolve_and_verify(d):
    """Check 1 (source still live) + Check 2 (store says in stock)."""
    if "slickdeals.net" in d["thread"]:
        try:
            page = HTTP.get(d["thread"], timeout=20).text
            if SD_EXPIRED.search(page[:300000]):
                return "dead", "Slickdeals marked expired"
            if m := re.search(r'href="(https://slickdeals\.net/click\?[^"]+)"', page):
                d["buy"] = direct_url(html.unescape(m.group(1)))
        except Exception:
            pass
    if not str(d["buy"]).startswith("http"):
        d["buy"] = d["thread"]
    status, why = check_store(d["buy"])
    if status == "in_stock":  # re-check once to dodge flaky/cached pages
        time.sleep(1.5)
        status2, why2 = check_store(d["buy"])
        if status2 != "in_stock":
            return status2, f"2nd check: {why2}"
    return status, why


# ───────────────────────── price memory ─────────────────────────
def history_note(d, prices):
    if not d["price"]:
        return ""
    key = product_key(d["title"])
    hist = prices.setdefault(key, [])
    note = ""
    if hist:
        low = min(p for _, p in hist)
        note = ("📉 Lowest DealHawk has seen" if d["price"] < low else
                "🟰 Ties lowest seen" if d["price"] == low else f"↗️ Was ${low:,.0f} before")
    hist.append([datetime.now(timezone.utc).date().isoformat(), d["price"]])
    prices[key] = hist[-50:]
    return note


# ───────────────────────── telegram ─────────────────────────
def tg(method, **payload):
    if not TG_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN not set")
    r = HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=20)
    if not r.ok:
        log(f"  ! telegram {r.status_code}: {r.text[:200]}")
    return r


def format_alert(d):
    """Compact: everything you need to decide in 3 lines."""
    e = html.escape
    price = f"<b>${d['price']:,.2f}</b>" if d["price"] else ""
    if d["reg"]:
        price += f" <s>${d['reg']:,.0f}</s>"
    if d["pct"]:
        price += f" −{d['pct']}%"
    l1 = f"{d['emoji']} <b>{e(d['title'][:150])}</b>"
    l2 = " · ".join(x for x in [price, e(d["store"]) if d["store"] else "",
                                f"👍{d['thumbs']}" if d["thumbs"] is not None else ""] if x)
    l3 = " · ".join(x for x in [STATUS_LABEL.get(d["status"], ""), d.get("hist", ""),
                                f"🎯 {e(d['watch'])}" if d.get("watch") else ""] if x)
    return "\n".join(x for x in [l1, l2, l3] if x)


def send(d, dry):
    text = format_alert(d)
    buttons = [{"text": "🛒 Buy", "url": d["buy"]}]
    if d["thread"].startswith("http") and d["thread"] != d["buy"]:
        buttons.append({"text": "💬 Details", "url": d["thread"]})
    if dry:
        log("\n" + html.unescape(re.sub(r"<[^>]+>", "", text)) + f"\n  BUY: {d['buy']}")
        return True
    return tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML",
              link_preview_options={"url": d["buy"], "prefer_small_media": True},
              reply_markup={"inline_keyboard": [buttons]}).ok


# ───────────────────────── main ─────────────────────────
def in_quiet_hours(cfg):
    start, end = cfg.get("quiet_hours", [23, 7])
    h = datetime.now(ZoneInfo(cfg.get("timezone", "America/Chicago"))).hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def load_seen():
    """seen.json maps deal id -> 'skip until' timestamp (v2). Migrates v1 (sent-at)."""
    s = load_json(SEEN_FILE, {})
    if s.get("_v") != 2:
        s = {k: v + SENT_TTL for k, v in s.items() if isinstance(v, (int, float))}
        s["_v"] = 2
    return s


def run(args):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    if in_quiet_hours(cfg) and not args.dry_run:
        return log("Quiet hours — skipping.")
    now = time.time()
    seen, prices = load_seen(), load_json(PRICES_FILE, {})
    strict = cfg.get("stock_policy", "strict") == "strict"

    sources = ([("local", feedparser.parse(args.feed).entries)] if args.feed else all_feeds(cfg))
    cands, ids = [], set()
    for name, entries in sources:
        log(f"{name}: {len(entries)} items")
        for e in entries:
            d = to_deal(e, name)
            hold = seen.get(d["id"], 0)
            if d["id"] in ids or hold > now and not (FORCE and hold < now + OOS_RECHECK + 60):
                continue
            ids.add(d["id"])
            d["watch"] = watch_hit(d, cfg)
            cat, emoji = categorize(d, cfg)
            ok, why = passes(d, cfg)
            if ok and (d["watch"] or cat):
                d["emoji"] = emoji or "🎯"
                cands.append(d)
            elif args.verbose:
                log(f"  skip: {d['title'][:70]} — {why or 'no category'}")

    cands.sort(key=lambda d: (not d["watch"], -(d["pct"] or 0), -(d["thumbs"] or 0)))
    cands = cands[: cfg["max_alerts_per_run"] * 3]  # verify only what could be sent

    if args.feed:
        results = [("in_stock", "offline test")] * len(cands)
    else:
        with ThreadPoolExecutor(8) as ex:  # parallel double-check
            results = list(ex.map(resolve_and_verify, cands))

    sent, tally = 0, {}
    for d, (status, why) in zip(cands, results):
        tally[status] = tally.get(status, 0) + 1
        log(f"  [{status}] {why} — {d['title'][:60]}")
        d["status"] = status
        if status in ("oos", "dead") or (strict and status != "in_stock"):
            seen[d["id"]] = now + OOS_RECHECK  # try again in 12h (could restock)
            continue
        if sent >= cfg["max_alerts_per_run"]:
            break
        d["hist"] = history_note(d, prices)
        if send(d, args.dry_run):
            seen[d["id"]] = now + SENT_TTL
            sent += 1

    log(f"Checked {len(cands)}: {tally}. Sent {sent}. Policy: {'strict' if strict else 'balanced'}.")
    if not args.dry_run:
        save_json(SEEN_FILE, {k: v for k, v in seen.items() if k == "_v" or v > now})
        save_json(PRICES_FILE, prices)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--check", metavar="URL")
    ap.add_argument("--feed")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.check:
        return log(str(check_store(a.check)))
    if a.test:
        return log("OK" if tg("sendMessage", chat_id=TG_CHAT, parse_mode="HTML",
                              text="🦅 <b>DealHawk is live.</b>").ok else "FAILED")
    run(a)


if __name__ == "__main__":
    main()
