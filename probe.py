import re, html, json, feedparser, requests, yaml
import dealhawk as dh
cfg = yaml.safe_load(open("config.yaml"))
pat = re.compile(r"headphone|earbud|bose|robot vac|patio|plaud", re.I)
n = 0
for f in cfg["feeds"][:7]:
    r = dh.HTTP.get(f["url"], timeout=20)
    fp = feedparser.parse(r.content)
    hits = [e for e in fp.entries if pat.search(e.get("title", ""))][:2] or fp.entries[:1]
    for e in hits[:2]:
        n += 1
        print("=" * 30, f["name"])
        print("KEYS", sorted(e.keys()))
        print("TITLE", e.get("title")); print("LINK", e.get("link")); print("ID", e.get("id"))
        print("TAGS", [t.get("term") for t in e.get("tags", [])])
        for k in ("media_thumbnail", "media_content", "enclosures", "links"):
            if e.get(k): print(k.upper(), json.dumps(e.get(k))[:500])
        s = (e.get("summary") or "") + " ".join(c.get("value", "") for c in e.get("content", []))
        print("SUMMARY", s[:1800])
        print("IMGS", re.findall(r'<img[^>]+src="([^"]+)"', s)[:3])
        print("HREFS", [html.unescape(x)[:160] for x in re.findall(r'href="([^"]+)"', s)][:6])
        d = dh.to_deal(e, f["name"])
        print("BUY", d["buy"], "| STORE", repr(d["store"]), "| price", d["price"], d["reg"], d["pct"], "thumbs", d["thumbs"])
        try:
            t = dh.HTTP.get(d["thread"], timeout=15, allow_redirects=True)
            body = t.text
            print("THREAD", t.status_code, t.url[:120], len(body))
            print("  og:image", re.findall(r'property="og:image"[^>]+content="([^"]+)"', body)[:1])
            outs = [html.unescape(x) for x in re.findall(r'href="([^"]+)"', body) if re.search(dh.STORE_DOMAINS + r"|/lw/click|/click\?|u2=", x)]
            print("  OUTLINKS", [o[:200] for o in outs[:4]])
            for o in outs[:1]:
                o = ("https://www.dealnews.com" + o) if o.startswith("/") else o
                rr = dh.HTTP.get(o, timeout=15, allow_redirects=True)
                print("  FOLLOW", rr.status_code, rr.url[:200], [h.headers.get("location", "")[:150] for h in rr.history][:3])
        except Exception as ex:
            print("THREAD ERR", ex)
