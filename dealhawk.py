#!/usr/bin/env python3
"""
DealHawk 🦅 — free deal sniper for Telegram. $0, zero AI tokens.

Every run:
  1. Reads your Telegram commands (/add, /block, /watch, /off …) and updates settings.
  2. Pulls fresh, community/editor-curated deal feeds (Slickdeals, DealNews).
  3. Filters by your categories, blocked words/stores, price.
  4. Verifies: source not expired + store page stock check where the store allows it.
  5. Sends 🎯 wish-list deals as their own photo cards (Buy button, marked ❌ EXPIRED when they die)
     + ONE grouped digest (by category) for everything else.

CLI: --dry-run  --test  --demo [FEED_URL]  --check URL  --feed FILE  -v
"""
import argparse
import calendar
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
SEEN_FILE, PRICES_FILE, SETTINGS_FILE = DATA / "seen.json", DATA / "prices.json", DATA / "settings.json"
CARDS_FILE = DATA / "cards.json"  # wish-list cards we sent, so we can mark them EXPIRED later

DAY = 86400
SENT_TTL, OOS_RECHECK = 30 * DAY, DAY // 2
TG_LIMIT = 3900

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=16))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
FORCE = os.getenv("RECHECK") == "true"

STORE_DOMAINS = r"amazon\.|walmart\.|bestbuy\.|homedepot\.|lowes\.|target\.|costco\.|samsclub\.|newegg\.|" \
                r"bhphotovideo\.|woot\.|wayfair\.|ikea\.|overstock\.|dell\.|lenovo\.|ebay\.|acehardware\.|" \
                r"microcenter\.|adorama\.|kohls\.|macys\.|apple\.|samsung\.|dyson\.|staples\.|zoro\.|northerntool\."
IN_STOCK = {"instock", "limitedavailability", "onlineonly", "preorder", "presale", "in stock"}
OUT_STOCK = {"outofstock", "soldout", "discontinued", "out of stock", "oos", "backorder"}
OOS_TEXT = re.compile(
    r'>\s*(out of stock|sold out|temporarily out of stock|currently unavailable|no longer available|'
    r'not available for delivery|delivery unavailable|unavailable)\s*<|value="(out of stock|sold out)"|'
    r'"(?:inventoryStatus|stockStatus|buttonState|availabilityStatus)"\s*:\s*"(out_of_stock|sold_out|oos|unavailable|not_available)"',
    re.I)
BLOCK_TEXT = re.compile(r"robot check|captcha|access denied|are you a human|pardon our interruption|request blocked", re.I)
PAGE_EXPIRED = re.compile(r'"isExpired"\s*:\s*true|this (?:deal|offer|sale) (?:has|is) (?:expired|ended|over)|'
                          r'deal (?:has )?expired|no longer available|sold out', re.I)
TITLE_NOISE = re.compile(r"\s*(?:\+\s*)?(?:free\s+(?:s&h|shipping|store pickup)[^,]*|w/\s*prime|"
                         r"\b(?:at|@)\s+[A-Z][\w&'. -]{1,25}$|\bfor \$[\d,.]+|\$[\d,.]+(?:\s*-\s*\$[\d,.]+)?)", re.I)


# ───────────────────────── small helpers ─────────────────────────
def log(m):
    print(m, flush=True)


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


def has(word, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(str(word).lower()) + r"(?![a-z0-9])", text) is not None


def parse_prices(t):
    t = t.replace(" ", " ")
    price = reg = pct = None
    if m := re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", t):
        price = money(m.group(1))
    if m := re.search(r"(?:reg\.?|was|list(?: price)?|msrp|orig\.?|originally|normally)\s*:?\s*\$\s?([\d,]+(?:\.\d{2})?)", t, re.I):
        reg = money(m.group(1))
    if m := re.search(r"(\d{1,2})\s?%\s?off", t, re.I):
        pct = int(m.group(1))
    elif price and reg and reg > price:
        pct = round(100 * (1 - price / reg))
    return price, reg, pct


def store_of(title, plain):
    if m := re.search(r"\b(?:at|@)\s+([A-Z][\w&'.\- ]{1,25}?)(?:\s*[\(\[|,]|$)", title):
        return m.group(1).strip()
    if m := re.match(r"\s*([A-Z][\w&'.!\- ]{1,25}?)\s+(?:\[[\w.]+\]\s+)?has\b", plain):  # "Woot! has …"
        return m.group(1).strip()
    return ""


def clean_title(t):
    t = re.sub(r"^\s*\[[^\]]{1,30}\]\s*[-:]?\s*", "", t)  # leading [Costco Members] tags
    t = re.sub(r"\((?:reg|was|list|orig|msrp)[^)]*\)|\(\s*\d+\s*%\s*off\s*\)", " ", t, flags=re.I)
    t = re.sub(r"\(\s*\)", " ", TITLE_NOISE.sub(" ", t))
    return re.sub(r"\s{2,}", " ", t).strip(" -–,:+")


def product_key(title):
    t = re.sub(r"[^a-z0-9 ]", " ", clean_title(title).lower())
    return " ".join([w for w in t.split() if w not in {"free", "shipping", "deal", "new", "w", "with", "and"}][:10])


# ───────────────────────── settings (Telegram commands) ─────────────────────────
HELP = """🦅 <b>DealHawk commands</b>
/add grill, dyson — want these (adds to 🎯 Your picks)
/remove grill — stop wanting
/block cushion — never show deals with this word
/unblock cushion
/watch dyson v15 400 — alert if under $400
/unwatch dyson
/off tools — turn a category off
/on clothing — turn one on
/list — see everything
<i>Commands apply on the next run (within 30 min).</i>"""


def apply_settings(cfg, st):
    cats = cfg["categories"]
    for c, on in st.get("toggles", {}).items():
        if c in cats:
            cats[c]["enabled"] = on
    if st.get("custom"):
        cats = {"custom": {"title": "Your picks", "emoji": "🎯", "keywords": st["custom"]}, **cats}
    cfg["categories"] = cats
    cfg["exclude_keywords"] = [w for w in cfg.get("exclude_keywords", []) if w not in st.get("unblocked", [])] \
        + st.get("blocked", [])
    cfg["watchlist"] = (cfg.get("watchlist") or []) + st.get("watch", [])
    return cfg


def find_cat(name, cfg):
    n = name.strip().lower().replace(" ", "_").replace("&", "")
    for c, v in cfg["categories"].items():
        if c == n or c.startswith(n) or v.get("title", "").lower().startswith(name.strip().lower()):
            return c
    return None


def words(arg):
    return [w.strip().lower() for w in arg.split(",") if w.strip()]


def command(text, cfg, st):
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.lower().split("@")[0]
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add" and arg:
        st.setdefault("custom", [])
        st["custom"] += [w for w in words(arg) if w not in st["custom"]]
        return f"🎯 Added: {html.escape(', '.join(words(arg)))}"
    if cmd == "/remove" and arg:
        st["custom"] = [w for w in st.get("custom", []) if w not in words(arg)]
        return f"Removed: {html.escape(', '.join(words(arg)))}"
    if cmd == "/block" and arg:
        st.setdefault("blocked", [])
        st["blocked"] += [w for w in words(arg) if w not in st["blocked"]]
        st["unblocked"] = [w for w in st.get("unblocked", []) if w not in words(arg)]
        return f"🚫 Blocked: {html.escape(', '.join(words(arg)))}"
    if cmd == "/unblock" and arg:
        st["blocked"] = [w for w in st.get("blocked", []) if w not in words(arg)]
        st.setdefault("unblocked", []).extend(words(arg))
        return f"✅ Unblocked: {html.escape(', '.join(words(arg)))}"
    if cmd == "/watch" and arg:
        parts = arg.replace("$", "").split()
        if len(parts) < 2 or not re.fullmatch(r"\d+(\.\d+)?", parts[-1]):
            return "Format: /watch dyson v15 400"
        kw, price = [p.lower() for p in parts[:-1]], float(parts[-1])
        st.setdefault("watch", []).append({"name": " ".join(kw), "keywords": kw, "max_price": price})
        return f"🎯 Watching <b>{html.escape(' '.join(kw))}</b> under ${price:,.0f}"
    if cmd == "/unwatch" and arg:
        before = len(st.get("watch", []))
        st["watch"] = [w for w in st.get("watch", []) if arg.lower() not in w["name"].lower()]
        return f"Removed {before - len(st['watch'])} watch(es)."
    if cmd in ("/on", "/off") and arg:
        c = find_cat(arg, cfg)
        if not c:
            return f"No category '{html.escape(arg)}'. Try /list"
        st.setdefault("toggles", {})[c] = cmd == "/on"
        return f"{'✅ On' if cmd == '/on' else '⏸️ Off'}: {cfg['categories'][c].get('title', c)}"
    if cmd == "/list":
        live = apply_settings(json.loads(json.dumps(cfg)), st)
        cats = "\n".join(f"{'✅' if v.get('enabled', True) else '⏸️'} {v.get('emoji', '')} {v.get('title', c)}"
                         f" — <code>{c}</code>" for c, v in live["categories"].items())
        w = "\n".join(f"• {html.escape(x['name'])} ≤ ${x['max_price']:,.0f}" for x in live["watchlist"]) or "none"
        b = ", ".join(st.get("blocked", [])) or "none"
        return f"<b>Categories</b>\n{cats}\n\n<b>Watchlist</b>\n{w}\n\n<b>Your blocks</b>: {html.escape(b)}"
    return "Didn't get that. /help"


def handle_commands(cfg, st):
    if not TG_TOKEN:
        return
    payload = {"timeout": 0, "allowed_updates": ["message"]}
    if st.get("tg_offset"):
        payload["offset"] = st["tg_offset"] + 1
    try:
        updates = tg("getUpdates", **payload).json().get("result", [])
    except Exception:
        return
    for u in updates:
        st["tg_offset"] = u["update_id"]
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TG_CHAT) or not m.get("text", "").startswith("/"):
            continue  # only you can control the bot
        reply = command(m["text"], cfg, st)
        log(f"  cmd {m['text'][:40]!r} -> {reply[:60]!r}")
        tg("sendMessage", chat_id=TG_CHAT, text=reply, parse_mode="HTML")
    if updates:  # confirm so Telegram drops them
        tg("getUpdates", offset=st["tg_offset"] + 1, timeout=0)


# ───────────────────────── feeds ─────────────────────────
def fetch_feed(url):
    urls = [url, url.replace("www.reddit.com", "old.reddit.com")] if "reddit.com" in url else [url]
    for i, u in enumerate(urls * 2):
        try:
            r = HTTP.get(u, timeout=20)
            r.raise_for_status()
            return feedparser.parse(r.content).entries
        except Exception as e:
            log(f"  ! feed try {i + 1} {u[:70]}: {e.__class__.__name__}")
            time.sleep(3)
    return []


def all_feeds(cfg):
    feeds = cfg.get("feeds", [])
    reddit = [f for f in feeds if "reddit.com" in f["url"]]
    other = [f for f in feeds if f not in reddit]
    with ThreadPoolExecutor(6) as ex:  # everything else in parallel
        out = {f["name"]: es for f, es in zip(other, ex.map(lambda f: fetch_feed(f["url"]), other))}
    for f in reddit:  # Reddit rate-limits hard: one at a time, spaced out
        out[f["name"]] = fetch_feed(f["url"])
        time.sleep(4)
    return [(f["name"], out[f["name"]]) for f in feeds]


def direct_url(u):
    q = parse_qs(urlparse(u).query)
    for k in ("u2", "u", "url"):
        if q.get(k) and q[k][0].startswith("http"):
            return q[k][0]
    return u


def domain_store(dom):
    """bestbuy -> Best Buy, homedepot -> Home Depot, else Capitalized."""
    names = {"bestbuy": "Best Buy", "homedepot": "Home Depot", "lowes": "Lowe's", "samsclub": "Sam's Club", "ebay": "eBay",
             "bhphotovideo": "B&H", "acehardware": "Ace Hardware", "microcenter": "Micro Center"}
    return names.get(dom.lower(), dom.capitalize())


def click_store(click):
    """Slickdeals click links carry the store name (trd=Amazon)."""
    t = (parse_qs(urlparse(click).query).get("trd") or [""])[0] if click else ""
    if m := re.match(r"https?\W+(?:www\W+)?([a-z0-9-]+)\W+(?:com|net|org)\b", t, re.I):  # trd is sometimes the store URL
        return domain_store(m.group(1))
    return t if 1 < len(t) <= 30 and not re.search(r"\d|deal|image|trial|coupon|here", t, re.I) else ""


def to_deal(entry, source):
    title = html.unescape(entry.get("title", "")).strip()
    summary = (entry.get("summary") or "") + " ".join(c.get("value", "") for c in entry.get("content", []))
    link = entry.get("link", "")
    buy = link
    hrefs = [html.unescape(x) for x in re.findall(r'href="([^"]+)"', summary)]
    for l in hrefs:
        l = direct_url(l) if "/click" in l else l
        if re.search(STORE_DOMAINS, l):
            buy = l
            break
    # Slickdeals' own "go to store" link: bots get 403, but in your phone's browser it lands on the store page.
    click = next((l for l in hrefs if "slickdeals.net/click" in l), None)
    if buy == link and click:
        buy = click
    img = next(iter(re.findall(r'<img[^>]+src="([^"]+)"', summary)), None) or (entry.get("media_content") or [{}])[0].get("url")
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(summary)))
    price, reg, pct = parse_prices(title + " " + plain[:400])
    pub = entry.get("published_parsed") or entry.get("updated_parsed")
    th = re.search(r"thumbs?\s*(?:up|score)?\s*:?\s*\+?(-?\d+)", plain, re.I)
    return {"id": hashlib.sha1((entry.get("id") or link or title).encode()).hexdigest()[:16],
            "title": title, "source": source, "thread": link, "buy": buy,
            "price": price, "reg": reg, "pct": pct, "thumbs": int(th.group(1)) if th else None,
            "store": entry.get("dealnews_retailer") or store_of(title, plain) or click_store(click)
            or (domain_store(m.group(1)) if (m := re.search(r"\[([a-z0-9-]+)\.com\]", plain[:300])) else ""),
            "image": html.unescape(img) if img else None, "pub": calendar.timegm(pub) if pub else None,
            "tl": title.lower(), "text": (title + " " + plain[:600]).lower(),
            "tags": " ".join(t.get("term", "") for t in entry.get("tags", [])).lower(),
            "age_h": (time.time() - calendar.timegm(pub)) / 3600 if pub else None}


# ───────────────────────── filtering ─────────────────────────
SAFE_PHRASES = re.compile(r"\b(dishwasher|oven|microwave|freezer|dryer)[- ]safe\b|\bfor (?:the )?(?:dishwasher|oven|microwave)\b")


# Phrases that contain a blocked word but aren't what the block is for:
# "robot vacuum for pet hair" isn't a pet product; "patio set with cushions" isn't a lone cushion.
BLOCK_SAFE = re.compile(r"\bpet[- ]hair\b|\b(?:for|with) (?:pets?|dogs?|cats?)\b(?=.*\bvac)|\banimal\b"
                        r"|(?:\bwith|\bw/)\s*(?:\w+ )?(?:cushions?|covers?)\b|\b(?:charging|wireless) case\b")


# AI note-taker titles often say "w/ magnetic case" or "+ 1-yr subscription" — those aren't phone cases or subs.
NOTE_TAKER = re.compile(r"\b(?:plaud|notepin|ticnote|soundcore work|(?:ai|voice) recorder|note ?taker)\b")
NOTE_TAKER_SAFE = re.compile(r"\b(?:magnetic |charging |carrying |protective )?case\b|\bsubscription\b|\bmembership\b")


def block_text(tl):
    tl = BLOCK_SAFE.sub(" ", tl)
    return NOTE_TAKER_SAFE.sub(" ", tl) if NOTE_TAKER.search(tl) else tl


def matches(c, tl):
    return any(has(k, tl) for k in c.get("keywords", [])) or any(re.search(p, tl) for p in c.get("patterns", []))


def categorize(d, cfg):
    cats = cfg["categories"]
    if (f := d.get("forced")) in cats:  # freebies/giveaways feeds
        return f if cats[f].get("enabled", True) else None
    tl = SAFE_PHRASES.sub(" ", d["tl"])  # "dishwasher safe" isn't an appliance
    if inst := next((n for n, c in cats.items() if c.get("instant") and c.get("enabled", True) and matches(c, tl)), None):
        return inst
    interest = next((n for n, c in cats.items() if c.get("enabled", True) and not c.get("feed_only")
                     and not c.get("instant") and n != "ultra" and matches(c, tl)), None)
    u = cats.get("ultra", {})
    if u and u.get("enabled", True):
        deep = interest and not cats[interest].get("relaxed") and (d["pct"] or 0) >= u.get("min_pct", 65)
        if matches(u, tl) or deep:
            return "ultra"
    return interest


def watch_hit(d, cfg):
    for w in cfg.get("watchlist") or []:
        if all(has(k, d["tl"]) for k in w["keywords"]) and d["price"] and d["price"] <= w["max_price"]:
            return w["name"]
    return None


def wish_kw(d, cfg):
    """The wish-list word this deal matched (your /add picks first), or None."""
    tl = SAFE_PHRASES.sub(" ", d["tl"])
    for name in ("custom", "wishlist"):
        c = cfg["categories"].get(name)
        if c and c.get("enabled", True):
            if k := next((k for k in sorted(c.get("keywords", []), key=len, reverse=True) if has(k, tl)), None):
                return k
    return None


def prize_ok(d, cfg, c):
    """Giveaways: only prizes you'd want — your categories' items, or big cash / store gift cards."""
    if any(re.search(p, d["tl"]) for p in c.get("prize_patterns", [])):
        return True
    return any(matches(v, d["tl"]) for n, v in cfg["categories"].items()
               if v.get("enabled", True) and not v.get("relaxed") and not v.get("instant") and n != "ultra")


def passes(d, cfg, relaxed=False):
    c = cfg["categories"].get(d.get("cat"), {})
    if c.get("prize_patterns") is not None:
        if not prize_ok(d, cfg, c):
            return False, "boring prize"
    elif hit := next((x for x in cfg.get("exclude_keywords", []) if has(x, block_text(d["tl"]))), None):
        return False, f"blocked '{hit}'"
    if hit := next((s for s in cfg.get("blocked_stores", []) if has(s, d["text"])), None):
        return False, f"store '{hit}'"
    if re.search(r"expired|out of stock|\boos\b|sold out|dead deal", d["tags"] + " " + d["tl"]):
        return False, "flagged expired"
    mx = d.get("feed_max_age") or cfg.get("max_age_hours")
    if mx and d["age_h"] and d["age_h"] > mx:
        return False, "too old (likely expired)"
    cat_max = cfg["categories"].get(d.get("cat"), {}).get("max_age_hours")
    if cat_max and d["age_h"] and d["age_h"] > cat_max:
        return False, "stale price mistake"
    if relaxed:
        return True, ""
    if d["price"] is not None and d["price"] < cfg.get("min_price", 0):
        return False, "under min price"
    if d["pct"] is not None and d["pct"] < cfg.get("min_discount_pct", 0):
        return False, "small discount"
    min_th = cfg.get("min_thumbs", -999)
    wl = cfg.get("wishlist_alerts") or {}
    if (d.get("wish") or d.get("watch")) and (d["age_h"] if d["age_h"] is not None else 99) <= wl.get("fresh_hours", 3):
        # fresh wish-list post: don't wait for the crowd to vote it up (older ones must have earned their votes)
        min_th = min(min_th, (cfg.get("wishlist_alerts") or {}).get("min_thumbs", min_th))
    if d["thumbs"] is not None and d["thumbs"] < min_th:
        return False, "low votes"
    return True, ""


# ───────────────────────── verification ─────────────────────────
def availability(body):
    found = [a.lower() for a in re.findall(r'"?availability"?\s*[:=]\s*"(?:https?://schema\.org/)?([A-Za-z ]+)"', body)]
    found += [a.lower() for a in re.findall(r'<meta[^>]+(?:product:availability|og:availability)[^>]+content="([^"]+)"', body)]
    if found:
        top = [a.replace(" ", "") for a in found[:3]] + found[:3]
        if any(a in OUT_STOCK for a in top):
            return "oos"
        if any(a in IN_STOCK for a in top):
            return "in_stock"
    if m := re.search(r'id="availability"[^>]*>(.{0,600}?)</div>', body, re.S):
        txt = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).lower()
        if "in stock" in txt or ("only" in txt and "left" in txt):
            return "in_stock"
        if "unavailable" in txt or "out of stock" in txt:
            return "oos"
    return "oos" if OOS_TEXT.search(body) else None


def check_store(url):
    try:
        r = HTTP.get(url, timeout=20, allow_redirects=True)
    except Exception as e:
        return "unknown", e.__class__.__name__
    host = urlparse(r.url).netloc.replace("www.", "")
    if r.status_code in (404, 410):
        return "dead", f"{host} HTTP {r.status_code}"
    body = r.text[:1_500_000]
    if r.status_code >= 400 or BLOCK_TEXT.search(body[:20000]):
        return "unknown", f"{host} blocks bots ({r.status_code})"
    if a := availability(body):
        return a, f"{host} says {'in stock' if a == 'in_stock' else 'OUT OF STOCK'}"
    return "unknown", f"{host}: no stock data"


def source_check(thread):
    """What the deal site itself says about the deal: ('dead' | 'live' | None, page html)."""
    try:
        r = HTTP.get(thread, timeout=20)
    except Exception:
        return None, ""
    if r.status_code in (404, 410):
        return "dead", ""
    if r.status_code != 200:
        return None, ""
    page = r.text[:1_500_000]
    if "slickdeals.net" in thread:  # every thread page states isExpiredDeal Yes/No
        tid = re.search(r"/f/(\d+)", thread)
        m = (tid and re.search(r'"isExpiredDeal"\s*:\s*"(Yes|No)"\s*,\s*"threadId"\s*:\s*' + tid.group(1), page)) \
            or re.search(r'"isExpiredDeal"\s*:\s*"(Yes|No)"', page)
        return ({"Yes": "dead", "No": "live"}[m.group(1)] if m else None), page
    if "dealnews.com" in thread:
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", " ", page[:400000], flags=re.S))
        return ("dead" if PAGE_EXPIRED.search(text[:20000]) else "live"), page
    return None, page


def resolve_click(u):
    """DealNews click pages hop through a JS redirect to the store. Follow it to the real product page."""
    try:
        m = re.search(r"location\.replace\('([^']+)'\)", HTTP.get(u, timeout=15).text[:3000])
        if not m:
            return None
        r = HTTP.get(m.group(1), timeout=15, allow_redirects=True)
        for final in [r.url] + [h.headers.get("location", "") for h in reversed(r.history)]:
            if re.search(STORE_DOMAINS, urlparse(final).netloc + ".") and len(final) < 1500 \
                    and not re.search(r"/blocked|captcha|/errors?/", final, re.I):
                return final
    except Exception:
        pass
    return None


def resolve_and_verify(d):
    site = "Slickdeals" if "slickdeals.net" in d["thread"] else "DealNews" if "dealnews.com" in d["thread"] else "source"
    # Check 1: the deal site's own page — is the deal still marked active?
    src, page = source_check(d["thread"]) if d["thread"].startswith("http") else (None, "")
    if src == "dead":
        return "dead", f"{site}: marked expired"
    if page:
        if m := re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page[:200000]):
            d["image_hd"] = html.unescape(m.group(1))
    store_url = d["buy"] if re.search(STORE_DOMAINS, urlparse(d["buy"]).netloc + ".") else None
    if site == "DealNews" and page:
        for l in re.findall(r'href="([^"]+)"', page):
            l = html.unescape(l)
            if re.search(STORE_DOMAINS, l) or "/lw/click" in l:
                d["buy"] = ("https://www.dealnews.com" + l) if l.startswith("/") else l
                store_url = d["buy"] if "/lw/click" not in l else resolve_click(d["buy"])
                break
    # Check 2: the store page (where the store lets bots look), twice if it says in stock
    if store_url:
        status, why = check_store(store_url)
        if status == "in_stock":
            time.sleep(1.5)
            status, why = check_store(store_url)
            why = why if status == "in_stock" else f"2nd check: {why}"
        if status == "in_stock":
            d["buy"] = store_url  # proven product page: link straight to it
        if status != "unknown":
            return status, why
    if src == "live":
        return "live", f"{site}: still active"
    return "unknown", "couldn't verify"


# ───────────────────────── price memory ─────────────────────────
def history_note(d, prices):
    if not d["price"]:
        return ""
    key = product_key(d["title"])
    hist = prices.setdefault(key, [])
    note = " 📉" if hist and d["price"] < min(p for _, p in hist) else ""
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


def short(t, n=46):
    t = clean_title(t) or t
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def deal_line(d):
    e = html.escape
    link = f'<a href="{e(d["buy"], quote=True)}">{e(short(d["title"]))}</a>'
    if d["cat"] in ("freebies", "giveaways") or not d["price"]:
        return f"• {link}"
    pct = f" <i>−{d['pct']}%</i>" if d["pct"] else ""
    return f"<b>${d['price']:,.0f}</b>{pct} {link}"


def send_text(text, dry, buttons=None, preview=None, silent=False):
    if dry:
        log("\n" + html.unescape(re.sub(r"<[^>]+>", "", text)) + (f"\n  [BUY] {buttons[0]['url']}" if buttons else ""))
        return True
    p = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_notification": silent,
         "link_preview_options": {"url": preview, "prefer_small_media": True} if preview else {"is_disabled": True}}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": [buttons]}
    return tg("sendMessage", **p).ok


def instant_alert(d, cfg, dry, silent=False):
    """Price mistakes: own message, right away, Buy button + Slickdeals/Reddit thread for proof."""
    c = cfg["categories"][d["cat"]]
    price = f"<b>${d['price']:,.2f}</b>" if d["price"] else ""
    if d["reg"]:
        price += f" <s>${d['reg']:,.0f}</s>"
    if d["pct"]:
        price += f" −{d['pct']}%"
    age = f"posted {d['age_h'] * 60:.0f} min ago" if d["age_h"] is not None and d["age_h"] < 1 else \
          (f"posted {d['age_h']:.0f}h ago" if d["age_h"] is not None else "")
    stock = "✅ store shows in stock" if d["status"] == "in_stock" else "⚡ move fast — may already be gone"
    text = "\n".join(x for x in [f"{c.get('emoji', '🚨')} <b>{c.get('title', 'PRICE MISTAKE').upper()}</b>",
                                  html.escape(short(d["title"], 120)), price,
                                  " · ".join(x for x in [age, stock] if x)] if x)
    btn = [{"text": "🛒 Buy now", "url": d["buy"]}]
    if d["thread"].startswith("http") and d["thread"] != d["buy"]:
        btn.append({"text": "💬 Proof / comments", "url": d["thread"]})
    if dry:
        return send_text(text, True, buttons=btn)
    return tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML", disable_notification=silent,
              link_preview_options={"url": d["buy"], "prefer_small_media": True},
              reply_markup={"inline_keyboard": [btn]}).ok


STORE_SEARCH = [
    ("amazon", "Amazon", "https://www.amazon.com/s?k={q}"), ("walmart", "Walmart", "https://www.walmart.com/search?q={q}"),
    ("best buy", "Best Buy", "https://www.bestbuy.com/site/searchpage.jsp?st={q}"),
    ("bestbuy", "Best Buy", "https://www.bestbuy.com/site/searchpage.jsp?st={q}"),
    ("target", "Target", "https://www.target.com/s?searchTerm={q}"),
    ("costco", "Costco", "https://www.costco.com/CatalogSearch?keyword={q}"),
    ("home depot", "Home Depot", "https://www.homedepot.com/s/{q}"), ("lowe", "Lowe's", "https://www.lowes.com/search?searchTerm={q}"),
    ("ebay", "eBay", "https://www.ebay.com/sch/i.html?_nkw={q}"), ("newegg", "Newegg", "https://www.newegg.com/p/pl?d={q}"),
    ("wayfair", "Wayfair", "https://www.wayfair.com/keyword.php?keyword={q}"),
    ("sam's club", "Sam's Club", "https://www.samsclub.com/s/{q}"), ("b&h", "B&H", "https://www.bhphotovideo.com/c/search?q={q}"),
]


def store_search(title, store):
    """A link that can't expire: search the store (or Google Shopping) for the product. -> (store label, url)"""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", clean_title(title) or title)
    t = re.sub(r"[,;:]", " ", re.split(r"\s(?:w/|\+|-)\s", t)[0])
    q = quote_plus(" ".join(t.split()[:9]))
    for key, label, url in STORE_SEARCH:
        if key in (store or "").lower():
            return label, url.format(q=q)
    return (store or "")[:22], f"https://www.google.com/search?tbm=shop&q={q}"


def usd(x):
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def ago(hours):
    if hours is None:
        return ""
    if hours < 1:
        return f"🟢 {max(1, round(hours * 60))} min ago"
    return f"{'🟡' if hours < 4 else '🟠'} {hours:.0f}h ago"


def card_parts(d, cfg, expired=None):
    """Caption + buttons for a wish-list card. `expired` = hours the deal lasted, once it's dead."""
    e = html.escape
    label, search = store_search(d["title"], d.get("store"))
    tag = e(d["watch"]) if d.get("watch") else e(d.get("wish") or "")
    title = e(short(d["title"], 95))
    price = usd(d["price"]) if d.get("price") else ""
    if expired is not None:
        last = f" · lasted ~{expired:.0f}h" if expired >= 1 else ""
        lines = [f"❌ <b>EXPIRED</b> · {tag}", f"<s>{title}</s>",
                 f"<s>{price}</s>{' at ' + e(d['store']) if d.get('store') else ''}{last}"]
        rows = [[{"text": f"🔎 Check {label or 'price'} anyway", "url": search}]]
    else:
        money_line = f"💰 <b>{price}</b>" if price else ""
        if d.get("reg") and d.get("price") and d["reg"] > d["price"]:
            money_line += f"  <s>{usd(d['reg'])}</s>"
        if d.get("pct"):
            money_line += f"  <b>−{d['pct']}%</b>" + (" 🔥" if d["pct"] >= 50 else "")
        where = " · ".join(x for x in [f"🏬 {e(d['store'])}" if d.get("store") else "", ago(d.get("age_h"))] if x)
        site = "Slickdeals" if "slickdeals.net" in d.get("thread", "") else "DealNews"
        proof = {"in_stock": f"✅ In stock at {e(label or d.get('store') or 'store')}",
                 "live": f"✅ Still live on {site}"}.get(d.get("status"), "⚠️ Couldn't verify — tap soon")
        if d.get("thumbs"):
            proof += f" · 👍 {d['thumbs']}"
        proof += f" · {cfg.get('_now', '')}" if cfg.get("_now") else ""
        low = "📉 Lowest price I've tracked for this" if d.get("hist") else ""
        head = "WATCH HIT" if d.get("watch") else "WISH LIST"
        lines = [f"🎯 <b>{head}</b> · {tag}", f"<b>{title}</b>", "", money_line, where, proof, low]
        rows = [[{"text": f"🛒 Buy at {label}" if label else "🛒 Open deal", "url": d["buy"]}],
                [{"text": f"🔎 Search {label}" if label else "🔎 Search price", "url": search}]]
    if d.get("thread", "").startswith("http") and d["thread"] != d.get("buy"):
        rows[-1].append({"text": "💬 Thread", "url": d["thread"]})
    cap = "\n".join(l for i, l in enumerate(lines) if l or i == 2)
    return cap.strip(), {"inline_keyboard": rows}


def fetch_image(d):
    """Download the product photo ourselves (bigger version first) so Telegram never fails to fetch it."""
    urls = [d.get("image_hd"), re.sub(r"/\d{2,3}x\d{2,3}/", "/450x450/", d["image"]) if d.get("image") else None,
            re.sub(r"\?h=\d+&w=\d+$", "?h=500&w=500", d["image"]) if d.get("image") else None, d.get("image")]
    for u in dict.fromkeys(u for u in urls if u):
        try:
            r = HTTP.get(u, timeout=15)
            if r.ok and r.headers.get("content-type", "").startswith("image") and 1500 < len(r.content) < 9_000_000:
                return r.content
        except Exception:
            pass
    return None


def send_card(d, cfg, dry, silent=False):
    """Wish-list deal = its own photo card with Buy button. Returns {'mid', 'photo'} or None."""
    cap, markup = card_parts(d, cfg)
    if dry:
        log("\n┌─ CARD " + "─" * 40 + "\n" + html.unescape(re.sub(r"<[^>]+>", "", cap)) + "\n" +
            "\n".join("  ".join(f"[{b['text']}]" for b in row) for row in markup["inline_keyboard"]) +
            f"\n  buy → {d['buy'][:110]}\n  photo → {d.get('image')}\n└" + "─" * 46)
        return {"mid": 0, "photo": False}
    try:
        if img := fetch_image(d):
            r = HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", timeout=30,
                          data={"chat_id": TG_CHAT, "caption": cap, "parse_mode": "HTML",
                                "reply_markup": json.dumps(markup), "disable_notification": "true" if silent else "false"},
                          files={"photo": ("deal.jpg", img)})
            if r.ok:
                return {"mid": r.json()["result"]["message_id"], "photo": True}
            log(f"  ! sendPhoto {r.status_code}: {r.text[:160]} — falling back to text")
    except Exception as ex:
        log(f"  ! photo card failed ({ex.__class__.__name__}) — falling back to text")
    r = tg("sendMessage", chat_id=TG_CHAT, text=cap, parse_mode="HTML", reply_markup=markup, disable_notification=silent,
           link_preview_options={"url": d.get("image") or d["buy"], "prefer_large_media": True, "show_above_text": True})
    if not r.ok:  # last resort: plain text, no preview
        r = tg("sendMessage", chat_id=TG_CHAT, text=cap, parse_mode="HTML", reply_markup=markup,
               link_preview_options={"is_disabled": True})
    try:
        return {"mid": r.json()["result"]["message_id"], "photo": False} if r.ok else None
    except Exception:
        return None


def sweep_cards(cards, cfg, dry):
    """Re-check cards already on your phone; when a deal dies, rewrite its card as ❌ EXPIRED and pull the Buy button."""
    now = time.time()
    keep_s = (cfg.get("wishlist_alerts") or {}).get("track_hours", 36) * 3600
    for k in [k for k, c in cards.items() if now - c["ts"] > keep_s]:
        del cards[k]
    live = sorted((c for c in cards.values() if not c.get("dead")), key=lambda c: -c["ts"])[:15]
    if not live or dry:
        return 0

    def state(c):
        src, _ = source_check(c["thread"]) if c.get("thread", "").startswith("http") else (None, "")
        if src != "dead" and re.search(STORE_DOMAINS, urlparse(c["buy"]).netloc + "."):
            src = "dead" if check_store(c["buy"])[0] in ("oos", "dead") else src
        return src
    with ThreadPoolExecutor(8) as ex:
        states = list(ex.map(state, live))
    n = 0
    for c, s in zip(live, states):
        if s != "dead":
            continue
        cap, markup = card_parts(c, cfg, expired=(now - (c.get("pub") or c["ts"])) / 3600)
        r = tg("editMessageCaption" if c.get("photo") else "editMessageText", chat_id=TG_CHAT, message_id=c["mid"],
               parse_mode="HTML", reply_markup=markup, **({"caption": cap} if c.get("photo") else
                                                           {"text": cap, "link_preview_options": {"is_disabled": True}}))
        c["dead"] = True  # even if the edit failed (message deleted): don't retry forever
        n += r.ok
        log(f"  ❌ marked expired: {c['title'][:60]}")
    return n


def digest(deals, cfg, dry):
    """Short and scannable: one header, category sections, ≤ per_cat lines each."""
    if not deals:
        return []
    per_cat = cfg.get("per_category", 4)
    now = datetime.now(ZoneInfo(cfg.get("timezone", "America/Chicago"))).strftime("%-I:%M %p")
    sent, blocks = [], []
    for cat, c in cfg["categories"].items():
        group = [d for d in deals if d["cat"] == cat][:per_cat]
        if group:
            sent += group
            blocks.append(f"{c.get('emoji', '')} <b>{c.get('title', cat)}</b>\n" + "\n".join(deal_line(d) for d in group))
    chunks, cur = [], f"🦅 <b>{len(sent)} deals</b> · {now}"
    for b in blocks:
        if len(cur) + len(b) + 2 > TG_LIMIT:
            chunks.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    chunks.append(cur)
    for ch in chunks:
        send_text(ch, dry, silent=cfg.get("digest_silent", False))
    return sent


# ───────────────────────── main ─────────────────────────
def in_quiet_hours(cfg):
    a, b = cfg.get("quiet_hours", [23, 7])
    h = datetime.now(ZoneInfo(cfg.get("timezone", "America/Chicago"))).hour
    return (h >= a or h < b) if a > b else (a <= h < b)


def load_seen():
    s = load_json(SEEN_FILE, {})
    if s.get("_v") != 2:
        s = {k: v + SENT_TTL for k, v in s.items() if isinstance(v, (int, float))}
        s["_v"] = 2
    return s


def run(args):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    st = load_json(SETTINGS_FILE, {})
    quiet = in_quiet_hours(cfg) and not args.dry_run
    if quiet:
        log("Quiet hours — price mistakes only, silent.")
    if not args.dry_run and not args.feed:
        if not st.get("welcomed_v4"):
            send_text("🦅 <b>DealHawk v4</b>: shorter digest · 🔥 Ultra deals · 🆓 Freebies · 🎁 Giveaways · "
                      "🏷️ Coupons · 🖼️ Home decor. Send /list to see categories.", False)
            st["welcomed_v4"] = True
        handle_commands(cfg, st)
    cfg = apply_settings(cfg, st)
    now, seen, prices = time.time(), load_seen(), load_json(PRICES_FILE, {})
    cards = load_json(CARDS_FILE, {})
    wl = cfg.get("wishlist_alerts") or {}
    cfg["_now"] = datetime.now(ZoneInfo(cfg.get("timezone", "America/Chicago"))).strftime("%-I:%M %p")
    if not args.feed:
        try:
            sweep_cards(cards, cfg, args.dry_run)
        except Exception as ex:  # never let housekeeping stop the hunt
            log(f"  ! card sweep failed: {ex!r}")
    strict = cfg.get("stock_policy") == "strict"
    cap = cfg.get("max_deals_per_run", 25)

    sources = [("local", feedparser.parse(args.feed).entries)] if args.feed else all_feeds(cfg)
    cands, ids, skips = [], set(), {}
    feed_cat = {f["name"]: f.get("category") for f in cfg.get("feeds", [])}
    feed_age = {f["name"]: f.get("max_age_hours") for f in cfg.get("feeds", [])}
    wish_only = {f["name"] for f in cfg.get("feeds", []) if f.get("wish_only")}
    for name, entries in sources:
        log(f"{name}: {len(entries)} items")
        for e in entries:
            d = to_deal(e, name)
            hold = seen.get(d["id"], 0)
            pk = product_key(d["title"])
            if d["id"] in ids or pk in ids or (hold > now and not (FORCE and hold < now + OOS_RECHECK + 60)):
                continue
            ids.update((d["id"], pk))  # same deal from two feeds = one line
            d["forced"], d["feed_max_age"] = feed_cat.get(name), feed_age.get(name)
            d["watch"], d["cat"] = watch_hit(d, cfg), categorize(d, cfg)
            d["wish"] = wish_kw(d, cfg)
            if name in wish_only and not (d["wish"] or d["watch"]):
                continue  # radar feeds only exist to catch wish-list items early
            ok, why = passes(d, cfg, relaxed=cfg["categories"].get(d["cat"], {}).get("relaxed", False))
            if quiet and not cfg["categories"].get(d["cat"], {}).get("instant"):
                continue  # leave it unseen for the morning
            if ok and (d["watch"] or d["cat"]):
                d["cat"] = d["cat"] or next(iter(cfg["categories"]))
                cands.append(d)
            else:
                k = why or "no category"
                skips[k] = skips.get(k, 0) + 1
                if args.verbose:
                    log(f"  skip: {d['title'][:70]} — {k}")
    log(f"Skipped: {dict(sorted(skips.items(), key=lambda x: -x[1]))}")

    cands.sort(key=lambda d: (not cfg["categories"].get(d["cat"], {}).get("instant"), not d.get("watch"),
                              not d.get("wish"), -(d["pct"] or 0), -(d["thumbs"] or 0)))
    cands = cands[: cap * 2]
    if args.feed:
        results = [("in_stock", "offline")] * len(cands)
    else:
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(resolve_and_verify, cands))

    keep, tally = [], {}
    for d, (status, why) in zip(cands, results):
        tally[status] = tally.get(status, 0) + 1
        age = f"{d['age_h']:.0f}h" if d["age_h"] is not None else "?"
        log(f"  [{status}] {age} old · {why} — {d['title'][:55]}")
        d["status"] = status
        if status in ("oos", "dead") or (strict and status not in ("in_stock", "live")):
            seen[d["id"]] = now + OOS_RECHECK
        elif len(keep) < cap:
            d["hist"] = history_note(d, prices)
            keep.append(d)

    instant = [d for d in keep if cfg["categories"].get(d["cat"], {}).get("instant")]
    for d in instant:
        instant_alert(d, cfg, args.dry_run, silent=quiet)
        seen[d["id"]] = now + SENT_TTL
    keep = [d for d in keep if d not in instant]
    # Wish list + watchlist: each gets its own photo card, freshest first. Overflow rides in the digest.
    wishes = sorted((d for d in keep if d.get("watch") or d.get("wish")),
                    key=lambda d: (not d.get("watch"), d["age_h"] if d["age_h"] is not None else 99))
    n_cards = 0
    for d in wishes[: wl.get("cards_per_run", 4)]:
        try:
            res = send_card(d, cfg, args.dry_run)
        except Exception as ex:
            log(f"  ! card failed, leaving it for the digest: {ex!r}")
            res = None
        if not res:
            continue
        n_cards += 1
        seen[d["id"]] = now + SENT_TTL
        keep.remove(d)
        cards[d["id"]] = {"mid": res["mid"], "photo": res["photo"], "ts": now, "pub": d.get("pub"),
                          **{k: d.get(k) for k in ("title", "buy", "thread", "store", "price", "watch", "wish")}}
    keep.sort(key=lambda d: (d["status"] not in ("in_stock", "live"), -(d["pct"] or 0), -(d["thumbs"] or 0)))
    sent = digest(keep, cfg, args.dry_run)
    for d in sent:  # deals that didn't fit stay eligible for the next run
        seen[d["id"]] = now + SENT_TTL

    log(f"Checked {len(cands)}: {tally}. Sent {len(instant)} price mistakes + {n_cards} wish-list cards + {len(sent)} in digest.")
    if not args.dry_run:
        save_json(SEEN_FILE, {k: v for k, v in seen.items() if k == "_v" or v > now})
        save_json(PRICES_FILE, prices)
        save_json(SETTINGS_FILE, st)
        save_json(CARDS_FILE, cards)


def demo(src):
    """Send one real wish-list card right now (newest live match), without touching saved state."""
    cfg = apply_settings(yaml.safe_load((ROOT / "config.yaml").read_text()), load_json(SETTINGS_FILE, {}))
    cfg["_now"] = datetime.now(ZoneInfo(cfg.get("timezone", "America/Chicago"))).strftime("%-I:%M %p")
    feeds = [("demo", fetch_feed(src))] if src.startswith("http") else all_feeds(cfg)
    for name, entries in feeds:
        for e in entries:
            d = to_deal(e, name)
            d["forced"] = d["feed_max_age"] = None
            d["watch"], d["cat"], d["wish"] = watch_hit(d, cfg), categorize(d, cfg), wish_kw(d, cfg)
            if not (d["wish"] or d["watch"]) or not passes(d, cfg, relaxed=True)[0]:
                continue
            d["status"], why = resolve_and_verify(d)
            log(f"  [{d['status']}] {why} — {d['title'][:70]}")
            if d["status"] in ("oos", "dead"):
                continue
            d["hist"] = ""
            res = send_card(d, cfg, False)
            return log(f"Sample card sent: {res}")
    send_text("🦅 No live wish-list deal right now to show as a sample. The next one will arrive as a card.", False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--check", metavar="URL")
    ap.add_argument("--feed")
    ap.add_argument("--demo", nargs="?", const="all", metavar="FEED_URL")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.check:
        return log(str(check_store(a.check)))
    if a.demo:
        return demo(a.demo)
    if a.test:
        return log("OK" if send_text("🦅 <b>DealHawk is live.</b> Send /help for commands.", False) else "FAILED")
    run(a)


if __name__ == "__main__":
    main()
