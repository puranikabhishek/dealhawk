#!/usr/bin/env python3
"""
DealHawk 🦅 — free deal sniper for Telegram. $0, zero AI tokens.

Every run:
  1. Reads your Telegram commands (/add, /block, /watch, /off …) and updates settings.
  2. Pulls fresh, community/editor-curated deal feeds (Slickdeals, DealNews, TechBargains, 9to5Toys,
     Hip2Save, Reddit r/deals).
  3. Filters by your categories, blocked words/stores, price.
  4. Verifies: source not expired + store page stock check where the store allows it.
  5. Sends 🎯 wish-list deals as their own photo cards (Buy button, marked ❌ EXPIRED when they die)
     + ONE grouped digest (by category) for everything else.

  6. 🚨 STEALS (wish-list item at a crazy price) go out FIRST: loud, pinned, even in quiet hours.

CLI: --dry-run  --radar  --test  --demo [FEED_URL|steal]  --check URL  --feed FILE  -v
     --radar = fast pass: radar + front-page feeds, steals and price mistakes only (no digest, no sweep)
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
STEAL_CHAT = os.getenv("TELEGRAM_STEAL_CHAT_ID", "") or TG_CHAT  # optional separate chat for 🚨 steals
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
SITES = {"slickdeals.net": "Slickdeals", "dealnews.com": "DealNews", "9to5toys.com": "9to5Toys",
         "techbargains.com": "TechBargains", "hip2save.com": "Hip2Save", "dealcatcher.com": "DealCatcher",
         "reddit.com": "Reddit"}
# Editor sites whose post pages say so when a deal dies (only ever used to DROP a deal, never to vouch for it).
EXPIRY_TEXT_SITES = ("9to5toys.com", "hip2save.com", "dealcatcher.com")


def site_of(url):
    host = urlparse(url or "").netloc.lower()
    return next((n for dom, n in SITES.items() if host == dom or host.endswith("." + dom)), "source")


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


NOT_PRICE_AFTER = re.compile(r"\s*(?:off\b|savings?\b|in (?:savings|rewards|credit)|credit|gift ?card|back\b|cash ?back|"
                             r"rewards?\b|bonus|reward|e?gift|promo|bill credit|instant savings|discount)", re.I)
NOT_PRICE_BEFORE = re.compile(r"(?:save|saving|savings of|up to|over|nearly|almost|extra|additional|get|earn|receive|"
                              r"reg\.?|was|list(?: price)?|msrp|orig\.?|originally|normally|value|valued at|worth|"
                              r"min(?:imum)?\.?|orders? (?:of|over)|spend|purchase of|\$\d+\s*-|w/)\s*:?\s*$", re.I)


CODE_RX = re.compile(r"(?i:\b(?:promo|coupon|discount|offer|checkout)?\s*codes?\b)\s*(?i:is|of)?\s*[:\-]?\s*[\"'“‘(]?"
                     r"([A-Z0-9][A-Z0-9-]{3,19})\b")
CODE_NOT = re.compile(r"^(?:AMAZON|WALMART|TARGET|CHECKOUT|PRIME|FREE|SAVE|DEAL|DEALS|OFF|SHIPPING|BELOW|ABOVE|HERE|NONE|"
                      r"NEEDED|REQUIRED|AT|IN|ON|FOR|THE|AND|WITH|PAGE|CLIP|COUPON|PROMO|CODE|USA|LLC|HTTPS?)$")


def extract_codes(t):
    """Promo codes written in a post ("w/ code SOE56LUB", "promo code: SAVE20"). Real codes only, not words."""
    out = []
    for m in CODE_RX.finditer(t or ""):
        c = m.group(1).strip("-")
        if CODE_NOT.match(c) or not (re.search(r"\d", c) or len(c) >= 5) or re.fullmatch(r"\d{1,3}", c):
            continue
        if c not in out:
            out.append(c)
    return out[:3]


def parse_prices(t):
    t = t.replace("\u00a0", " ")
    price = reg = pct = None
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d{2})?)", t):
        if NOT_PRICE_AFTER.match(t, m.end()) or NOT_PRICE_BEFORE.search(t[max(0, m.start() - 25): m.start()]):
            continue
        price = money(m.group(1))
        break
    if m := re.search(r"(?:regularly|reg\.?|was|list(?: price)?|msrp|orig\.?|originally|normally)\s*:?\s*\$\s?([\d,]+(?:\.\d{2})?)", t, re.I):
        reg = money(m.group(1))
    if price and reg and reg > price:  # real math beats the post's headline %
        pct = round(100 * (1 - price / reg))
    elif m := re.search(r"(\d{1,2})\s?%\s?off", t, re.I):
        pct = int(m.group(1))
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
    # Hip2Save/9to5Toys style: "X Only $89 Shipped on Walmart.com", "… drops to new Amazon all-time low"
    t = re.sub(r"\s+(?:only|just)\b(?=\s*(?:shipped|on\b|at\b|\+|$))", " ", t, flags=re.I)
    t = re.sub(r"\s+(?:shipped\s+)?(?:on|at)\s+[a-z]+\.com\b.*$|\s+shipped\b", " ", t, flags=re.I)
    t = re.sub(r"\(\s*reg\.?[^)]*$", " ", t, flags=re.I)
    t = re.sub(r"\s+(?:on|at)\s+(?:amazon|walmart|target|best buy|home depot|costco|woot)\s*$|\s+(?:at|for|from)\s*$", " ", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" -–,:+")


# Readable product name for links: brand + model + what it is (no prices, colors, specs, store tags).
TYPES = ["contact sensors", "contact sensor", "over-ear headphones", "over-the-ear headphones", "headphones",
         "headphone", "earbuds", "air fryer oven", "air fryer", "toaster oven", "mechanical keyboard", "gaming keyboard",
         "keyboard", "gaming mouse", "wireless mouse", "mouse", "office chair", "office chairs", "desk chair", "chair",
         "bookcase", "bookshelf", "file cabinet", "filing cabinet", "drawers", "thermostat", "smart display",
         "voice recorder", "note taker", "patio set", "sectional", "sofa", "string lights",
         "strip lights", "lights", "frames", "tracker", "tv", "monitor", "computer", "controller"]

def product_name(t, n=48):
    s = t.replace("’", "'")
    s = re.sub(r"^\s*(?:\[[^\]]{1,30}\]\s*)+", "", s)                        # [Prime] [Open-Box]
    s = re.sub(r"^\s*\$[\d,.]+\*?\s*[:|\-]\s*", "", s)                      # "$94: " / "$69* | "
    s = re.sub(r"^\s*[A-Z][\w'&. ]{1,25}?\s+members\s*:\s*", "", s, flags=re.I)  # "Sam's Club Members:"
    s = re.sub(r"^\s*(?:\[[^\]]{1,30}\]\s*)+", "", s)
    s = re.sub(r"\s*(?:\+|,)?\s*(?:with\s+)?free\s+(?:s&h|s/h|shipping|store pickup|ship).*$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:for\s+)?\$[\d,.]+\*?.*$", "", s)                     # price and everything after
    s = re.sub(r"\s+(?:at|on|from)\s+(?:[A-Z][\w&'.]*\.com|amazon|walmart|target|best buy|home depot|costco|woot|lowe's)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\b\d[\d,]*\s?(?:pa|db|mah)\b|\b20\d\d\s+new\b", " ", s, flags=re.I)  # spec noise
    s = re.sub(r"\((?!\w{1,3}\))[^)]*\)|\[[^\]]*\]", " ", s)   # drop (White) (4 Colors), keep model tags like (3a)
    s = re.sub(r"\s+(?:w/|with)\s.*$|\s*[,;|].*$|\s+-\s+(?=[A-Z][a-z]+(?:/[A-Z][a-z]+)?\s*$).*$", "", s, flags=re.I)  # extras, trailing "- Black"
    s = re.sub(r"^([A-Z][\w&.']+)\s+-\s+", r"\1 ", s)                       # "Shokz - OpenRun" → "Shokz OpenRun"
    s = re.sub(r"\b\d[\d,]*\s?(?:pa|db|mah)\b|\b20\d\d\s+new\b", " ", s, flags=re.I)  # spec noise
    s = re.sub(r"\s{2,}", " ", s).strip(" -–:+*")
    if len(s) > n:  # keep brand + model, then the product type
        low = s.lower()
        typ = next((x for x in TYPES if re.search(rf"\b{re.escape(x)}\b", low)), "")
        if typ:
            i = low.find(typ)
            head = s[:i].split()
            ty = s[i:i + len(typ)]
            keep = []
            for w in head:
                if len(" ".join(keep + [w, ty])) > n: break
                keep.append(w)
            s = " ".join(keep + [ty]) if keep else ty
        if len(s) > n:
            s = s[:n].rsplit(" ", 1)[0] + "…"
    return s[:1].upper() + s[1:] if s else t[:n]


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
        cats = {"custom": {"title": "Your Added Keywords", "emoji": "🎯", "keywords": st["custom"]}, **cats}
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
    if SALE_TITLE.search(title) and "$" not in title:
        price = reg = None  # a storewide sale has no one price; the summary's $ is shipping/minimums
    pub = entry.get("published_parsed") or entry.get("updated_parsed")
    th = re.search(r"thumbs?\s*(?:up|score)?\s*:?\s*\+?(-?\d+)", plain, re.I)
    return {"id": hashlib.sha1((entry.get("id") or link or title).encode()).hexdigest()[:16],
            "title": title, "source": source, "thread": link, "buy": buy, "hrefs": hrefs, "plain": plain[:400],
            "codes": extract_codes(title + " " + plain),
            "price": price, "reg": reg, "pct": pct, "thumbs": int(th.group(1)) if th else None,
            "store": entry.get("dealnews_retailer") or store_of(title, plain) or click_store(click)
            or (domain_store(m.group(1)) if (m := re.search(r"\[([a-z0-9-]+)\.com\]", plain[:300])) else "")
            or (domain_store(m.group(1)) if (m := re.search(r"(?:^|\.)([a-z0-9-]+)\.com$", urlparse(buy).netloc))
                and re.search(STORE_DOMAINS, urlparse(buy).netloc + ".") else ""),
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


# Room-revamp items trip the 'cable' / 'monitor' / 'tv' blocks: "cable management tray", "LED strip for TV / monitor".
ROOM_SAFE = re.compile(r"\bcables?(?= (?:management|organi[sz]er|raceway|tray|clips?|sleeves?|ties|hider|box)\b)")
ROOM_LIGHT = re.compile(r"\b(?:backlights?|bias light\w*|light ?bar|led strip|strip lights?|light strip)\b")
ROOM_LIGHT_SAFE = re.compile(r"\b(?:tvs?|monitors?)\b")


# Echo Show titles often say "smart display with speaker" — that's not a standalone speaker.
ECHO_SHOW = re.compile(r"\becho show\b")
ECHO_SHOW_SAFE = re.compile(r"\b(?:smart |stereo |premium )?speakers?\b")


def block_text(tl):
    tl = BLOCK_SAFE.sub(" ", tl)
    if ECHO_SHOW.search(tl):
        tl = ECHO_SHOW_SAFE.sub(" ", tl)
    tl = ROOM_SAFE.sub(" ", tl)
    if ROOM_LIGHT.search(tl):
        tl = ROOM_LIGHT_SAFE.sub(" ", tl)
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


def steal_hit(d, cfg):
    """A wish-list/watchlist item at a crazy price -> short reason string, else None."""
    sc = cfg.get("steal_alerts") or {}
    if not sc.get("enabled", True) or not d.get("price") or not (d.get("wish") or d.get("watch")):
        return None
    for w in cfg.get("watchlist") or []:
        if w.get("steal") and all(has(k, d["tl"]) for k in w["keywords"]) and d["price"] <= w["steal"]:
            return f"under your ${w['steal']:,.0f} steal price"
    if (d.get("pct") or 0) >= sc.get("min_pct", 50):
        return f"{d['pct']}% off"
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
    if not d["price"] and not c.get("prize_patterns") and d.get("cat") != "freebies" and \
            re.search(r"\breview\b|\?|\bhands[- ]on\b|\bfirst look\b|\bmeet the\b|\bannounc|\blaunch", d["tl"]):
        return False, "article, not a deal"
    mx = d.get("feed_max_age") or cfg.get("max_age_hours")
    if mx and d["age_h"] and d["age_h"] > mx:
        return False, "too old (likely expired)"
    cat_max = cfg["categories"].get(d.get("cat"), {}).get("max_age_hours")
    if cat_max and d["age_h"] and d["age_h"] > cat_max:
        return False, "stale price mistake"
    if relaxed:
        return True, ""
    if d["price"] is not None and not d.get("watch") and d["price"] < c.get("min_price", cfg.get("min_price", 0)):
        return False, "under min price"  # a section can set its own floor; watchlist hits have their own price cap
    if d["pct"] is not None and d["pct"] < cfg.get("min_discount_pct", 0):
        return False, "small discount"
    min_th = c.get("min_thumbs", cfg.get("min_thumbs", -999))
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


def store_price(body, h):
    """The price the STORE is charging right now (buy box), or None if we can't tell."""
    num = r"\$?\s?([\d,]+\.\d{2}|[\d,]+)"
    tries = []
    if "amazon." in h:
        # Only the "price to pay" box. Never the struck-through list price, per-unit prices, or other sellers.
        # If Amazon doesn't show it to us, we don't guess.
        m = re.search(r'class="a-price[^"]*(?:\bpriceToPay\b|apexPriceToPay)[^"]*"[^>]*>\s*<span class="a-offscreen">\s*\$\s?([\d,]+\.\d{2})', body)
        return money(m.group(1)) if m and not re.search(r"/\s*(?:count|ounce|oz|fl oz|lb|item|unit)",
                                                       body[m.end(): m.end() + 400]) else None
    if "walmart." in h:
        tries.append(re.search(r'"priceInfo"\s*:\s*\{.{0,400}?"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*([\d.]+)', body, re.S))
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body, re.S)[:8]:
        if re.search(r'"@type"\s*:\s*"(?:Product|Offer|AggregateOffer)"', blob):
            tries.append(re.search(r'"(?:price|lowPrice)"\s*:\s*"?' + num, blob))
    tries.append(re.search(r'<meta[^>]+(?:property|name)="(?:product:price:amount|og:price:amount|twitter:data1)"[^>]+content="\$?([\d,.]+)"', body))
    tries.append(re.search(r'itemprop="price"[^>]+content="([\d,.]+)"', body))
    for m in tries:
        if m and (v := money(m.group(1))) and 0.5 < v < 50000:
            return v
    return None


def check_store(url, want_price=False):
    def out(status, why, price=None):
        return (status, why, price) if want_price else (status, why)
    try:
        r = HTTP.get(url, timeout=20, allow_redirects=True)
    except Exception as e:
        return out("unknown", e.__class__.__name__)
    host_ = urlparse(r.url).netloc.replace("www.", "")
    if r.status_code in (404, 410):
        return out("dead", f"{host_} HTTP {r.status_code}")
    body = r.text[:1_500_000]
    if r.status_code >= 400 or BLOCK_TEXT.search(body[:20000]):
        return out("unknown", f"{host_} blocks bots ({r.status_code})")
    price = store_price(body, host_)
    if a := availability(body):
        return out(a, f"{host_} says {'in stock' if a == 'in_stock' else 'OUT OF STOCK'}", price)
    return out("unknown", f"{host_}: no stock data", price)


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
    if any(s in urlparse(thread).netloc for s in EXPIRY_TEXT_SITES):
        body = page[:400000]
        body = re.search(r"<article.*?</article>", body, re.S).group(0) if "<article" in body and "</article>" in body else body
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S))
        if PAGE_EXPIRED.search(text[:6000]) or re.search(r'class="[^"]*\bexpired\b', body[:60000], re.I):
            return "dead", page
    return None, page


# ───────────────────────── direct store links ─────────────────────────
# Every Buy button must land on the STORE's page — never a blog post, forum thread or deal-site page.
NOT_STORE = re.compile(r"(?:^|\.)(?:slickdeals\.net|sdimg\.com|dealnews\.com|dlnws\.com|9to5toys\.com|9to5mac\.com|"
                       r"9to5google\.com|electrek\.co|hip2save\.com|dealcatcher\.com|techbargains\.com|reddit\.com|"
                       r"redd\.it|redditmedia\.com|facebook\.com|twitter\.com|x\.com|instagram\.com|pinterest\.com|"
                       r"tiktok\.com|youtube\.com|youtu\.be|linkedin\.com|google\.com|gstatic\.com|googleapis\.com|"
                       r"wp\.com|wordpress\.com|gravatar\.com|amazon-adsystem\.com|doubleclick\.net|ltk\.com|attn\.tv|"
                       r"bigcrunch\.com|pub\.network|btloader\.com|cloudfront\.net|cloudflare\.com|jsdelivr\.net|"
                       r"apple\.com/app|podcasts\.apple\.com|t\.co|bsky\.app|threads\.net|slickdealscdn\.com|"
                       r"confiant-integrations\.net|googletagmanager\.com|googlesyndication\.com|criteo\.com|"
                       r"taboola\.com|outbrain\.com|myklpages\.com|klaviyo\.com|mailchi\.mp|list-manage\.com|adnxs\.com|rubiconproject\.com|pubmatic\.com|amazonaws\.com)$"
                       r"|^(?:cdn|static|assets?|img|images|media|ads?|analytics|tags?|pixel|beacon)\d*\.")
# Redirect/affiliate hosts: follow them to wherever they land.
HOPS = re.compile(r"(?:^|\.)(?:link\.amazon|amzn\.to|amzn\.com|a\.co|amzlinks\.in|avantlink\.com|dpbolvw\.net|anrdoezrs\.net|"
                  r"jdoqocy\.com|kqzyfj\.com|tkqlhce\.com|emjcd\.com|linksynergy\.com|sjv\.io|pxf\.io|ojrq\.net|"
                  r"evyy\.net|howl\.me|howl\.link|go\.skimresources\.com|go\.redirectingat\.com|shop-links\.co|"
                  r"bit\.ly|tinyurl\.com|goto\.walmart\.com|goto\.target\.com|bestbuy\.7tiv\.net|"
                  r"click\.linksynergy\.com|prf\.hn|awin1\.com|shareasale\.com|rstyle\.me|mavely\.app|"
                  r"joinhoney\.com|go\.magik\.ly|magik\.ly|fave\.co|lvmv\.co|ztk5\.net|klarna\.com|7tiv\.net|bhpho\.to|ebay\.us|"
                  r"rover\.ebay\.com|walmrt\.us|tgt\.biz|homedepot\.sjv\.io|lowes\.sjv\.io|imp\.i\d+\.net)$")
# Pages that list many deals rather than THE product. Only OK when the deal itself is a store-wide sale.
GENERIC_PAGE = re.compile(r"/gp/goldbox|/deals?(?:[-/]|$|\?)|/promotions?(?:[-/]|$)|/coupons?(?:[-/]|$)|/shop/deals|/black-?friday|/cyber-?monday|/stores/page/|"
                          r"/s\?|/search|/browse/|/cp/|/b/?\?|/sale/?$|/offers?/?$", re.I)
SALE_TITLE = re.compile(r"\bsale\b|up to \d+%|\bdeals\b|sitewide|storewide|clearance|\bevent\b|% off (?:select|all|everything)", re.I)
TITLE_STORES = {"amazon": "amazon.", "walmart": "walmart.", "target": "target.", "best buy": "bestbuy.",
                "home depot": "homedepot.", "lowe's": "lowes.", "lowes": "lowes.", "costco": "costco.",
                "sam's club": "samsclub.", "wayfair": "wayfair.", "kohl's": "kohls.", "woot": "woot.", "newegg": "newegg."}
URL_PARAMS = ("u2", "u", "url", "murl", "destination", "dest", "ued", "ulp", "RD_PARM1", "redirect", "redirect_url", "r")
REDIRECT_ONLY = re.compile(r"slickdeals\.net/click|dealnews\.com/lw/click")  # pure redirects: a tap goes straight to the store


def host(u):
    return urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")


def unwrap(u):
    """Affiliate links often carry the real store URL in a query param (url=, u2=, murl=…)."""
    for _ in range(3):
        q = parse_qs(urlparse(u).query)
        inner = next((q[k][0] for k in URL_PARAMS if q.get(k) and q[k][0].startswith("http")), None)
        if not inner or host(inner) == host(u):
            return u
        u = inner
    return u


def is_store(u):
    h = host(u)
    return u.startswith("http") and "." in h and not NOT_STORE.search(h) and not HOPS.search(h) \
        and len(urlparse(u).path.strip("/")) > 1 \
        and not re.search(r"\.(?:js|css|png|jpe?g|gif|webp|svg|ico|json|xml)(?:\?|$)", urlparse(u).path, re.I)


def follow(u, depth=0):
    """Follow redirects (HTTP + the JS/meta kind) and return the store page it lands on, or None."""
    if depth > 2:
        return None
    try:
        r = HTTP.get(u, timeout=15, allow_redirects=True)
    except Exception:
        return None
    chain = [r.url] + [h.headers.get("location", "") for h in reversed(r.history)]
    for final in chain:
        final = unwrap(final)
        if is_store(final) and len(final) < 1500 and not re.search(r"/blocked|captcha|/errors?/", final, re.I):
            return final
    m = re.search(r"location\.(?:replace|href)\s*[=(]\s*['\"]([^'\"]+)|http-equiv=[\"']refresh[^>]+url=([^\"'>]+)",
                  r.text[:5000], re.I)
    return follow(html.unescape(m.group(1) or m.group(2)), depth + 1) if m else None


def body_region(page):
    """Just the post itself — not sidebars, ads or 'related deals'. Returns (html, found_it)."""
    m = re.search(r'<(?:div|section)[^>]+class="[^"]*\b(?:entry-content|post-content|article-content|article-body|'
                  r'post-body|content-body|deal-content|thread-content)\b', page)
    if m:
        return page[m.start(): m.start() + 80000], True
    if (a := page.find("<article")) >= 0:
        e = page.find("</article>", a)
        return page[a: e if e > 0 else a + 80000], True
    return page, False


def link_score(u, d):
    """Prefer real product pages, on the store the title names."""
    p, h = urlparse(u).path.lower(), host(u)
    sc = 0
    if re.search(r"/dp/|/gp/product/|/gp/aw/d/|/ip/|/site/.+\.p\b|/p/|/product|/products/|/itm/|/a-|\.html?$|/sku", p):
        sc += 3
    if re.search(r"/stores/page/|/s\?|/search|/b/|/browse/|/cp/|/collections?/?$", p + "?" + urlparse(u).query[:3]):
        sc -= 2
    if (st := (d.get("store") or "").lower().replace(" ", "")) and st[:5] in h.replace("-", ""):
        sc += 2
    elif re.search(r"\b(" + re.escape(h.split(".")[-2]) + r")\b", d["tl"]):
        sc += 2
    return sc


def title_store(d):
    """Store the deal title names ('… on Walmart.com', '… at Target'), as a domain stub."""
    tl = d["tl"]
    hits = [dom for name, dom in TITLE_STORES.items()
            if re.search(r"\b(?:on|at|@|from|via)\s+" + re.escape(name) + r"(?:\.com)?\b|^" + re.escape(name) + r"\b", tl)]
    return hits[0] if len(hits) == 1 else None


def slickdeals_body(page):
    """The original post's HTML from the thread page's Nuxt payload (not comments or sidebars)."""
    m = re.search(r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    try:
        arr = json.loads(m.group(1)) if m else []
    except ValueError:
        arr = []
    for o in arr:
        if isinstance(o, dict) and isinstance(o.get("bodyHtml"), int) and o["bodyHtml"] < len(arr):
            if isinstance(body := arr[o["bodyHtml"]], str):
                return html.unescape(body)
    return ""


def slickdeals_store_links(page, d):
    """Slickdeals' 'Get Deal' button is a bot-walled redirect, but the post usually spells the store URL out:
    as link text (amazon.com/dp/B0H1F9VT52), shortened with '…' (walmart.com/ip/Name.../846132139),
    or as an Amazon a.co short link in the button's trd= field. Rebuild a clean product URL from those."""
    body = slickdeals_body(page)
    if not body:
        return []
    out = []
    for t in re.findall(r">\s*(https?://[^<\s]{8,})\s*</a>", body):
        t = html.unescape(t)
        if "..." not in t and "…" not in t:
            out.append(t)
            continue
        h = host(t)
        if "walmart." in h and (m := re.search(r"/(\d{6,12})(?:[/?#]|$)", t)):
            out.append(f"https://www.walmart.com/ip/{m.group(1)}")
        elif "amazon." in h and (m := re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?![A-Z0-9])", t)):
            out.append(f"https://www.amazon.com/dp/{m.group(1)}")
        elif "target." in h and (m := re.search(r"/A-(\d{6,10})", t)):
            out.append(f"https://www.target.com/p/-/A-{m.group(1)}")
        elif "bestbuy." in h and (m := re.search(r"skuId=(\d{6,8})|/(\d{7})(?:\.p)?(?:[/?#]|$)", t)):
            out.append(f"https://www.bestbuy.com/site/{m.group(1) or m.group(2)}.p?skuId={m.group(1) or m.group(2)}")
    for href in re.findall(r'href="([^"]*slickdeals\.net/click[^"]*)"', body):
        trd = (parse_qs(urlparse(html.unescape(href)).query).get("trd") or [""])[0]
        if m := re.match(r"https?\W+a\W+co\W+d\W+([A-Za-z0-9]{6,12})\s*$", trd):  # Amazon's own short link
            short = f"https://a.co/d/{m.group(1)}"
            try:
                loc = HTTP.get(short, timeout=10, allow_redirects=False).headers.get("location", "")
            except Exception:
                loc = ""
            if a := re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", loc):
                out.append(f"https://www.amazon.com/dp/{a.group(1)}")
            elif loc.startswith("http") and is_store(loc):
                out.append(loc)
    clean, seen = [], set()
    for u in out:
        u = unwrap(u)
        if "amazon." in host(u) and (a := re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", u)):
            u = f"https://www.amazon.com/dp/{a.group(1)}"  # drop referral tags and tracking
        else:
            p = urlparse(u)
            q = "&".join(x for x in p.query.split("&") if x and not re.match(r"(?:utm_[a-z]+|tag|ref|afsrc|irgwc|clickid|gclid)=", x))
            u = p._replace(query=q, fragment="").geturl()
        if is_store(u) and re.search(STORE_DOMAINS, host(u) + ".") and u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def find_store_link(d, page):
    """Best direct store link for a deal, or None. Checks the feed text first, then the post itself."""
    want = title_store(d)

    def ok(u):  # right store (when the title names one) and not a page of many deals (unless it's a sale)
        if want and want not in host(u) + ".":  # title says Walmart → only a Walmart link will do
            return False
        return not GENERIC_PAGE.search(urlparse(u).path + "?" + urlparse(u).query) or bool(SALE_TITLE.search(d["title"]))

    if is_store(d["buy"]) and not REDIRECT_ONLY.search(d["buy"]):
        return d["buy"] if ok(d["buy"]) else None
    raw = list(d.get("hrefs") or [])
    site = host(d["thread"])
    if page and "slickdeals.net" in site:  # only the thread's own "See Deal" button
        if sd := re.search(r'https?:\\?/\\?/slickdeals\.net\\?/click[^"\'\s<>]+', page[:600000]):
            raw.append(html.unescape(sd.group(0).replace("\\/", "/")))
    elif page and "dealnews.com" in site:  # only the deal's own buy button (first click-out on the page)
        raw += [html.unescape(x) for x in re.findall(r'https?://www\.dealnews\.com/lw/click\.html\?[\d,]+', page[:900000])][:1]
    elif page:
        region, found = body_region(page)
        links = [html.unescape(x) for x in re.findall(r'(?:href|data-href|data-url)="([^"]+)"', region)]
        if not found:  # whole page = other deals too: only trust the first couple of known-store links
            links = [x for x in links if x.startswith("http") and
                     (re.search(STORE_DOMAINS, host(unwrap(x)) + ".") or HOPS.search(host(x)))][:2]
        raw += links
    seen, direct, hops, redirect_only = set(), [], [], []
    for u in raw:
        if not u.startswith("http"):
            if u.startswith("/"):
                u = "https://" + site + u
            else:
                continue
        u = unwrap(u)
        if u in seen:
            continue
        seen.add(u)
        if REDIRECT_ONLY.search(u):
            redirect_only.append(u)
        elif is_store(u):
            direct.append(u)
        elif HOPS.search(host(u)):
            hops.append(u)
    direct = [u for u in direct if ok(u)]
    if direct:
        return max(direct, key=lambda u: (link_score(u, d), -direct.index(u)))
    if page and "slickdeals.net" in site:  # the poster's own store URL, hidden behind Slickdeals' bot-walled button
        sd = [u for u in slickdeals_store_links(page, d) if ok(u)]
        if sd:
            return max(sd, key=lambda u: (link_score(u, d), -sd.index(u)))
    for u in hops[:4] + [x for x in redirect_only if "dealnews" in x][:1]:
        if (final := follow(u)) and ok(final):
            return final
    # Store redirects bots can't follow (Slickdeals' button, retailer short links): on your phone they
    # open the store page directly, no deal-site page in between.
    return next((u for u in redirect_only if "slickdeals.net/click" in u), None) \
        or next((u for u in hops if not want or want.rstrip(".") in host(u).replace("bhpho", "bhphotovideo")), None)


COUPON_TITLE = re.compile(r"coupon|clip|\bcode\b|promo|s&s|subscribe ?& ?save|prime (?:exclusive|members?|only|day)|"
                          r"for prime|member|w/ ?card|"
                          r"in cart|at checkout|after rebate|rebate|bundle|\+ ?free (?!shipping)|when you buy|\bbogo\b", re.I)


PRICE_FLAGS = [
    (r"\bclip\b|\bcoupon\b", "clip coupon"), (r"\bcode\b|promo", "use code"),
    (r"s&s|subscribe ?& ?save", "Subscribe & Save"), (r"prime (?:exclusive|members?|only)|for prime|\bprime\b price", "Prime price"),
    (r"in cart|at checkout", "price drops in cart"), (r"rebate", "after rebate"),
    (r"open[- ]box|renewed|refurb", "open-box/refurb"), (r"\bymmv\b|in[- ]store|select stores", "varies by store"),
    (r"select (?:colors?|sizes?|styles?)|various colors|\d+ colors", "only some colors"),
    (r"w/ ?(?:\w+ )?card|cardmember", "with store card"),
]


def price_flags(d):
    t = d["title"] + " " + d.get("plain", "")[:300]
    return [note for rx, note in PRICE_FLAGS if re.search(rx, t, re.I)][:2]


def price_verdict(d, sp):
    """Make the alert show what the store actually charges. Returns a reason to DROP, or None."""
    tp = d.get("price")
    d["price_ok"] = True
    if not tp:
        d["price"] = sp
        return None
    if abs(sp - tp) <= max(1.0, 0.03 * tp):
        d["price"] = sp  # same deal; use the store's exact cents
        return None
    if sp < tp:  # even cheaper now
        d["price"] = sp
        d["price_note"] = "cheaper now"
        if d.get("reg") and d["reg"] > sp:
            d["pct"] = round(100 * (1 - sp / d["reg"]))
        return None
    # Store is charging more than the post said.
    if COUPON_TITLE.search(d["title"] + " " + d.get("plain", "")[:300]):
        d["price_note"] = f"page shows ${sp:,.2f} before coupon/code"
        d["store_price"] = sp
        return None
    d["price_ok"] = False
    return f"price went up: post said ${tp:,.2f}, store now ${sp:,.2f} — not sending"


def resolve_and_verify(d):
    site = site_of(d["thread"])
    d["title_price"], d["price_ok"], d["flags"] = d.get("price"), False, price_flags(d)
    # Check 1: the deal site's own page — is the deal still marked active?
    src, page = source_check(d["thread"]) if d["thread"].startswith("http") and not is_store(d["thread"]) else (None, "")
    if src == "dead":
        return "dead", f"{site}: marked expired"
    if page:
        if m := re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page[:200000]):
            d["image_hd"] = html.unescape(m.group(1))
    if "use code" in d["flags"]:
        if not d.get("codes") and page:  # the post's own body usually spells the code out
            region, found = body_region(page)
            d["codes"] = [] if not found else extract_codes(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", region)))[:6000])
        if not d.get("codes"):
            return "nocode", f"{site}: needs a promo code but the post doesn't show it — not sending"
        d["flags"] = [f"code {' / '.join(d['codes'][:2])} at checkout" if f == "use code" else f for f in d["flags"]]
    link = find_store_link(d, page)
    if not link:
        return "nolink", f"{site}: no direct store link — not sending"
    d["buy"] = link
    if not d.get("store") and is_store(link) and (m := re.search(r"([a-z0-9-]+)\.(?:com|net|co|us)$", host(link))):
        d["store"] = domain_store(m.group(1))
    # Check 2: the store page (where the store lets bots look), twice if it says in stock
    if is_store(link):
        status, why, sp = check_store(link, want_price=True)
        if status == "in_stock":
            time.sleep(1.5)
            status, why, sp2 = check_store(link, want_price=True)
            sp = sp2 or sp
            why = why if status == "in_stock" else f"2nd check: {why}"
        if sp and status != "oos" and not SALE_TITLE.search(d["title"]):
            verdict = price_verdict(d, sp)
            if verdict:
                return "pricegone", verdict
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


def short(t, n=48):
    return product_name(t, n)


def heat(pct):
    """Color badge by discount depth — lets you spot the big ones at a glance."""
    return "🟥" if pct >= 70 else "🟧" if pct >= 50 else "🟨" if pct >= 30 else "▫️"


def deal_line(d, n=None):
    """One deal = its own little block:
         ① 🟧 $169  −47%  (was $319)
            Ninja SLUSHi Frozen Drink Maker      ← tap
            🛒 Walmart ›  · ✂️ clip coupon
    Price first so you can scan prices down the page; blank line between deals."""
    e = html.escape
    label, _ = store_search(d["title"], d.get("store"))
    store = e(label or d.get("store") or "Open deal")
    href = e(d["buy"], quote=True)
    num = f"{n}. " if n else ""
    name = f'<a href="{href}">{e(short(d["title"], 60))}</a>'
    if d["cat"] in ("freebies", "giveaways") or not d["price"]:
        head = "🎁 <b>FREE</b>" if d["cat"] in ("freebies", "giveaways") else "🏷️ <b>Store sale</b>"
        return f"{num}{head}\n{name}\n🛒 <a href=\"{href}\">{store} ›</a>"
    pct = d.get("pct") or 0
    top = f"{num}{heat(pct)} <b>{usd(d['price'])}</b>"
    if pct:
        top += f"   <b>−{pct}%</b>"
    if d.get("reg") and d["reg"] > d["price"]:
        top += f"   <i>was {usd(d['reg'])}</i>"
    extra = [f"✂️ {e(f)}" if "clip" in f else e(f) for f in (d.get("flags") or [])[:1]]
    if not d.get("price_ok"):
        extra.append("price not confirmed")
    tail = f"🛒 <a href=\"{href}\">{store} ›</a>" + (f"  <i>· {' · '.join(extra)}</i>" if extra else "")
    return f"{top}\n{name}\n{tail}"


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
    """Price mistakes: own photo card, right away, Buy button + Slickdeals/Reddit thread for proof."""
    e = html.escape
    c = cfg["categories"][d["cat"]]
    label, _ = store_search(d["title"], d.get("store"))
    money = ""
    if d["price"]:
        money = f"💸 <b>{'' if d.get('price_ok') else '≈'}{usd(d['price'])}</b>"
        if d["reg"]:
            money += f"   <s>{usd(d['reg'])}</s>"
        if d["pct"]:
            money += f"   <b>−{d['pct']}%</b>"
        if d.get("flags"):
            money += f"\n<i>{e(', '.join(d['flags']))}</i>"
    age = ago(d["age_h"])
    stock = "✅ In stock right now" if d["status"] == "in_stock" else "⚡ Unverified — may already be gone"
    where = " · ".join(x for x in [f"🏬 {e(d['store'])}" if d.get("store") else "", age] if x)
    lines = [f"{c.get('emoji', '🚨')} <b>{e(c.get('title', 'Price Error: Buy Fast').upper())}</b>",
             f"<b>{e(short(d['title'], 90))}</b>", "",
             f"<blockquote>{money}</blockquote>" if money else "", *code_lines(d), where, stock, "",
             "<i>Price errors get cancelled or pulled fast. Order first, ask questions later.</i>"]
    cap = tidy(lines)
    btn = [{"text": f"🛒 Buy at {label}" if label else "🛒 Buy now", "url": d["buy"]}]
    if d["thread"].startswith("http") and d["thread"] != d["buy"]:
        btn.append({"text": "💬 Proof", "url": d["thread"]})
    rows = [btn] + ([copy_button(d)] if copy_button(d) else [])
    return send_card(d, cfg, dry, silent=silent, parts=(cap, {"inline_keyboard": rows}))


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


def tidy(lines):
    """Join caption lines; never more than one blank line in a row."""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


CODE_HOWTO = [
    ("amazon", 'Proceed to checkout → Payment → "Add a gift card or promotion code" → Apply'),
    ("walmart", 'Continue to checkout → "Add promo code" → Apply'),
    ("target", 'Check out → "Promo code" → Apply'),
    ("best buy", 'Checkout → "Gift card or discount code" → Apply'),
]


def code_lines(d):
    """The code, big and copyable, plus what the cart will show and where to paste it."""
    codes = d.get("codes") or []
    if not codes:
        return []
    e = html.escape
    label, _ = store_search(d["title"], d.get("store"))
    out = ["🔑 <b>Code:</b>  " + "  /  ".join(f"<code>{e(c)}</code>" for c in codes[:2]) + "   <i>(tap to copy)</i>"]
    if d.get("store_price") and d.get("price") and d["store_price"] > d["price"]:
        out.append(f"🧾 Cart shows {usd(d['store_price'])} → <b>{usd(d['price'])}</b> after the code")
    how = next((h for k, h in CODE_HOWTO if k in (label or d.get("store") or "").lower()), "Paste it in the promo/coupon box at checkout")
    out.append(f"👉 {e(label or d.get('store') or 'Store')}: {e(how)}")
    return out


def copy_button(d):
    c = (d.get("codes") or [None])[0]
    return [{"text": f"📋 Copy {c}", "copy_text": {"text": c}}] if c else []


def code_parts(d, cfg):
    """🎟️ Promo-code deal: its own card, the code front and center."""
    e = html.escape
    label, search = store_search(d["title"], d.get("store"))
    pct = f" · −{d['pct']}%" if d.get("pct") else ""
    money = f"💰 You pay <b>{usd(d['price'])}</b>" if d.get("price") else ""
    if money and d.get("reg") and d["reg"] > d["price"]:
        money += f"   <i>was {usd(d['reg'])}</i>"
    where = " · ".join(x for x in [f"🏬 {e(label or d.get('store') or '')}" if (label or d.get("store")) else "",
                                   ago(d.get("age_h"))] if x)
    proof = {"in_stock": "✅ In stock", "live": f"✅ Still live on {site_of(d.get('thread'))}"}.get(
        d.get("status"), "⚠️ Couldn't verify stock")
    wish = f"🎯 {e(d.get('watch') or d.get('wish'))}" if (d.get("watch") or d.get("wish")) else ""
    lines = [f"🎟️ <b>PROMO CODE DEAL{pct}</b>", f"<b>{e(short(d['title'], 80))}</b>", "", money, *code_lines(d), "",
             " · ".join(x for x in [where, proof] if x), wish]
    rows = [[{"text": f"🛒 Open at {label}" if label else "🛒 Open deal", "url": d["buy"]}], copy_button(d)]
    return tidy(lines), {"inline_keyboard": [r for r in rows if r]}


def card_parts(d, cfg, expired=None):
    """Caption + buttons for a wish-list card. `expired` = hours the deal lasted, once it's dead."""
    e = html.escape
    label, search = store_search(d["title"], d.get("store"))
    tag = e(d["watch"]) if d.get("watch") else e(d.get("wish") or "")
    title = e(short(d["title"], 60))
    price = usd(d["price"]) if d.get("price") else ""
    if expired is not None:
        last = f" · lasted ~{expired:.0f}h" if expired >= 1 else ""
        lines = [f"❌ <b>EXPIRED</b> · {tag}", f"<s>{title}</s>",
                 f"<s>{price}</s>{' at ' + e(d['store']) if d.get('store') else ''}{last}"]
        rows = [[{"text": f"🔎 Check {label or 'price'} anyway", "url": search}]]
    else:
        money_line = f"💸 <b>{price}</b>" if price else ""
        if d.get("reg") and d.get("price") and d["reg"] > d["price"]:
            money_line += f"   <s>{usd(d['reg'])}</s>"
        if d.get("pct"):
            money_line += f"   <b>−{d['pct']}%</b> {heat(d['pct'])}"
        notes = ([d["price_note"]] if d.get("price_note") else []) + list(d.get("flags") or [])
        if money_line and not d.get("price_ok"):
            notes.append("price from deal post, not confirmed on store page")
        if notes and money_line:
            money_line += f"\n<i>{e(' · '.join(dict.fromkeys(notes)))}</i>"
        if money_line:
            money_line = f"<blockquote>{money_line}</blockquote>"
        where = " · ".join(x for x in [f"🏬 {e(d['store'])}" if d.get("store") else "", ago(d.get("age_h"))] if x)
        site = site_of(d.get("thread"))
        proof = {"in_stock": f"✅ In stock at {e(label or d.get('store') or 'store')}",
                 "live": f"✅ Still live on {site}"}.get(d.get("status"), "⚠️ Couldn't verify — tap soon")
        if d.get("thumbs"):
            proof += f" · 👍 {d['thumbs']}"
        proof += f" · {cfg.get('_now', '')}" if cfg.get("_now") else ""
        low = "📉 Lowest price I've tracked for this" if d.get("hist") else ""
        head = "TARGET PRICE HIT" if d.get("watch") else "WISH LIST"
        if d.get("codes") and not d.get("watch") and not d.get("wish"):
            return code_parts(d, cfg)
        lines = [f"🎯 <b>{head}</b> · <i>{tag}</i>", f"<b>{title}</b>", "", money_line, *code_lines(d), where, proof, low]
        rows = [[{"text": f"🛒 Buy at {label}" if label else "🛒 Open deal", "url": d["buy"]},
                 {"text": "🔎 Compare", "url": search}]]
        if copy_button(d):
            rows.append(copy_button(d))
    cap = "\n".join(l for i, l in enumerate(lines) if l or i == 2)
    return cap.strip(), {"inline_keyboard": rows}


def fetch_image(d):
    """Download the product photo ourselves (bigger version first) so Telegram never fails to fetch it."""
    urls = [d.get("image_hd"), re.sub(r"/\d{2,3}x\d{2,3}/", "/450x450/", d["image"]) if d.get("image") else None,
            re.sub(r"\?h=\d+&w=\d+$", "?h=500&w=500", d["image"]) if d.get("image") else None,
            re.sub(r"size_\d+x\d+", "size_500x500", d["image"]) if d.get("image") else None, d.get("image")]
    for u in dict.fromkeys(u for u in urls if u):
        try:
            r = HTTP.get(u, timeout=15)
            if r.ok and r.headers.get("content-type", "").startswith("image") and 1500 < len(r.content) < 9_000_000:
                return r.content
        except Exception:
            pass
    return None


def send_card(d, cfg, dry, silent=False, parts=None, chat=None):
    """Wish-list deal = its own photo card with Buy button. Returns {'mid', 'photo', 'chat'} or None."""
    cap, markup = parts or card_parts(d, cfg)
    chat = chat or TG_CHAT
    if dry:
        log("\n┌─ CARD " + "─" * 40 + "\n" + html.unescape(re.sub(r"<[^>]+>", "", cap)) + "\n" +
            "\n".join("  ".join(f"[{b['text']}]" for b in row) for row in markup["inline_keyboard"]) +
            f"\n  buy → {d['buy'][:110]}\n  photo → {d.get('image')}\n└" + "─" * 46)
        return {"mid": 0, "photo": False, "chat": chat}
    try:
        if img := fetch_image(d):
            r = HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", timeout=30,
                          data={"chat_id": chat, "caption": cap, "parse_mode": "HTML",
                                "reply_markup": json.dumps(markup), "disable_notification": "true" if silent else "false"},
                          files={"photo": ("deal.jpg", img)})
            if r.ok:
                return {"mid": r.json()["result"]["message_id"], "photo": True, "chat": chat}
            log(f"  ! sendPhoto {r.status_code}: {r.text[:160]} — falling back to text")
    except Exception as ex:
        log(f"  ! photo card failed ({ex.__class__.__name__}) — falling back to text")
    r = tg("sendMessage", chat_id=chat, text=cap, parse_mode="HTML", reply_markup=markup, disable_notification=silent,
           link_preview_options={"url": d.get("image") or d["buy"], "prefer_large_media": True, "show_above_text": True})
    if not r.ok:  # last resort: plain text, no preview
        r = tg("sendMessage", chat_id=chat, text=cap, parse_mode="HTML", reply_markup=markup,
               link_preview_options={"is_disabled": True})
    try:
        return {"mid": r.json()["result"]["message_id"], "photo": False, "chat": chat} if r.ok else None
    except Exception:
        return None


def steal_parts(d, cfg):
    """🚨 STEAL card: impossible to mistake for a normal wish-list card."""
    e = html.escape
    label, search = store_search(d["title"], d.get("store"))
    pct = f" · {d['pct']}% OFF" if d.get("pct") else ""
    money_line = f"💰 <b>{usd(d['price'])}</b>"
    if d.get("reg") and d["reg"] > d["price"]:
        money_line += f"   <s>{usd(d['reg'])}</s>"
    if not d.get("price_ok"):
        money_line += "  <i>(price from deal post)</i>"
    where = " · ".join(x for x in [f"🏬 {e(d['store'])}" if d.get("store") else "", ago(d.get("age_h"))] if x)
    proof = {"in_stock": f"✅ In stock at {e(label or d.get('store') or 'store')}",
             "live": f"✅ Still live on {site_of(d.get('thread'))}"}.get(d.get("status"), "⚠️ Couldn't verify stock")
    lines = [f"🚨🚨 <b>STEAL{pct}</b> 🚨🚨", f"<b>{e(short(d['title'], 70))}</b>", "",
             money_line, *code_lines(d), f"🎯 {e(d.get('watch') or d.get('wish') or '')} · {e(d['steal'])}", where,
             f"{proof} · {cfg.get('_now', '')}", "",
             "⚡ <b>Deals this cheap sell out in about an hour. Buy now, decide later</b> (most stores take returns)."]
    rows = [[{"text": f"🛒 BUY NOW at {label}" if label else "🛒 BUY NOW", "url": d["buy"]}],
            [{"text": f"🔎 Search {label}" if label else "🔎 Search price", "url": search}]] + \
        ([copy_button(d)] if copy_button(d) else [])
    return tidy(lines), {"inline_keyboard": rows}


def send_steal(d, cfg, dry, silent=False):
    sc = cfg.get("steal_alerts") or {}
    res = send_card(d, cfg, dry, silent=silent, parts=steal_parts(d, cfg), chat=STEAL_CHAT)
    if res and not dry and sc.get("pin", True):
        tg("pinChatMessage", chat_id=res["chat"], message_id=res["mid"], disable_notification=silent)
    return res


def sweep_cards(cards, cfg, dry, before=None):
    """Re-check cards already on your phone; when a deal dies, rewrite its card as ❌ EXPIRED and pull the Buy button."""
    now = time.time()
    keep_s = (cfg.get("wishlist_alerts") or {}).get("track_hours", 36) * 3600
    for k in [k for k, c in cards.items() if now - c["ts"] > keep_s]:
        del cards[k]
    before = before or now + 1  # skip cards sent this very run
    live = sorted((c for c in cards.values() if not c.get("dead") and c["ts"] < before), key=lambda c: -c["ts"])[:15]
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
        chat = c.get("chat") or TG_CHAT
        if c.get("steal"):
            tg("unpinChatMessage", chat_id=chat, message_id=c["mid"])
        r = tg("editMessageCaption" if c.get("photo") else "editMessageText", chat_id=chat, message_id=c["mid"],
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
            body = "\n\n".join(deal_line(d) for d in group)
            blocks.append(f"━━━━━━━━━━━━━━\n{c.get('emoji', '')} <b>{html.escape(c.get('title', cat).upper())}</b>"
                          f"  <i>({len(group)})</i>\n━━━━━━━━━━━━━━\n\n{body}")
    if not sent:
        return []
    best = max(sent, key=lambda d: d.get("pct") or 0)
    top = f"\n🏆 Best: <b>−{best['pct']}%</b> · {html.escape(short(best['title'], 34))}" if best.get("pct") else ""
    chunks, cur = [], f"🦅 <b>DEALHAWK</b> · {now} · {len(sent)} deal{'' if len(sent) == 1 else 's'}{top}"
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
    steal_loud = (cfg.get("steal_alerts") or {}).get("ignore_quiet_hours", True)
    if quiet:
        log(f"Quiet hours — price mistakes (silent) + steals ({'loud' if steal_loud else 'silent'}) only.")
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
    strict = cfg.get("stock_policy") == "strict"
    cap = cfg.get("max_deals_per_run", 25)

    if args.feed:
        sources = [("local", feedparser.parse(args.feed).entries)]
    elif args.radar:  # fast pass: the feeds where steals show up first
        rc = dict(cfg, feeds=[f for f in cfg.get("feeds", []) if f.get("wish_only") or f.get("radar")])
        sources = all_feeds(rc)
    else:
        sources = all_feeds(cfg)
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
            d["steal"] = steal_hit(d, cfg)
            is_instant = cfg["categories"].get(d["cat"], {}).get("instant")
            if args.radar and not (d["steal"] or is_instant):
                continue  # radar pass only hunts steals + price mistakes; the rest waits for the full run
            if name in wish_only and not (d["wish"] or d["watch"]
                                          or cfg["categories"].get(d["cat"], {}).get("radar")):
                continue  # radar feeds only exist to catch wish-list (and `radar: true` section) items early
            ok, why = passes(d, cfg, relaxed=bool(d["steal"]) or cfg["categories"].get(d["cat"], {}).get("relaxed", False))
            if quiet and not (is_instant or d["steal"]):
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

    cands.sort(key=lambda d: (not d.get("steal"), not cfg["categories"].get(d["cat"], {}).get("instant"), not d.get("watch"),
                              not d.get("wish"), -(d["pct"] or 0), -(d["thumbs"] or 0)))
    cands = cands[: cap * 3]
    if args.feed:
        results = [("in_stock", "offline")] * len(cands)
    else:
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(resolve_and_verify, cands))

    keep, tally = [], {}
    for d, (status, why) in zip(cands, results):
        tally[status] = tally.get(status, 0) + 1
        age = f"{d['age_h']:.0f}h" if d["age_h"] is not None else "?"
        log(f"  [{status}] {age} old · {why} — {d['title'][:55]} → {host(d['buy'])}{urlparse(d['buy']).path[:40]}"
            f" · alert ${d.get('price') or 0:,.2f} (post ${d.get('title_price') or 0:,.2f})")
        d["status"] = status
        unchecked = d.get("price") and not d.get("price_ok")
        limit = cfg.get("live_price_max_hours", 24) if status == "live" else cfg.get("unchecked_price_max_hours", 8)
        if unchecked and (d["age_h"] if d["age_h"] is not None else 99) > limit:
            tally[status] -= 1
            status = d["status"] = "stale"  # store wouldn't show its price and the post is old: price has likely moved
            tally["stale"] = tally.get("stale", 0) + 1
            log("     ↳ dropped: price not confirmable and post too old")
        if status in ("oos", "dead", "nolink", "nocode", "pricegone", "stale") or (strict and status not in ("in_stock", "live")):
            seen[d["id"]] = now + OOS_RECHECK
        elif len(keep) < cap:
            d["hist"] = history_note(d, prices)
            keep.append(d)

    # 🚨 Steals first — every second counts.
    steals = [d for d in keep if d.get("steal")]
    for d in steals:
        try:
            res = send_steal(d, cfg, args.dry_run, silent=quiet and not steal_loud)
        except Exception as ex:
            log(f"  ! steal alert failed: {ex!r}")
            res = None
        if not res:
            continue
        log(f"  🚨 STEAL sent ({d['steal']}): {d['title'][:60]}")
        seen[d["id"]] = now + SENT_TTL
        keep.remove(d)
        cards[d["id"]] = {"mid": res["mid"], "photo": res["photo"], "chat": res["chat"], "steal": d["steal"], "ts": now,
                          "pub": d.get("pub"), **{k: d.get(k) for k in ("title", "buy", "thread", "store", "price", "watch", "wish")}}
    if not args.feed and not args.radar:
        try:  # re-check older cards AFTER steals are out, so a steal never waits on housekeeping
            sweep_cards(cards, cfg, args.dry_run, before=now)
        except Exception as ex:  # never let housekeeping stop the hunt
            log(f"  ! card sweep failed: {ex!r}")

    instant = [d for d in keep if cfg["categories"].get(d["cat"], {}).get("instant")]
    for d in instant:
        instant_alert(d, cfg, args.dry_run, silent=quiet)
        seen[d["id"]] = now + SENT_TTL
    keep = [d for d in keep if d not in instant]
    # 🎟️ Deals that need a promo code: their own card with the code, never buried in the digest.
    code_deals = [d for d in keep if d.get("codes") and not d.get("watch") and not d.get("wish")]
    n_codes = 0
    for d in code_deals[: cfg.get("code_cards_per_run", 5)]:
        try:
            res = send_card(d, cfg, args.dry_run, parts=code_parts(d, cfg), silent=quiet)
        except Exception as ex:
            log(f"  ! code card failed, leaving it for the digest: {ex!r}")
            res = None
        if not res:
            continue
        n_codes += 1
        seen[d["id"]] = now + SENT_TTL
        keep.remove(d)
        cards[d["id"]] = {"mid": res["mid"], "photo": res["photo"], "ts": now, "pub": d.get("pub"),
                          **{k: d.get(k) for k in ("title", "buy", "thread", "store", "price", "codes")}}
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
    sent = [] if args.radar else digest(keep, cfg, args.dry_run)
    for d in sent:  # deals that didn't fit stay eligible for the next run
        seen[d["id"]] = now + SENT_TTL

    log(f"Checked {len(cands)}: {tally}. Sent {len(steals)} steals + {len(instant)} price mistakes + "
        f"{n_codes} code cards + {n_cards} wish-list cards + {len(sent)} in digest.")
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
            if d["status"] in ("oos", "dead", "nolink", "nocode", "pricegone"):
                continue
            d["hist"] = ""
            res = send_card(d, cfg, False)
            return log(f"Sample card sent: {res}")
    send_text("🦅 No live wish-list deal right now to show as a sample. The next one will arrive as a card.", False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--radar", action="store_true")
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
