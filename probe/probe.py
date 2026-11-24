import re, html, requests, feedparser, json
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
s = requests.Session(); s.headers.update(UA)
print("== REDDIT ==")
for u in ["https://www.reddit.com/r/deals/new/.rss", "https://old.reddit.com/r/deals/new/.rss",
          "https://www.reddit.com/r/deals/new.json?limit=25", "https://api.reddit.com/r/deals/new?limit=25",
          "https://www.reddit.com/r/deals/new/.rss?limit=25"]:
    for ua in [UA["User-Agent"], "dealhawk/1.0 (personal deal alerts)"]:
        try:
            r = requests.get(u, headers={"User-Agent": ua}, timeout=20)
            print(r.status_code, len(r.content), u, ua[:20])
        except Exception as e: print("ERR", u, e)
print("== SLICKDEALS ==")
f = feedparser.parse(s.get("https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1", timeout=20).content)
for e in f.entries[:4]:
    print("\nTHREAD", e.link[:120])
    summ = e.get("summary","")
    clicks = [html.unescape(x) for x in re.findall(r'href="([^"]*slickdeals\.net/click[^"]*)"', summ)]
    print(" feed click:", clicks[:1])
    try:
        p = s.get(e.link, timeout=20); print(" page", p.status_code, len(p.text))
        t = p.text
        for k in ["dealUrl","storeUrl","outclickUrl","buyUrl","merchantUrl","productUrl","\"url\"","data-href","u2="]:
            m = re.findall(k + r'[^\n]{0,200}', t)
            if m: print("  ", k, "->", [x[:200] for x in m[:2]])
        for m in re.findall(r'https?:(?:\\?/){2}(?:www\.)?(?:amazon|walmart|target|bestbuy|homedepot|lowes|costco|wayfair|ebay|newegg|woot)\.com[^"\'\s<>]{0,150}', t)[:5]:
            print("   storeurl", m)
        sd = re.search(r'https?:\\?/\\?/slickdeals\.net\\?/click[^"\'\s<>]+', t)
        c = html.unescape(sd.group(0).replace("\\/","/")) if sd else (clicks[0] if clicks else None)
        if c:
            r = s.get(c, timeout=20, allow_redirects=False); print(" click", r.status_code, r.headers.get("location","")[:200], len(r.text))
            print("  body", re.sub(r"\s+"," ",r.text[:600]))
            r2 = s.get(c, timeout=20, allow_redirects=True); print(" follow", r2.status_code, r2.url[:200], [h.status_code for h in r2.history])
    except Exception as ex: print(" ERR", ex)
