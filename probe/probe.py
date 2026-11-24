import re, requests, feedparser
s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
f = feedparser.parse(s.get("https://www.dealcatcher.com/rss", timeout=20).content)
for e in f.entries[:6]:
    t = s.get(e.link, timeout=20).text
    print("\n", e.title[:60])
    for pat in [r'asin[^,]{0,40}', r'(?:amazon|walmart|target|bestbuy|homedepot)\.com[^"\'\s<>,]{0,120}', r'"(?:url|link|buyUrl|dealUrl|storeUrl|merchantUrl|affiliateUrl)":"[^"]{0,160}"']:
        print("  ", pat[:12], sorted(set(re.findall(pat, t, re.I)))[:5])
