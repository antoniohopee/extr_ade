# extr-ade

Estrazione delle fatture elettroniche dal cassetto fiscale dell'Agenzia delle
Entrate: accesso, consultazione delle fatture emesse e ricevute, download degli
XML e lettura in dati strutturati. Tutto da terminale.

> **Progetto non affiliato all'Agenzia delle Entrate.** Non è un prodotto
> ufficiale, non è approvato né sostenuto dall'Agenzia, e usa il portale
> pubblico esattamente come farebbe una persona con un browser.

## Stato del progetto

È nato per un'esigenza personale ed è pubblicato perché possa servire da base a
chi ha lo stesso problema. Funziona, è usato davvero, ma non è un prodotto:
non c'è supporto garantito e le segnalazioni vengono guardate quando c'è tempo.

Se ti serve qualcosa di diverso, il modo migliore di usarlo è forkarlo.

## Prima di iniziare: due cose da sapere

### 1. Serve un'utenza Entratel o Fisconline

L'accesso avviene con **utente, password e PIN** rilasciati dall'Agenzia delle
Entrate.

**Con SPID, CIE o CNS questo programma non funziona**, e non è una mancanza che
si possa colmare: quelle modalità richiedono un'app, una smart card o un
riconoscimento interattivo, cioè esattamente ciò che un programma automatico
non può fare al posto tuo. Le credenziali Entratel/Fisconline sono riservate a
professionisti e imprese; i cittadini privati accedono solo con SPID/CIE/CNS e
quindi non possono usare questo strumento.

### 2. Scaricare una fattura RICEVUTA registra la presa visione

Sulle fatture passive, il download dal portale registra la **presa visione**
del documento, che ha rilevanza fiscale (è la data da cui decorrono alcuni
termini).

Il programma non può evitarlo: è il portale a registrarla nel momento in cui si
scarica il file, esattamente come accadrebbe cliccando a mano.

Tienine conto soprattutto se pensi di eseguire lo scaricamento in modo
automatico e ricorrente. Sulle fatture **emesse** non si pone il problema.

## Requisiti

- Python 3.12
- circa 200 MB di spazio per il browser usato in automazione

## Installazione

```bash
git clone https://github.com/<utente>/extr_ade.git
cd extr_ade

python3.12 -m venv .venv
source .venv/bin/activate          # su Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium        # NON saltare questo passo
```

Il secondo comando scarica il browser che il programma pilota. Senza, il
programma si avvia e fallisce subito.

## Configurazione

```bash
cp .env.example .env
```

Poi riempi `.env` con le tue credenziali. Le variabili sono documentate dentro
il file stesso. In alternativa puoi non toccarlo: al primo avvio le credenziali
vengono chieste a terminale e ti viene proposto di salvarle.

Il file `.env` è in `.gitignore` e non deve mai finire in un commit.

## Uso

```bash
PYTHONPATH=src python -m extr_ade.consultation
```

Il programma chiede una cosa alla volta:

1. credenziali (o conferma di quelle salvate)
2. **Me stesso** o **Incaricato**; se incaricato, per quale soggetto delegante
3. fatture **emesse** o **ricevute**
4. mostra l'elenco numerato da 0, e poi un menu:

```
Cosa vuoi fare?
  0  Filtra la lista
  1  Scarica fatture
  2  Cambia sezione (emesse/ricevute)
  3  Ricarica l'elenco dal portale
  4  Esci
```

Alla voce **Scarica fatture** si indica cosa prendere con gli indici mostrati:
`0-20` per un intervallo, `3` per una sola, `0,4,7` per più fatture.

Il browser lavora in modalità headless: **non si apre nessuna finestra**.

### Dove finiscono i file

```
fatture/
  emesse/
    IT00000000000_XXXXX.xml     nome originale assegnato dal portale
    .scaricate.json             indice delle fatture già prese
  ricevute/
```

La cartella è creata al primo download ed è in `.gitignore`.

L'indice `.scaricate.json` serve a non riscaricare due volte la stessa fattura:
il nome del file lo decide il portale e non è ricavabile in anticipo, quindi
teniamo traccia dell'associazione. Se cancelli un XML a mano, al giro successivo
viene riscaricato.

### Filtri

- **date di emissione**: applicate sul form del sito, restringono i risultati
  alla fonte
- **numero fattura** e **cliente**: applicati ai risultati già scaricati,
  perché la ricerca del portale non li prevede (accetta solo P.IVA o codice
  fiscale)

Conseguenza pratica: restringi prima con le date, poi filtra per nome.

### Lettura degli XML

Il modulo `extr_ade.invoice_xml` trasforma i file scaricati in dati
strutturati (soggetti, righe di dettaglio, riepilogo IVA), con gli importi in
`Decimal`:

```python
from pathlib import Path
from extr_ade.invoice_xml import parse_folder

for fattura in parse_folder(Path("fatture/emesse")):
    print(fattura.number, fattura.issue_date, fattura.customer.name, fattura.total)
```

## Test

```bash
python -m pytest
```

I test non si collegano al portale e usano dati inventati. Girano in meno di un
secondo.

## Limiti noti

- **Solo credenziali Entratel/Fisconline** (vedi sopra): con SPID/CIE/CNS non
  funziona.
- **Le fatture firmate (`.xml.p7m`) non vengono lette** dal parser: sono XML
  dentro una busta crittografica. Vengono scaricate, ma `parse_folder` le
  segnala come non leggibili anziché ignorarle in silenzio.
- **Gli endpoint del portale non sono documentati né stabili.** Tutti i
  selettori usati sono stati letti dalle pagine reali e sono raccolti in
  `src/extr_ade/constants.py`, con l'indicazione di quando sono stati
  verificati. Se un giorno qualcosa smette di funzionare, quello è il primo
  file da guardare: il programma è scritto per fermarsi con un errore esplicito
  invece di produrre dati incompleti.
- **Il totale della fattura è quello dichiarato dall'emittente**
  (`ImportoTotaleDocumento`). Può differire di qualche centesimo dalla somma
  di imponibile e imposta, a causa degli arrotondamenti: la differenza è
  esposta e non nascosta (`has_total_mismatch`).

## Struttura del codice

| File | Cosa fa |
|---|---|
| `constants.py` | URL e selettori del portale, tutti verificati e datati |
| `credentials.py` | credenziali da `.env` o da terminale |
| `login.py` | accesso headless al portale |
| `identity.py` | scelta utenza di lavoro (me stesso / incaricato) |
| `consultation.py` | navigazione, menu e flusso principale |
| `invoices.py` | lettura dell'elenco fatture dalla tabella |
| `downloader.py` | download degli XML e indice dei già scaricati |
| `invoice_xml.py` | lettura degli XML in dati strutturati |

## Licenza

MIT — vedi [LICENSE](LICENSE). Il software è fornito "così com'è", senza
garanzie: dipende da un portale di terzi che può cambiare in qualsiasi momento.
