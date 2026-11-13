import sys, re, json, time, yaml; sys.argv=["x"]; sys.path.insert(0,".")
import dealhawk as dh
from concurrent.futures import ThreadPoolExecutor
def old_parse(t):
    m=re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", t); return dh.money(m.group(1)) if m else None
cfg=dh.apply_settings(yaml.safe_load(open("config.yaml")), json.load(open("data/settings.json")))
cands, ids = [], set()
for name, entries in dh.all_feeds(cfg):
    for e in entries:
        d=dh.to_deal(e,name); pk=dh.product_key(d["title"])
        if d["id"] in ids or pk in ids: continue
        ids.update((d["id"],pk))
        d["forced"]=None; d["feed_max_age"]=None
        d["watch"], d["cat"] = dh.watch_hit(d,cfg), dh.categorize(d,cfg); d["wish"]=dh.wish_kw(d,cfg)
        ok,_=dh.passes(d,cfg)
        if ok and (d["watch"] or d["cat"]): cands.append(d)
cands=cands[:60]
for d in cands: d["old"]=old_parse(d["title"]+" "+d.get("plain","")); d["newparse"]=d["price"]
def go(d):
    try: d["status"],d["why"]=dh.resolve_and_verify(d)
    except Exception as ex: d["status"],d["why"]="err",repr(ex)
    return d
with ThreadPoolExecutor(8) as ex: res=list(ex.map(go,cands))
print("AUDIT  status | OLD alert $ | NEW alert $ | store $ | title")
bad=0
for d in res:
    sp=None
    if d["status"]=="pricegone": sp=float(re.findall(r"store now \$([\d,.]+)",d["why"])[0].replace(",",""))
    elif d.get("title_price") is not None or d["price"]!=d["newparse"]: sp=d["price"]
    flag=""
    if sp and d["old"] and abs(sp-d["old"])>max(1,0.03*d["old"]): flag="❗OLD WRONG"; bad+=1
    print(f"AUDIT {d['status']:9} | {d['old'] or 0:8.2f} | {(d['price'] or 0) if d['status']!='pricegone' else 0:8.2f} | {sp or 0:8.2f} | {flag:10} {d['title'][:60]} [{dh.host(d['buy'])}] {d.get('price_note','')}")
print("AUDIT old-wrong:",bad,"of",len(res),"with store price:",sum(1 for d in res if d.get('title_price') is not None or d['status']=='pricegone'))
