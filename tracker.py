"""Track record dello Supply Stress Score sul Brent.

Regola di valutazione:
  score > BUY_ABOVE   -> segnale BUY  -> "azzeccato" se il Brent a +24h è più alto di quello al segnale
  score < SELL_BELOW  -> segnale SELL -> "azzeccato" se il Brent a +24h è più basso
  altrimenti          -> NEUTRAL      -> non conta come previsione, ma viene comunque misurato
                                        (serve per calcolare la baseline)

Ogni snapshot (anche NEUTRAL) viene salvato con prezzo di ingresso e, dopo 24h, prezzo di uscita.
Gli snapshot fatti a mercato chiuso (ingresso o uscita) vengono marcati "skipped" ed esclusi
dalle statistiche, per non misurare il gap del weekend al posto di un vero movimento a 24h.
"""
import math
from datetime import datetime, timedelta, timezone

import requests

BUY_ABOVE = 6          # score > 6  -> BUY
SELL_BELOW = 4         # score < 4  -> SELL
HORIZON_H = 24         # finestra di verifica
MAX_ENTRY_AGE_H = 3    # prezzo di ingresso più vecchio di così = mercato chiuso
MAX_EXIT_GAP_H = 2     # prima barra oraria utile dopo +24h entro questo scarto, altrimenti mercato chiuso
SCORE_VERSION = 1      # incrementalo se cambi la formula dello score: i record restano separabili

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F"
HEADERS = {"User-Agent": "Mozilla/5.0 OilRiskMonitor/1.0"}


def signal_for(score):
    if score > BUY_ABOVE:
        return "BUY"
    if score < SELL_BELOW:
        return "SELL"
    return "NEUTRAL"


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def fetch_brent():
    """Ultimo prezzo del Brent (futures BZ=F) + barre orarie dell'ultimo mese.

    Ritorna {"price", "ts", "bars": [(epoch, open), ...]} con le barre in ordine crescente.
    """
    r = requests.get(YAHOO, params={"interval": "1h", "range": "1mo"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    quote = res["indicators"]["quote"][0]
    bars = [(int(t), float(o)) for t, o in zip(res.get("timestamp") or [], quote["open"]) if o is not None]
    meta = res["meta"]
    return {"price": float(meta["regularMarketPrice"]), "ts": int(meta["regularMarketTime"]), "bars": bars}


def new_record(score_value, brent, now):
    rec = {
        "timestamp": now.isoformat(),
        "score": score_value,
        "score_version": SCORE_VERSION,
        "signal": signal_for(score_value),
        "target_ts": (now + timedelta(hours=HORIZON_H)).isoformat(),
        "brent_entry": None,
        "brent_entry_ts": None,
        "status": "pending",
        "reason": None,
        "outcome": None,
    }
    if not brent:
        rec.update(status="skipped", reason="prezzo Brent non disponibile")
        return rec
    rec["brent_entry"] = brent["price"]
    rec["brent_entry_ts"] = _iso(brent["ts"])
    if now.timestamp() - brent["ts"] > MAX_ENTRY_AGE_H * 3600:
        rec.update(status="skipped", reason="mercato chiuso al momento del segnale")
    return rec


def evaluate_pending(records, bars, now):
    """Chiude i record 'pending' il cui target a +24h è ormai passato."""
    for r in records:
        if r["status"] != "pending":
            continue
        target = datetime.fromisoformat(r["target_ts"])
        if now < target:
            continue
        t = target.timestamp()
        nxt = next(((ts, p) for ts, p in bars if ts >= t), None)
        if nxt is None:
            # nessuna barra dopo il target (es. weekend): riprova al prossimo giro, poi rinuncia
            if now - target > timedelta(days=7):
                r.update(status="skipped", reason="prezzo di uscita non disponibile")
            continue
        ts, price = nxt
        if ts - t > MAX_EXIT_GAP_H * 3600:
            r.update(status="skipped", reason="mercato chiuso a +24h")
            continue
        ret = (price / r["brent_entry"] - 1) * 100
        r.update(status="evaluated", brent_exit=price, brent_exit_ts=_iso(ts), ret24h_pct=round(ret, 3))
        if r["signal"] == "BUY":
            r["outcome"] = "hit" if ret > 0 else "miss"
        elif r["signal"] == "SELL":
            r["outcome"] = "hit" if ret < 0 else "miss"


def _binom_sf(k, n, p):
    """P(X >= k) per X ~ Binomiale(n, p). Test esatto a una coda."""
    if n == 0 or not (0 < p < 1):
        return None
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def summarize(records, now):
    ev = [r for r in records if r["status"] == "evaluated"]
    rets = [r["ret24h_pct"] for r in ev]
    up = sum(x > 0 for x in rets)
    down = sum(x < 0 for x in rets)
    up_rate = up / len(ev) if ev else None
    down_rate = down / len(ev) if ev else None

    out = {
        "updated_at": now.isoformat(),
        "horizon_h": HORIZON_H,
        "thresholds": {"buy_above": BUY_ABOVE, "sell_below": SELL_BELOW},
        "counts": {
            "total": len(records),
            "evaluated": len(ev),
            "pending": sum(r["status"] == "pending" for r in records),
            "skipped": sum(r["status"] == "skipped" for r in records),
        },
        "baseline": {"n": len(ev), "up_rate": up_rate, "down_rate": down_rate, "avg_ret_pct": _mean(rets)},
        "signals": {},
        "by_score": [],
    }

    for sig, base in (("BUY", up_rate), ("SELL", down_rate)):
        rs = [r for r in ev if r["signal"] == sig]
        hits = sum(r["outcome"] == "hit" for r in rs)
        signed = [r["ret24h_pct"] if sig == "BUY" else -r["ret24h_pct"] for r in rs]
        out["signals"][sig] = {
            "n": len(rs),
            "hits": hits,
            "hit_rate": hits / len(rs) if rs else None,
            "avg_signed_ret_pct": _mean(signed),   # rendimento medio "seguendo" il segnale (SELL = short)
            "baseline_rate": base,                  # % di volte in cui il Brent si è mosso in quella direzione in generale
            "p_value": _binom_sf(hits, len(rs), base) if base is not None else None,
            "pending": sum(r["status"] == "pending" and r["signal"] == sig for r in records),
        }

    for s in range(0, 11):
        rs = [r for r in ev if r["score"] == s]
        out["by_score"].append({
            "score": s,
            "n": len(rs),
            "up_rate": (sum(r["ret24h_pct"] > 0 for r in rs) / len(rs)) if rs else None,
            "avg_ret_pct": _mean([r["ret24h_pct"] for r in rs]),
        })
    return out
