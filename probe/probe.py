import re, html, requests, feedparser
s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
f = feedparser.parse(s.get("https://www.dealcatcher.com/rss", timeout=20).content)
for e in f.entries[:3]:
    print("\n", e.title[:70], e.link)
    print(" summary links:", re.findall(r'href="([^"]+)"', e.get("summary",""))[:4])
    t = s.get(e.link, timeout=20).text
    links = [html.unescape(x) for x in re.findall(r'href="([^"]+)"', t) if re.search(r"go|redirect|out|click|amazon|walmart|target|bestbuy", x, re.I)]
    print(" page links:", links[:10])
    for l in links:
        if "dealcatcher" in l and re.search(r"go|redirect|out|click", l):
            r = s.get(l if l.startswith("http") else "https://www.dealcatcher.com" + l, timeout=20, allow_redirects=False)
            print("  ->", r.status_code, r.headers.get("location","")[:200]); break
