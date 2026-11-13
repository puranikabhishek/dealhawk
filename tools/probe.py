import sys, re, json, yaml; sys.argv=["x"]; sys.path.insert(0,".")
import dealhawk as dh
from concurrent.futures import ThreadPoolExecutor
cfg=dh.apply_settings(yaml.safe_load(open("config.yaml")), json.load(open("data/settings.json")))
cands, ids = [], set()
for name, entries in dh.all_feeds(cfg):
    for e in entries:
        d=dh.to_deal(e,name); pk=dh.product_key(d["title"])
        if d["id"] in ids or pk in ids: continue
        ids.update((d["id"],pk)); d["forced"]=None; d["feed_max_age"]=None
        d["watch"], d["cat"] = dh.watch_hit(d,cfg), dh.categorize(d,cfg); d["wish"]=dh.wish_kw(d,cfg)
        if dh.passes(d,cfg)[0] and (d["watch"] or d["cat"]): cands.append(d)
cands=cands[:70]
for d in cands: d["tp"]=d["price"]
def go(d):
    try: d["status"],d["why"]=dh.resolve_and_verify(d)
    except Exception as ex: d["status"],d["why"]="err",repr(ex)
    return d
with ThreadPoolExecutor(8) as ex: res=list(ex.map(go,cands))
n=0
for d in res:
    if d.get("title_price") is None and d["status"]!="pricegone": continue
    n+=1
    print(f"AUDIT {d['status']:9} post ${d['tp'] or 0:8.2f} → sent ${d['price'] or 0:8.2f} | {d['title'][:58]} [{dh.host(d['buy'])}] {d.get('price_note','')} {d['why'] if d['status']=='pricegone' else ''}")
print("AUDIT store price read for",n,"of",len(res),"| statuses:",{s:sum(1 for d in res if d['status']==s) for s in set(d['status'] for d in res)})
