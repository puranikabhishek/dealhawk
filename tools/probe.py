import requests, feedparser, time, calendar, sys
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
C={
"9to5Toys":"https://9to5toys.com/feed/",
"9to5Toys Home":"https://9to5toys.com/guides/home-goods/feed/",
"BensBargains":"https://bensbargains.com/rss/",
"BensBargains2":"https://www.bensbargains.com/rss.xml",
"TechBargains":"https://www.techbargains.com/rss.xml",
"TechBargains2":"https://www.techbargains.com/rss",
"BradsDeals":"https://www.bradsdeals.com/deals/feed",
"BradsDeals2":"https://www.bradsdeals.com/rss",
"Reddit deals":"https://www.reddit.com/r/deals/new/.rss",
"Reddit deals old":"https://old.reddit.com/r/deals/new/.rss",
"Reddit amazondeals":"https://old.reddit.com/r/amazondeals/new/.rss",
"Reddit DealsReddit":"https://old.reddit.com/r/DealsReddit/new/.rss",
"Hip2Save":"https://hip2save.com/feed/",
"KrazyCouponLady":"https://thekrazycouponlady.com/feed",
"DealCatcher":"https://www.dealcatcher.com/rss",
"DealsPlus":"https://www.dealsplus.com/rss/hot",
"Verge deals":"https://www.theverge.com/rss/deals/index.xml",
"Wirecutter deals":"https://www.nytimes.com/wirecutter/deals/feed/",
"Mashable deals":"https://mashable.com/feeds/rss/deals",
"Dealmoon":"https://www.dealmoon.com/rss/en",
"GottaDeal":"https://www.gottadeal.com/rss.xml",
"Engadget deals":"https://www.engadget.com/deals/rss.xml",
"ZDNet deals":"https://www.zdnet.com/topic/deals/rss.xml",
"ToolGuyd deals":"https://toolguyd.com/deals/feed/",
"DealNews Furniture":"https://www.dealnews.com/c286/Home-Garden/Furniture/?rss=1",
"Slickdeals Home":"https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1&forumchoice%5B%5D=9&q=home",
}
for n,u in C.items():
    try:
        r=requests.get(u,headers=UA,timeout=20); e=feedparser.parse(r.content).entries
        ages=[(time.time()-calendar.timegm(x.get("published_parsed") or x.get("updated_parsed")))/3600 for x in e if x.get("published_parsed") or x.get("updated_parsed")]
        print(f"{n:22} {r.status_code} n={len(e):3} newest={min(ages):.1f}h" if ages else f"{n:22} {r.status_code} n={len(e)}", "|", (e[0].get("title","")[:70] if e else ""), "|", (e[0].get("link","")[:80] if e else ""))
        if e:
            l=e[0].get("link","")
            try: print("    page:",requests.get(l,headers=UA,timeout=15).status_code)
            except Exception as x: print("    page ERR",x.__class__.__name__)
    except Exception as x: print(f"{n:22} ERR {x.__class__.__name__}")
