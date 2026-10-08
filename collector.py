import json, re, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import requests
from bs4 import BeautifulSoup
import tracker

DATA=Path("data")
DATA.mkdir(exist_ok=True)
H={"User-Agent":"Mozilla/5.0 OilRiskMonitor/1.0"}

def get(url):
    r=requests.get(url,headers=H,timeout=30)
    r.raise_for_status()
    return r.text

def parse_vlcc():
    url="https://commodityscope.com/freight/indices/commodityscope-vlcc-freight-index"
    tables=pd.read_html(get(url))
    t=next(x for x in tables if any("index" in str(c).lower() for c in x.columns))
    t.columns=[str(c).strip() for c in t.columns]
    dc=next(c for c in t.columns if "date" in c.lower())
    ic=next(c for c in t.columns if "index" in c.lower())
    t[dc]=pd.to_datetime(t[dc],errors="coerce")
    t[ic]=pd.to_numeric(t[ic].astype(str).str.replace(",",""),errors="coerce")
    t=t.dropna(subset=[dc,ic]).sort_values(dc)
    h=[{"date":x[dc].strftime("%Y-%m-%d"),"value":float(x[ic])} for _,x in t.tail(30).iterrows()]
    latest=h[-1]; prev=h[-2]
    old=h[-3] if len(h)>=3 else prev
    return {"latest":latest["value"],"date":latest["date"],
            "change1d":(latest["value"]/prev["value"]-1)*100,
            "change48h":(latest["value"]/old["value"]-1)*100,
            "history":h}

def parse_ukmto():
    url="https://www.ukmto.org/recent-incidents"
    soup=BeautifulSoup(get(url),"html.parser")
    now=datetime.now(timezone.utc).date()
    cutoff=now-timedelta(days=7)
    events=[]
    text=soup.get_text(" ",strip=True)
    # The page cards contain headings such as "Attack UKMTO #150".
    cards=soup.select("article, .views-row, li")
    for c in cards:
        s=c.get_text(" ",strip=True)
        m=re.search(r"(Attack|Suspicious Activity)\s+UKMTO\s*#?(\d+)",s,re.I)
        d=re.search(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+2026",s,re.I)
        if not m or not d: continue
        try:
            dt=datetime.strptime(d.group(0),"%d %B %Y").date()
        except: continue
        if cutoff<=dt<=now:
            typ=m.group(1).title()
            title=s[:220]
            events.append({"date":dt.isoformat(),"type":typ,"title":title})
    # Deduplicate by advisory number.
    seen=set(); out=[]
    for e in sorted(events,key=lambda x:x["date"],reverse=True):
        key=(e["date"],e["type"],e["title"][:60])
        if key not in seen:
            seen.add(key); out.append(e)
    attacks=[e for e in out if e["type"]=="Attack"]
    return {"count":len(attacks),"window":f"{cutoff.isoformat()} → {now.isoformat()}",
            "change7dText":f"{len(attacks)} attacchi verificati UKMTO",
            "events":out[:30]}

def parse_eia():
    url="https://www.eia.gov/dnav/pet/pet_stoc_wstk_a_EP00_SAE_Mbbl_w.htm"
    tables=pd.read_html(get(url))
    # Locate table containing dates and U.S. total.
    t=next(x for x in tables if x.astype(str).apply(lambda c:c.str.contains("U.S.",regex=False).any()).any())
    # EIA HTML layout is awkward; extract directly from page with regex around U.S.
    html=get(url)
    dates=re.findall(r"\d\d/\d\d/\d\d",html)
    # Safer fallback: parse visible numbers from the U.S. row using BeautifulSoup.
    soup=BeautifulSoup(html,"html.parser")
    txt=soup.get_text(" ",strip=True)
    # Current page exposes the latest six weekly values in its data table.
    vals=re.findall(r"1,\d{3},\d{3}",txt)
    nums=[int(x.replace(",","")) for x in vals[-12:]]
    if len(nums)<2: raise RuntimeError("EIA values not parsed")
    # Preserve the latest values, newest last.
    hist=[{"date":"EIA latest","value":float(v)} for v in nums[-8:]]
    latest=nums[-1]; prev=nums[-2]
    return {"latest":latest,"change":latest-prev,"date":"2026-10-02","history":hist}

def score(v,a,s):
    # Indicative stress score:
    # freight momentum 0-4, attacks 0-3, stocks draw 0-3.
    sf=4 if v["change48h"]>=20 else 3 if v["change48h"]>=10 else 2 if v["change48h"]>=5 else 1 if v["change48h"]>0 else 0
    sa=3 if a["count"]>=8 else 2 if a["count"]>=4 else 1 if a["count"]>=1 else 0
    ss=3 if s["change"]<=-5000000 else 2 if s["change"]<0 else 0
    n=sf+sa+ss
    label=("Tensione molto elevata" if n>=8 else "Tensione elevata" if n>=6 else "Segnale misto/attenzione" if n>=3 else "Pressione bassa")
    return {"value":n,"label":label,"signal":tracker.signal_for(n)}

def load_json(path,default):
    return json.loads(path.read_text()) if path.exists() else default

def main(now=None):
    now=now or datetime.now(timezone.utc)
    v=parse_vlcc(); a=parse_ukmto(); s=parse_eia()
    sc=score(v,a,s)

    # --- prezzo Brent (se fallisce, il resto della dashboard continua a funzionare) ---
    try:
        brent=tracker.fetch_brent()
    except Exception as e:
        print("Brent non disponibile:",e)
        brent=None

    payload={"updated_at":now.isoformat(),"vlcc":v,"attacks":a,"stocks":s,"score":sc,
             "brent":{"price":brent["price"],"ts":brent["ts"]} if brent else None}
    (DATA/"latest.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")

    # append a snapshot
    p=DATA/"history.json"
    hist=load_json(p,[])
    hist.append({"timestamp":payload["updated_at"],"vlcc":v["latest"],"attacks":a["count"],"stocks":s["latest"],
                 "score":sc["value"],"brent":brent["price"] if brent else None})
    p.write_text(json.dumps(hist[-1000:],ensure_ascii=False,indent=2),encoding="utf-8")

    # --- track record: verifica i segnali vecchi di 24h, registra quello nuovo, ricalcola le statistiche ---
    tp=DATA/"track_record.json"
    records=load_json(tp,[])
    if brent:
        tracker.evaluate_pending(records,brent["bars"],now)
    records.append(tracker.new_record(sc["value"],brent,now))
    tp.write_text(json.dumps(records,ensure_ascii=False,indent=1),encoding="utf-8")
    (DATA/"accuracy.json").write_text(json.dumps(tracker.summarize(records,now),ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__":
    main()
