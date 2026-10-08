import json, re, os
from io import StringIO
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
    tables=pd.read_html(StringIO(get(url)))
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

MONTHS="January|February|March|April|May|June|July|August|September|October|November|December"
UKMTO_HEAD=re.compile(r"(Attack|Suspicious Activity)\s+UKMTO\s*#?\s*(\d+)",re.I)
UKMTO_DATE=re.compile(r"(\d{1,2})\s+("+MONTHS+r")\s+(\d{4})",re.I)

def get_rendered_text(url,wait_s=30):
    """La lista incidenti di UKMTO viene caricata via JavaScript (l'HTML statico dice solo '0 reports'):
    serve un browser vero che esegua la pagina. Non uso 'networkidle': molte pagine non smettono mai
    di fare richieste (analytics, polling) e il caricamento andrebbe in timeout."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch()
        try:
            pg=b.new_page(user_agent=H["User-Agent"])
            pg.goto(url,wait_until="domcontentloaded",timeout=60000)
            try:
                # aspetto che compaia almeno un incidente ("Attack UKMTO #123") o che il contatore smetta di dire 0
                pg.wait_for_function(
                    r"() => { const t=document.body.innerText; return /UKMTO\s*#\s*\d+/i.test(t) || (/\d+\s*reports/i.test(t) && !/(^|\s)0\s*reports/i.test(t)); }",
                    timeout=wait_s*1000)
            except Exception:
                pass
            pg.wait_for_timeout(1500)
            text=pg.inner_text("body")
        finally:
            b.close()
    if re.search(r"just a moment|checking your browser|verify you are human|access denied",text[:1500],re.I):
        (DATA/"ukmto_debug.txt").write_text(text[:3000],encoding="utf-8")
        raise RuntimeError("UKMTO ha bloccato il browser automatico (pagina di verifica anti-bot)")
    return text

def ukmto_events(text,today,days=7):
    cutoff=today-timedelta(days=days)
    heads=list(UKMTO_HEAD.finditer(text))
    out=[];seen=set()
    for i,m in enumerate(heads):
        nxt=heads[i+1].start() if i+1<len(heads) else len(text)
        block=text[m.start():min(nxt,m.start()+1200)]
        # la data della card è quella più vicina al titolo, sia che segua sia che preceda
        after=UKMTO_DATE.search(text,m.end(),min(nxt,m.end()+400))
        prev=list(UKMTO_DATE.finditer(text,max(0,m.start()-150),m.start()))
        cands=[]
        if after: cands.append((after.start()-m.end(),after))
        if prev: cands.append((m.start()-prev[-1].end(),prev[-1]))
        if not cands or m.group(2) in seen: continue
        d=min(cands,key=lambda c:c[0])[1]
        try: dt=datetime.strptime(" ".join(d.groups()),"%d %B %Y").date()
        except ValueError: continue
        if cutoff<=dt<=today:
            seen.add(m.group(2))
            out.append({"date":dt.isoformat(),"type":m.group(1).title(),"title":re.sub(r"\s+"," ",block).strip()[:900]})
    return sorted(out,key=lambda x:x["date"],reverse=True),cutoff

def parse_ukmto(url="https://www.ukmto.org/recent-incidents"):
    now=datetime.now(timezone.utc).date()
    text=get_rendered_text(url)
    out,cutoff=ukmto_events(text,now)
    if not out:
        # nessun evento: salvo il testo renderizzato per capire se la pagina è cambiata o se davvero non ci sono eventi
        (DATA/"ukmto_debug.txt").write_text(text[:3000],encoding="utf-8")
    print("UKMTO: eventi trovati nella finestra:",len(out))
    attacks=[e for e in out if e["type"]=="Attack"]
    return {"count":len(attacks),"window":f"{cutoff.isoformat()} → {now.isoformat()}",
            "change7dText":f"{len(attacks)} attacchi verificati UKMTO",
            "events":out[:30]}

def parse_eia():
    # Valori in MIGLIAIA di barili (Mbbl): es. 1,248,641 = 1,25 mld bbl. Le colonne sono in ordine cronologico.
    url="https://www.eia.gov/dnav/pet/pet_stoc_wstk_a_EP00_SAE_Mbbl_w.htm"
    txt=BeautifulSoup(get(url),"html.parser").get_text(" ",strip=True)
    nums=[int(x.replace(",","")) for x in re.findall(r"\b1,\d{3},\d{3}\b",txt)]
    if len(nums)<2: raise RuntimeError("EIA values not parsed")
    vals=nums[-6:]
    ds=[f"20{y}-{m}-{d}" for m,d,y in re.findall(r"\b(\d\d)/(\d\d)/(\d\d)\b",txt)]
    labels=ds[:6] if len(ds)>=6 and len(vals)==6 else ["n/d"]*len(vals)
    hist=[{"date":l,"value":float(v)} for l,v in zip(labels,vals)]
    latest=vals[-1]; prev=vals[-2]
    print("EIA:",hist[-2:],"change (Mbbl):",latest-prev)
    return {"latest":latest,"change":latest-prev,"date":labels[-1],"history":hist}

def score(v,a,s):
    # Indicative stress score:
    # freight momentum 0-4, attacks 0-3, stocks draw 0-3.
    sf=4 if v["change48h"]>=20 else 3 if v["change48h"]>=10 else 2 if v["change48h"]>=5 else 1 if v["change48h"]>0 else 0
    sa=3 if a["count"]>=8 else 2 if a["count"]>=4 else 1 if a["count"]>=1 else 0
    ss=3 if s["change"]<=-5000 else 2 if s["change"]<0 else 0   # Mbbl: -5000 = -5 mln bbl
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
