import requests, feedparser, re, html
from urllib.parse import urlparse
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
F={"TechBargains":"https://www.techbargains.com/rss.xml","9to5Toys":"https://9to5toys.com/feed/","Hip2Save":"https://hip2save.com/feed/",
"DealCatcher":"https://www.dealcatcher.com/rss","Reddit":"https://www.reddit.com/r/deals/new/.rss",
"DealNews":"https://www.dealnews.com/?rss=1","SD Front":"https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"}
skip=re.compile(r"(9to5toys|9to5mac|hip2save|dealcatcher|reddit|redd\.it|dealnews|dlnws|slickdeals|sdimg|techbargains|facebook|twitter|pinterest|instagram|youtube|wp\.com|gravatar|x\.com|apple\.com/app|google\.com/store/apps)")
for n,u in F.items():
    e=feedparser.parse(requests.get(u,headers=UA,timeout=20).content).entries[:4]
    print("=====",n)
    for x in e:
        summ=(x.get("summary") or "")+" ".join(c.get("value","") for c in x.get("content",[]))
        hrefs=[html.unescape(h) for h in re.findall(r'href="([^"]+)"',summ)]
        out=[h for h in hrefs if not skip.search(urlparse(h).netloc)]
        print("-",x.get("title","")[:60],"| link:",x.get("link","")[:70])
        print("   feed hrefs:",[h[:110] for h in out[:4]])
        if n in ("9to5Toys","Hip2Save","DealCatcher"):
            try:
                p=requests.get(x.link,headers=UA,timeout=15).text
                ph=[html.unescape(h) for h in re.findall(r'href="([^"]+)"',p)]
                ph=[h for h in ph if h.startswith("http") and not skip.search(urlparse(h).netloc)]
                print("   page hrefs:",list(dict.fromkeys(h[:110] for h in ph))[:8])
                # also hip2save/dealcatcher internal go links
                go=[h for h in re.findall(r'href="([^"]+)"',p) if re.search(r"/go/|/click|/out/|/redirect|/visit",h)]
                print("   go links:",list(dict.fromkeys(go))[:4])
            except Exception as ex: print("   page ERR",ex)
    # resolve first out link
    for x in e[:2]:
        summ=(x.get("summary") or "")+" ".join(c.get("value","") for c in x.get("content",[]))
        hrefs=[html.unescape(h) for h in re.findall(r'href="([^"]+)"',summ) if not skip.search(urlparse(html.unescape(h)).netloc)]
        if hrefs:
            try:
                r=requests.get(hrefs[0],headers=UA,timeout=15,allow_redirects=True)
                print("   RESOLVE",hrefs[0][:60],"->",r.status_code,r.url[:120],"| hops",[h.headers.get("location","")[:60] for h in r.history][:4])
            except Exception as ex: print("   RESOLVE ERR",ex)
