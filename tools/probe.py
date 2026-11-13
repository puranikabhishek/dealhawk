import sys, re, html; sys.argv=["x"]; sys.path.insert(0,".")
import dealhawk as dh
U={"InstantPot TB":"https://www.amazon.com/gp/product/B08PPZWNCV/ref=ox_sc_act_title_1?smid=ATVPDKIKX0DER&psc=1",
   "Monster40W TB":"https://www.amazon.com/gp/product/B0HGR1PLMN/ref=ox_sc_act_title_1",
   "Roborock":"https://www.amazon.com/gp/product/B0H1W9CCYS/ref=ox_sc_act_title_1?smid=A2NZXCP1D8DIIH&th=1",
   "iPad":"https://www.amazon.com/gp/product/B0DZ77D5HL/"}
for n,u in U.items():
    r=dh.HTTP.get(u,timeout=20); b=r.text
    print("=====",n,r.status_code,len(b), "blocked" if dh.BLOCK_TEXT.search(b[:20000]) else "")
    for anchor in ('corePriceDisplay_desktop_feature_div','corePrice_feature_div','apex_desktop','buybox','priceToPay','a-price-whole'):
        i=b.find(anchor); print("  ",anchor,i)
    i=b.find('corePriceDisplay_desktop_feature_div')
    if i<0: i=b.find('corePrice_feature_div')
    if i>=0:
        seg=b[i:i+6000]
        print("   offscreens:",re.findall(r'class="a-offscreen">([^<]+)<',seg)[:6])
        print("   priceToPay:",re.findall(r'priceToPay[^>]*>.{0,300}',seg,re.S)[:1])
        print("   classes:",re.findall(r'class="(a-price[^"]*)"',seg)[:6])
        print("   text:",re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",seg))[:500])
    print("   priceAmount:",re.findall(r'"priceAmount"\s*:\s*([\d.]+)',b)[:5])
    print("   deliver to:",re.findall(r'glow-ingress-line2[^>]*>([^<]+)',b)[:1], "| coupon:", bool(re.search(r"couponBadge|Apply \$?\d+ coupon|clip coupon",b,re.I)), "| deal badge:", re.findall(r'(Limited time deal|Lightning Deal|Prime Big Deal|Black Friday Deal|Prime exclusive)',b)[:3])
