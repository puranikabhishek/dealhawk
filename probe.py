import re, html, json, feedparser
import dealhawk as dh
def grab(u):
    r = dh.HTTP.get(u, timeout=20); return r.status_code, r.text
def ctx(body, pat, n=6, w=110):
    out = []
    for m in re.finditer(pat, body, re.I):
        s = body[max(0, m.start()-w): m.end()+w].replace("\n", " ")
        out.append(re.sub(r"\s+", " ", s))
        if len(out) >= n: break
    return out
urls = {"OLD": "https://slickdeals.net/f/18814108", "LIVE": "https://slickdeals.net/f/20295609"}
fp = feedparser.parse(dh.HTTP.get("https://slickdeals.net/newsearch.php?q=bose+quietcomfort&searcharea=deals&searchin=first&rss=1", timeout=20).content)
for e in fp.entries[:8]:
    print("SEARCH", e.get("published", "")[:22], "|", e.get("title", "")[:90], "|", e.get("link", "")[:70], "| tags", [t.get("term") for t in e.get("tags", [])])
for i, e in enumerate([e for e in fp.entries if "124" in e.get("title", "")][:2]):
    urls[f"BOSE{i}"] = e.link
for k, u in urls.items():
    sc, b = grab(u)
    print("=" * 20, k, sc, len(b), u[:90])
    print(" count 'xpired':", len(re.findall("xpired", b, re.I)), "| diavail:", set(re.findall(r"diavail-\w+", b)), "| dipgavail:", set(re.findall(r"dipgavail-\w+", b)))
    for c in ctx(b, r"xpired"): print("  EXP>", c)
    for c in ctx(b, r'"availability"|InStock|OutOfStock|SoldOut', 3): print("  AVAIL>", c)
    for c in ctx(b, r'"(?:dealStatus|status|isLive|isDead|dealState)"\s*:', 4, 60): print("  STATUS>", c)
    for c in ctx(b, r'og:(?:title|description)"', 2, 10): print("  OG>", c[:260])
    lds = re.findall(r'<script type="application/ld\+json">(.*?)</script>', b, re.S)
    for l in lds[:3]: print("  LD>", re.sub(r"\s+", " ", l)[:600])
fp = feedparser.parse(dh.HTTP.get("https://www.dealnews.com/c142/Electronics/?rss=1", timeout=20).content)
for e in fp.entries[:4]:
    print("DN", {k: e.get(k) for k in e.keys() if k.startswith("dealnews_")}, e.get("published"))
sc, b = grab("https://www.dealnews.com/lw/click.html?20,2,22383984")
print("DNCLICK", sc, len(b), re.sub(r"\s+", " ", b[:700]))
print(" urls:", [x[:200] for x in re.findall(r'https?://[^"\'<> ]+', b) if "dealnews" not in x and "dlnws" not in x][:8])
