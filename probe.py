"""Temporary: test candidate free deal feeds from GitHub's servers."""
import calendar, time, feedparser, requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
SD = "https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1"
URLS = {f"sd forum {n}": SD + f"&forumchoice%5B%5D={n}" for n in (4, 10, 25, 38, 53)}
URLS.update({
  "sd frontpage ulta": SD + "&mode=frontpage&q=ulta",
  "sd popdeals ulta": SD + "&mode=popdeals&q=ulta",
  "sd search ulta": SD + "&q=ulta",
  "sd search freebie": SD + "&q=freebie",
  "reddit r/freebies": "https://www.reddit.com/r/freebies/new/.rss",
  "dealnews coupons": "https://www.dealnews.com/coupons/?rss=1",
  "dealnews freebies": "https://www.dealnews.com/c1143/Freebies/?rss=1",
  "dealnews electronics c142": "https://www.dealnews.com/c142/Electronics/?rss=1",
  "dealnews health beauty c756": "https://www.dealnews.com/c756/Health-Beauty/?rss=1",
  "dealnews staff picks": "https://www.dealnews.com/f1682/Staff-Pick/?rss=1",
  "dealnews home decor c1071": "https://www.dealnews.com/c1071/Home-Garden/Home-Decor/?rss=1",
  "dealnews furniture c600": "https://www.dealnews.com/c600/Home-Garden/Furniture/?rss=1",
})
for name, u in URLS.items():
    try:
        r = requests.get(u, headers=H, timeout=20)
        f = feedparser.parse(r.content); es = f.entries
        ages = sorted((time.time() - calendar.timegm(e.published_parsed)) / 3600 for e in es if e.get("published_parsed"))
        span = f"{ages[0]:.0f}h/{ages[len(ages)//2]:.0f}h/{ages[-1]:.0f}h" if ages else "-"
        print(f"## {name}: HTTP {r.status_code}, {len(es)} items, ages {span}, feed title={f.feed.get('title','')[:60]!r}")
        for e in es[:3]:
            print(f"     - {e.get('title','')[:95]}")
    except Exception as ex:
        print(f"## {name}: ERROR {ex.__class__.__name__}")
    time.sleep(1.2)
