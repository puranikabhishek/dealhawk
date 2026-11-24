import sys, feedparser; sys.path.insert(0, ".")
import dealhawk as dh
n = hit = 0
for fu in ["https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
           "https://slickdeals.net/newsearch.php?q=patio&searcharea=deals&searchin=first&rss=1",
           "https://slickdeals.net/newsearch.php?q=headphones&searcharea=deals&searchin=first&rss=1"]:
    for e in feedparser.parse(dh.HTTP.get(fu, timeout=20).content).entries[:10]:
        n += 1
        d = dh.to_deal(e, "Slickdeals"); d["tl"] = d["title"].lower()
        src, page = dh.source_check(d["thread"])
        links = dh.slickdeals_store_links(page, d)
        final = dh.find_store_link(d, page)
        hit += bool(final and "slickdeals" not in final)
        print(f"{'OK ' if final and 'slickdeals' not in final else 'SD '} {d['title'][:55]:55} -> {final}")
print(f"direct store links: {hit}/{n}")
