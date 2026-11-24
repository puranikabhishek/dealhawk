import re, html, json, requests, feedparser
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
s = requests.Session(); s.headers.update(UA)
STORE = r'(?:amazon|walmart|target|bestbuy|homedepot|lowes|costco|wayfair|ebay|newegg|woot|samsclub|kohls|macys|dell|lenovo|bhphotovideo)\.com'
def nuxt(t):
    m = re.search(r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>', t, re.S)
    if not m: return None
    return json.loads(m.group(1))
def deref(arr, v, depth=0):
    if depth > 3: return v
    if isinstance(v, int) and 0 <= v < len(arr): return arr[v]
    return v
f = feedparser.parse(s.get("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1", timeout=20).content)
for e in f.entries[:6]:
    print("\nTHREAD", e.title[:90])
    t = s.get(e.link, timeout=20).text
    arr = nuxt(t)
    if not arr: print(" no nuxt"); continue
    for i, o in enumerate(arr):
        if isinstance(o, dict) and "outclickUrl" in o and "storeName" in o:
            for k in ("outclickUrl", "outclickAttrs", "storeName", "storeUrl", "ctaButtonText"):
                if k in o:
                    v = deref(arr, o[k])
                    if isinstance(v, dict): v = {kk: deref(arr, vv) for kk, vv in v.items()}
                    print("  ", k, "=", str(v)[:400])
            break
    body = next((deref(arr, o["bodyHtml"]) for o in arr if isinstance(o, dict) and "bodyHtml" in o), "")
    print("  body links:", [html.unescape(x)[:160] for x in re.findall(r'href="([^"]+)"', str(body))][:5])
    strs = [x for x in arr if isinstance(x, str) and re.search(STORE, x)]
    print("  store strings in payload:", [x[:160] for x in strs[:6]])
    for k in ("asin", "upc", "sku", "productUrl", "merchantUrl", "landingUrl", "destinationUrl"):
        hits = [x for x in arr if isinstance(x, dict) and k in x]
        if hits: print("  key", k, str({kk: deref(arr, vv) for kk, vv in hits[0].items()})[:300])
print("\n== ALT FEEDS ==")
for u in ["https://www.bensbargains.com/rss/", "https://www.bradsdeals.com/rss", "https://www.dealsplus.com/rss/hot_deals",
          "https://kinja-deals.com/rss", "https://www.theverge.com/rss/deals/index.xml", "https://www.woot.com/blog/feed",
          "https://www.dealsea.com/rss", "https://feeds.feedburner.com/dealsea", "https://www.dealcatcher.com/rss",
          "https://www.wired.com/feed/category/gear/deals/latest/rss", "https://www.engadget.com/rss.xml?tag=deals",
          "https://lemmy.world/feeds/c/deals.xml?sort=New", "https://www.reddit.com/r/buildapcsales/new/.rss"]:
    try:
        r = s.get(u, timeout=20); n = len(feedparser.parse(r.content).entries)
        e0 = feedparser.parse(r.content).entries[:1]
        print(r.status_code, n, u, "|", (e0[0].title[:60] if e0 else ""))
    except Exception as ex: print("ERR", u, ex.__class__.__name__)
