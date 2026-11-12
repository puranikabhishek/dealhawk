import sys, re, html; sys.argv=["x"]; sys.path.insert(0,".")
import dealhawk as dh, feedparser
es=dh.fetch_feed("https://www.dealnews.com/c196/Home-Garden/?rss=1")[:3]
for e in es:
    d=dh.to_deal(e,"DealNews"); print("==",d["title"][:60], d["thread"][:80])
    src,page=dh.source_check(d["thread"]); print("src",src,len(page))
    clicks=re.findall(r'href="([^"]*?/lw/click[^"]*)"', page[:400000]); print("clicks",clicks[:3])
    allc=re.findall(r'[^"\s]*lw/click[^"\s<]*', page); print("any lw/click",allc[:3])
    outs=[h for h in re.findall(r'href="(https?://[^"]+)"',page) if "dealnews" not in h][:8]; print("outs",outs)
    for c in allc[:1]:
        u=c if c.startswith("http") else "https://www.dealnews.com"+html.unescape(c)
        r=dh.HTTP.get(u,timeout=15); print("click page",r.status_code,r.url[:90],re.findall(r"location[^;]{0,200}",r.text[:4000])[:2])
        print("follow ->",dh.follow(u)); r2=dh.HTTP.get(u,timeout=15,allow_redirects=True); print("hops",[h.headers.get("location","")[:120] for h in r2.history], r2.url[:120])
