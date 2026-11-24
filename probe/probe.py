import re, html, json, requests, feedparser
from urllib.parse import urlparse, parse_qs
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36"}
s = requests.Session(); s.headers.update(UA)
feeds = ["https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
         "https://slickdeals.net/newsearch.php?q=patio&searcharea=deals&searchin=first&rss=1",
         "https://slickdeals.net/newsearch.php?q=headphones&searcharea=deals&searchin=first&rss=1"]
n = 0
for fu in feeds:
    for e in feedparser.parse(s.get(fu, timeout=20).content).entries[:12]:
        n += 1
        summ = html.unescape(e.get("summary", ""))
        trds = [(parse_qs(urlparse(h).query).get("trd") or [""])[0] for h in re.findall(r'href="([^"]+)"', summ) if "click" in h]
        page = s.get(e.link, timeout=20).text
        ptrds = [(parse_qs(urlparse(html.unescape(h)).query).get("trd") or [""])[0]
                 for h in re.findall(r'https://slickdeals\.net/click\?[^"\'\s<>\\]+', page)]
        urlish = sorted({t for t in trds + ptrds if re.match(r"https?\W", t)})
        anchors = sorted({html.unescape(a) for a in re.findall(r'>(https?://(?:www\.)?[a-z0-9.-]+\.com/[^<]{5,})</a>', page)})[:3]
        print(f"\n{e.title[:70]}\n  feed trd: {trds[:3]}\n  url-trd: {urlish[:3]}\n  anchors: {anchors}")
print("total", n)
