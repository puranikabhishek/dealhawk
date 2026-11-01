"""Temporary: test candidate free deal feeds from GitHub's servers."""
import calendar, re, time, feedparser, requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
SD = "https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1"
URLS = {
  "sd search grill": SD + "&q=grill",
  "sd search grill sort=newest": SD + "&q=grill&sort=newest",
  "sd search grill orderby=postdate": SD + "&q=grill&orderby=postdate&sortorder=desc",
  "sd frontpage+q grill": SD + "&mode=frontpage&q=grill",
  "sd frontpage+q patio": SD + "&mode=frontpage&q=patio",
  "sd popdeals": SD + "&mode=popdeals",
  "sd hotdeals forum": "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1&forumchoice%5B%5D=9",
  "dealnews": "https://www.dealnews.com/?rss=1",
  "dealnews home": "https://www.dealnews.com/c196/Home-Garden/?rss=1",
  "bensbargains": "https://bensbargains.com/rss/",
  "reddit r/deals": "https://www.reddit.com/r/deals/new/.rss",
  "woot": "https://www.woot.com/feeds/deals.rss",
}
for name, u in URLS.items():
    try:
        r = requests.get(u, headers=H, timeout=20)
        es = feedparser.parse(r.content).entries
        ages = sorted((time.time() - calendar.timegm(e.published_parsed)) / 3600 for e in es if e.get("published_parsed"))
        span = f"{ages[0]:.0f}h/{ages[len(ages)//2]:.0f}h/{ages[-1]:.0f}h" if ages else "-"
        print(f"\n## {name}: HTTP {r.status_code}, {len(es)} items, age min/med/max {span}")
        for e in es[:2]:
            body = (e.get("summary") or "") + "".join(c.get("value", "") for c in e.get("content", []))
            links = [l for l in re.findall(r'href="([^"]+)"', body) if "http" in l][:3]
            print(f"   - {e.get('title','')[:90]}\n     link={e.get('link','')[:90]}\n     tags={[t.get('term') for t in e.get('tags',[])][:5]} hrefs={[l[:80] for l in links]}")
    except Exception as ex:
        print(f"\n## {name}: ERROR {ex.__class__.__name__}")
    time.sleep(1.5)
