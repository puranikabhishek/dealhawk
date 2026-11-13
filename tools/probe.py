import sys, re; sys.argv=["x"]; sys.path.insert(0,".")
import dealhawk as dh
for u in ["https://www.amazon.com/gp/product/B08PPZWNCV/ref=ox_sc_act_title_1?smid=ATVPDKIKX0DER&psc=1","https://www.amazon.com/dp/B0FWD1L7QN","https://www.amazon.com/gp/product/B0HGR1PLMN/"]:
    r=dh.HTTP.get(u,timeout=20); b=r.text
    print("=====",u[-40:],r.status_code,len(b),"blocked" if dh.BLOCK_TEXT.search(b[:20000]) else "", "avail:",dh.availability(b))
    for m in list(re.finditer(r'\$\s?\d[\d,]*\.\d{2}', b))[:14]:
        ctx=re.sub(r"\s+"," ",b[max(0,m.start()-160):m.end()+60])
        print("   ·",ctx[-230:])
    print("   ids:",sorted(set(re.findall(r'id="([^"]*(?:[Pp]rice|buybox|apex)[^"]*)"',b)))[:25])
