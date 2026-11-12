# Bot Telegram — sconti caffeina su Amazon.it

Bot personale che sorveglia Amazon.it e ti manda un messaggio su Telegram **solo quando c'è uno sconto alto** su compresse/capsule di caffeina. Nessun rumore: soglie configurabili, tetto giornaliero e nessun doppione.

## Cosa controlla

- Ricerche: `caffeina compresse`, `caffeina capsule`, `caffeina 200 mg`, `caffeine pills` (modificabili in `config.toml`).
- Sezioni **Offerte e Risparmi** e **Offerte del giorno** di Amazon per le stesse ricerche: più probabilità di intercettare sconti veri senza aprire mille pagine.
- Per ogni prodotto: prezzo attuale, prezzo consigliato, sconto %, coupon, disponibilità, numero di compresse e mg per compressa.
- Calcola il **costo per dose da 200 mg**, che è il vero indicatore di convenienza (evita "sconti" su prodotti sovrapprezzati).
- Se Amazon blocca le richieste, riprova con Chromium headless (Playwright) invece di perdere il giro.

## Quando arriva la notifica

Una notifica parte se vale **almeno una** di queste condizioni:

| Regola | Default | Significato |
|---|---|---|
| Sconto alto e prezzo buono | sconto ≥ 30% **e** costo ≤ 0,025 €/dose 200 mg | affare solido |
| Sconto folle | sconto ≥ 45% **e** costo ≤ 0,035 €/dose | etichetta 🔥 SCONTO FOLLE |
| Nuovo minimo storico | prezzo ≥ 12% sotto il minimo registrato dal bot | 🥇 prezzo ai minimi |

Filtri anti-spam: massimo 8 alert al giorno, stesso prodotto al massimo una volta ogni 48 ore (prima se il prezzo scende ancora del 5%), niente prodotti non disponibili, niente prezzi sospetti.

Lo sconto è calcolato **anche sul coupon** applicato al checkout, quindi un 23% + coupon 20% diventa −38% reale.

## Requisiti

- Python 3.11+ (qui: 3.12) con `requests` e `beautifulsoup4`
- Un bot Telegram (2 minuti con @BotFather)
- PC acceso (o un runner) quando vuoi i controlli

## 1. Installazione

```powershell
cd C:\Users\miche\amazon-caffeina-alerts
python -m pip install -r requirements.txt
copy config.example.toml config.toml
```

## 2. Creare il bot Telegram

1. Su Telegram cerca **@BotFather** → `/newbot`.
2. Scegli un nome e un username che finisce per `bot` (es. `caffeina_deals_bot`).
3. Copia il **token** che ti restituisce (formato `123456789:AAE...`).
4. Cerca il tuo nuovo bot e invia `/start` (serve per fargli conoscere la tua chat).

## 3. Configurazione guidata

```powershell
python run.py setup
```

- incolla il token;
- il bot ti risponde con il suo username: aprigli la chat e invia `/start`;
- lo script rileva la chat id, salva tutto in `.env` e ti manda un messaggio di conferma.

Verifica: `python run.py test` → deve arrivarti "🔔 Test".

## 4. Prima scansione

```powershell
python run.py scan --dry    # mostra gli alert senza inviarli
python run.py scan          # invia davvero su Telegram
```

La prima scansione valuta fino a 25 prodotti (circa 1–2 minuti, con pause volutamente lente). Le successive usano la cache e ruotano i prodotti.

## 5. Avvio continuo

```powershell
python run.py run
```

Il bot resta attivo: risponde ai comandi e scansiona ogni 30 minuti (±5 di jitter). Log in `data/bot.log`, stato in `data/state.db`.

### Avvio automatico su Windows

Doppio clic su `avvia_bot.bat` per la versione con finestra (utile per vedere i log), oppure `avvia_bot_silenzioso.vbs` per farlo girare senza finestra.

Per farlo partire a ogni accesso:

```powershell
schtasks /Create /TN "CaffeinaBot" /SC ONLOGON /RL LIMITED /F /TR "wscript.exe \"C:\Users\miche\amazon-caffeina-alerts\avvia_bot_silenzioso.vbs\""
```

Rimuoverlo: `schtasks /Delete /TN "CaffeinaBot" /F`.

## Comandi del bot

| Comando | Effetto |
|---|---|
| `/stato` | soglie attuali, ultima scansione, statistiche |
| `/soglia 35` | sconto minimo % |
| `/folle 50` | soglia "SCONTO FOLLE" |
| `/dose 0,025` | tetto € per dose 200 mg |
| `/dose auto` | tetto calcolato dal mercato rilevato (25° percentile × 0,8) |
| `/pausa` · `/riprendi` | sospende/riattiva le notifiche |
| `/top` | i 5 sconti migliori in archivio |
| `/info ASIN` | dettaglio, storico e ultimo alert di un prodotto |
| `/report` | riepilogo degli ultimi 7 giorni |
| `/ultimi` | ultimi 5 alert |
| `/scan` | forza una scansione subito |
| `/aiuto` | elenco comandi |

Le soglie cambiate dal bot sono salvate nel database e hanno priorità sul `config.toml`.

## Ottimizzazioni attive

- **Fonti adattive**: un filtro Offerte che per 3 giri non restituisce nulla viene sospeso per 6 ore (misurato: dal cloud quegli 8 URL rendevano 0 risultati, ora vengono saltati).
- **Dettagli in parallelo** (2 worker) con limite globale di una richiesta ogni ~2,5 s: scansione da 25 prodotti passata da ~105 s a ~46 s.
- **Priorità**: prima le offerte con badge, poi gli sconti già visibili in vetrina (listino barrato validato), poi i nuovi, poi i più vecchi.
- **Deduplica per confezione** (marca + forma + compresse + mg), non solo per ASIN: le varianti dello stesso listing non generano doppioni.
- **Digest**: con 4 o più alert in un giro arriva un unico messaggio riepilogativo.
- **Silenziamento**: bottone "Ignora 30 giorni" su ogni alert.
- **Misure**: ogni scansione registra durata e numero di richieste, visibili in `/stato`.
- **Soglia dose tarabile**: `/dose auto` la calcola dal mercato rilevato (25° percentile × 0,8); i valori scelti a mano non vengono sovrascritti dagli aggiornamenti.

## Personalizzazione (`config.toml`)

- `queries`: aggiungi ricerche specifiche, es. `"caffeina anidra compresse"`, `"prolab caffeina"`.
- `filtri_offerte`: sezioni Offerte da scandagliare (`[]` per disattivarle).
- `pagine`: 2 per più copertura (più richieste).
- `tag_affiliato`: se hai un tag Amazon, i link diventano affiliati.
- `ritardo_min/max_secondi`: non scendere sotto 1,5 s.
- `max_dettagli_per_scan`: quanti prodotti aprire per giro (con 130+ candidati la coda si smaltisce in più giri).
- `browser_se_captcha`: fallback Chromium headless quando Amazon blocca.
- `invia_immagine`: alert con foto del prodotto quando il testo sta nel limite didascalia.
- `parallelismo`: pagine prodotto scaricate in parallelo (1 = seriale).
- `digest_min`: numero di alert oltre il quale parte il riepilogo unico.
- `intervallo_minuti`: ogni quanto controllare (30 è un buon compromesso).

## Cosa esiste già (ricerca fatta a novembre 2026)

| Progetto / servizio | Approccio | Cosa ne ho ricavato |
|---|---|---|
| [albertopasqualetto/amazon-deals-telegram-bot](https://github.com/albertopasqualetto/amazon-deals-telegram-bot) (31★, MIT) | Selenium sulla pagina `/deals` con filtro `discounts-widget` ≥50%, poi pagina prodotto, invio con foto e link affiliato | Idea dei filtri Offerte e alert con immagine. Selenium non usato come default: parte solo il fallback Playwright |
| [AleG94/Pricegram](https://github.com/AleG94/Pricegram) (50★, JS, fermo al 2022) | Tracking per singolo prodotto, alert prezzo e disponibilità | Conferma dell'impianto: dedupe per ASIN e cooldown tra alert |
| [siddiquiaffan/price-tracker](https://github.com/siddiquiaffan/price-tracker) (87★, GPL) | Bot + API + worker per molti utenti | Non adatto a uso personale: troppa infrastruttura |
| [brunotr88/amazon-telegram-bot](https://github.com/brunotr88/amazon-telegram-bot) (IT) | PA-API 5.0 + scoring delle offerte | Lo scoring è simile al nostro €/dose, ma la PA-API è deprecata dal 15/05/2026 |
| [Keepa API](https://keepa.com/api-docs/deals.html) | `/deal`: 5 token, fino a 150 sconti, `deltaPercentRange`, `titleSearch`, `domainId 8` = Italia, storico prezzi vero | Tecnicamente il migliore, ma i piani API partono da ~49 €/mese senza piano gratuito: sproporzionato qui |
| [Amazon Creators API](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction) | API ufficiale al posto della PA-API | Richiede iscrizione Associates + 10 vendite qualificate in 30 giorni: non praticabile senza un canale affiliato attivo |
| CamelCamelCamel / PrezzoAlert | Solo interfaccia web, nessuna API pubblica | Niente da integrare |

Conclusione: scraping leggero in locale, con filtri Offerte e fallback browser, copre il caso d'uso a costo zero. Keepa diventa sensato solo se vuoi storico prezzi certificato e molte ricerche.

## Modalità cloud: solo Telegram, nessun PC acceso

Il bot può girare interamente su **GitHub Actions** (repo pubblico = minuti gratis), senza server e senza tenere il PC acceso:

- ogni 15 minuti un job legge i comandi in coda su Telegram e, se è passata mezz'ora, scansiona Amazon;
- lo stato (storico prezzi, chat id, offset comandi) viaggia nella cache di Actions;
- il primo `/start` viene registrato automaticamente e da quel momento ricevi gli alert.

Comandi utili:

```powershell
gh workflow run sconti.yml          # forza una passata
gh run list --limit 3               # ultime esecuzioni
gh run view <id> --log              # log completi
```

Limiti da conoscere:

- granularità 15 minuti (GitHub può ritardare i cron sotto carico);
- dagli IP dei runner Amazon restituisce meno risultati rispetto a casa (nessun captcha nei test);
- GitHub disattiva i workflow schedulati dopo 60 giorni senza attività sul repo: basta un commit o un avvio manuale;
- repo pubblico: il token sta nei Secrets, mai nel codice.

Per copertura piena e comandi immediati serve un host 24/7: `Dockerfile` e `docker-compose.yml` sono pronti per una VM (Oracle Cloud Always Free, 2 OCPU/12 GB ARM, è l'opzione gratuita più solida).

## Struttura

```
run.py                  CLI: setup / scan / run / test / stats
caffeina_bot/
  amazon.py             ricerca (+ filtri Offerte) e pagina prodotto, parsing, anti-blocco
  browser.py            fallback Chromium headless via Playwright
  analyze.py            mg/compresse dal titolo, costo per dose, regole sconto
  scanner.py            orchestrazione scan → alert
  bot.py                comandi Telegram + loop periodico
  telegram.py           client Bot API + formattazione messaggi
  store.py              SQLite: storico prezzi, alert, cache, soglie
  config.py             config.toml + .env
tests/                  54 test offline (parser, regole, comandi, fonti, ottimizzazioni)
data/                   state.db, bot.log, cookies.json (ignorati da git)
```

## Limiti e note

- Amazon non offre un'API pubblica per i prezzi: il bot legge le pagine pubbliche, con pause e cache. Uso personale, frequenza bassa. [da verificare: i termini d'uso Amazon vietano lo scraping automatizzato — valuta l'uso di un'API ufficiale se il volume cresce]
- Se Amazon mostra un captcha il bot salta il giro e ti avvisa (max un avviso ogni 6 ore). Rimedi: aumentare i ritardi, riavviare da un'altra rete, ridurre `pagine`.
- I prezzi "consigliati" sono indicati dal venditore: il filtro €/dose serve proprio a non fidarsi solo della percentuale.
- I coupon a volte valgono solo per il primo ordine o per l'abbonamento: il messaggio lo segnala.
- Alternativa più solida e a pagamento: API Keepa (storico prezzi ufficiale). [da verificare: piani e costi attuali]

## Troubleshooting

| Sintomo | Rimedio |
|---|---|
| `Telegram non configurato` | `python run.py setup` |
| Non arriva nulla | controlla di aver inviato `/start` al bot e che il token in `.env` sia giusto |
| `Amazon ha richiesto un captcha` | il fallback browser dovrebbe risolverlo; se persiste alza `ritardo_min_secondi`, riduci `pagine`, riprova più tardi |
| Fallback browser non parte | `python -m playwright install chromium` |
| `HTTP 503` ripetuto | Amazon sta limitando: attendi 30–60 minuti |
| Prezzi a 0 o strani | apri `data/bot.log`, controlla l'ASIN e rilancia `python run.py scan --dry` |
| Vuoi ripartire da zero | chiudi il bot, cancella `data/state.db` |
