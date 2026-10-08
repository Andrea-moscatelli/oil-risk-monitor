# 🛢️ Oil Risk Monitor

Dashboard gratuita per monitorare i principali segnali di stress del mercato petrolifero:

- VLCC Freight Index
- attacchi/incidenti alle petroliere negli ultimi 7 giorni
- scorte USA settimanali
- scorte OECD mensili
- Supply Stress Score

## Come pubblicarla gratis

1. Crea un repository GitHub, ad esempio `oil-risk-monitor`.
2. Carica tutti i file di questo progetto.
3. Vai in **Settings → Pages**.
4. Seleziona **GitHub Actions** come source.
5. Il workflow `deploy.yml` pubblicherà automaticamente il sito.
6. Il workflow `.github/workflows/collect.yml` aggiornerà i dati ogni 6 ore.

L'URL sarà normalmente:

`https://TUO-USERNAME.github.io/oil-risk-monitor/`

## Nota sui dati

- VLCC: CommodityScope, indice giornaliero. Non è una quotazione TCE.
- Incidenti: UKMTO, conteggio degli eventi classificati Attack negli ultimi 7 giorni.
- USA: EIA, dati settimanali.
- OECD: IEA, dati mensili; l'ultimo dato può essere più vecchio dei dati USA.
- Il punteggio è un indicatore sintetico creato per questa dashboard, non un indicatore ufficiale.

## Storico di accuratezza dello score

A ogni raccolta (ogni 6 ore) `collector.py` salva in `data/track_record.json` lo score, il segnale e il prezzo del Brent (futures `BZ=F`, Yahoo Finance):

- score **> 6** → segnale **BUY**: azzeccato se dopo 24h il Brent è più alto
- score **< 4** → segnale **SELL**: azzeccato se dopo 24h il Brent è più basso
- altrimenti **NEUTRAL**: non conta come previsione ma viene misurato lo stesso, per calcolare la baseline

Gli snapshot fatti (o da verificare) a mercato chiuso vengono marcati `skipped` ed esclusi dalle statistiche.
`data/accuracy.json` contiene il riepilogo (hit rate, rendimento medio, baseline, p-value, tabella per valore di score) e viene mostrato in dashboard.
Le soglie sono in `tracker.py` (`BUY_ABOVE`, `SELL_BELOW`); se cambi la formula dello score incrementa `SCORE_VERSION`.

## Obiettivo

Il pannello è pensato soprattutto per leggere il rischio di shock dell'offerta petrolifera. Non deve essere usato come unico criterio per aprire o chiudere una posizione.
