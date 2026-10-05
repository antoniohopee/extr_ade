"""Scaricamento dei file XML delle fatture.

Il portale non espone un indirizzo da cui prelevare l'XML: il file è prodotto
da una funzione JavaScript della pagina di dettaglio. Quindi si passa dal
browser, cliccando il bottone e intercettando il file che ne esce.

Conseguenza pratica: per ogni fattura bisogna aprire il suo dettaglio. Sono
uno o due secondi a fattura. Se un domani diventasse troppo lento, l'alternativa
è il servizio "Consultazioni e download massivi" (`/cons/mass-web/`), che
prepara pacchetti in modo asincrono.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from extr_ade.constants import (
    DOWNLOAD_BUTTON_NAME,
    ENV_INVOICES_DIR,
    INVOICES_DIR,
)
from extr_ade.invoices import Invoice
from extr_ade.portal import PortalDownError, raise_if_portal_down

# Quanto aspettiamo che il file arrivi dopo il click.
DOWNLOAD_TIMEOUT_MS = 60_000

# Pausa fra un download e il successivo. Non è prudenza eccessiva: venti
# richieste consecutive a raffica su un portale pubblico sono maleducate e
# rischiano di farci limitare o bloccare.
PAUSE_SECONDS = 1.0

# Pausa usata per il RESTO del giro dopo che il portale ci ha rifiutato almeno
# una volta. Se ha cominciato a dire di no, ha senso tenere un passo più lento
# anche quando riprende a rispondere, invece di tornare subito a bussare al
# ritmo che ci ha fatti rifiutare.
#
# Il valore predefinito resta 1 secondo perché a quel ritmo il programma ha
# funzionato per mesi: non lo cambiamo in base a un sospetto. Questo invece è
# il passo "ci siamo già scottati", e si applica solo dopo il primo rifiuto.
SLOW_PAUSE_SECONDS = 5.0

# Quante volte riproporre la STESSA fattura quando il portale la rifiuta.
# Tre: il primo rifiuto può essere un caso, il terzo non lo è più.
PORTAL_RETRIES = 3

# Attesa dopo un rifiuto, prima di riprovare. Raddoppia a ogni tentativo: se
# il portale sta limitando la frequenza, il rimedio è il tempo, e insistere
# allo stesso ritmo è il modo di non uscirne.
#
# Con `PORTAL_RETRIES = 3` le attese che avvengono davvero sono DUE, 15s e
# 30s: dopo il terzo rifiuto si rinuncia subito, senza aspettare. Vale la pena
# scriverlo perché il conto non è ovvio, e il commento che diceva "15s, 30s,
# 60s" prometteva un'attesa che non è mai stata fatta.
#
# `MAX_PAUSE_SECONDS` è quindi un tetto che oggi non si raggiunge: serve solo
# se un domani si alzasse `PORTAL_RETRIES`. Resta perché la formula senza
# tetto, al decimo tentativo, aspetterebbe più di due ore.
#
# Onestà sui numeri: non sappiamo quale sia la soglia vera del portale, non è
# documentata da nessuna parte. Questi valori sono una stima prudente, stanno
# qui in un punto solo, e vanno corretti con i dati del primo giro completo.
RECOVERY_PAUSE_SECONDS = 15.0
MAX_PAUSE_SECONDS = 120.0

# Dopo quanti fallimenti CONSECUTIVI smettiamo del tutto.
#
# È la rete di sicurezza per il caso che qui non abbiamo previsto: se il
# portale cambia il testo delle sue pagine di rifiuto, il riconoscimento non
# aggancia più e torneremmo a macinare fallimenti a vuoto. Il 2026-09-28 ne ha
# fatti 24 di fila e il 2026-10-05 altri 31, senza che nessuno li fermasse.
# Cinque bastano a capire che non è la singola fattura.
MAX_FAILURES_IN_A_ROW = 5

# Pausa prima del secondo passaggio sulle non riuscite.
#
# Un minuto perché la finestra di rifiuto osservata il 2026-10-05 è durata
# circa cinque minuti, e il secondo passaggio ha comunque i suoi tre
# tentativi con le loro attese: fra pausa e tentativi si arriva a coprirla.
SECOND_PASS_PAUSE_SECONDS = 60.0

# Dopo quante fatture consecutive senza intoppi si torna al passo normale.
#
# Il passo lento (`SLOW_PAUSE_SECONDS`) serve finché il portale è nervoso. Se
# resta appiccicato a tutto il giro, un solo rifiuto all'inizio costa un
# quarto d'ora su 241 fatture: un prezzo pagato per un problema finito.
CLEAN_STREAK_TO_SPEED_UP = 10

# Caratteri che non possono comparire in un nome di file.
UNSAFE_CHARACTERS = re.compile(r'[/\\:*?"<>|]')

# Indice delle fatture già scaricate, uno per cartella. Collega
# l'identificativo della fattura sul portale al nome del file salvato.
#
# Serve perché il nome del file lo decide il portale e non lo conosciamo prima
# di aver cliccato "Download": senza indice non potremmo sapere se una fattura
# è già stata presa, e la riscaricheremmo ogni volta.
INDEX_FILENAME = ".scaricate.json"


def safe_filename(name: str) -> str:
    """Rende un nome utilizzabile come nome di file.

    I numeri di fattura contengono spesso barre ("2026/123"), che in un nome
    di file verrebbero interpretate come cartelle: il file finirebbe altrove,
    o la scrittura fallirebbe.
    """
    cleaned = UNSAFE_CHARACTERS.sub("_", name).strip()
    return cleaned or "fattura"


def destination_dir(kind: str) -> Path:
    """Cartella dove salvare gli XML: `fatture/emesse` o `fatture/ricevute`.

    Sta nella radice del progetto e viene creata al primo download.

    `EXTR_ADE_INVOICES_DIR` permette di spostarla altrove senza toccare il
    codice: serve sul server del cron, dove i file vanno tipicamente su un
    disco diverso da quello del progetto.
    """
    base = Path(os.getenv(ENV_INVOICES_DIR, "").strip() or INVOICES_DIR)
    return base / kind


def download_invoice(page: Page, invoice: Invoice, folder: Path) -> Path | None:
    """Scarica l'XML di una fattura già aperta in dettaglio.

    Restituisce il percorso del file, o None se il download non è riuscito.

    Solleva `PortalDownError` se il motivo del fallimento è che il portale si
    è rifiutato: quello non è un esito della fattura, ed è l'unico caso in cui
    chi chiama deve aspettare e riprovare invece di passare oltre.

    Perché il controllo sta anche qui, e non solo nell'apertura del dettaglio.
    Il 2026-10-05 la fattura 21 di 33 è caduta esattamente in questo buco: il
    dettaglio si era aperto benissimo, il bottone c'era, il click è partito —
    e il file non è mai arrivato perché nel frattempo il portale aveva
    cominciato a rifiutare. Qui non c'era nessun controllo, quindi sono stati
    sessanta secondi di attesa e un messaggio generico. La fattura successiva,
    che ha incontrato lo stesso rifiuto un passo prima, lo ha riconosciuto e
    detto. Due comportamenti diversi per la stessa causa: è il buco che questa
    riga chiude.
    """
    try:
        button = page.get_by_role("button", name=DOWNLOAD_BUTTON_NAME)
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            button.first.click()
        download = download_info.value

        # Il portale propone già un nome sensato; se mancasse ripieghiamo sul
        # numero della fattura.
        suggested = download.suggested_filename or f"{invoice.number}.xml"
        destination = folder / safe_filename(suggested)

        download.save_as(destination)
        return destination

    except PlaywrightTimeout:
        # Prima di dire "il file non è arrivato", chiediamo alla pagina se il
        # portale ha messo lì una delle sue pagine di rifiuto.
        raise_if_portal_down(page)
        print(f"  ! {invoice.number}: il file non è arrivato entro il tempo massimo")
        return None
    except PortalDownError:
        raise
    except Exception as error:  # noqa: BLE001 - vogliamo proseguire comunque
        raise_if_portal_down(page)
        print(f"  ! {invoice.number}: download non riuscito ({error})")
        return None


class FilePendingError(Exception):
    """L'XML della fattura non è ancora stato preparato dal portale.

    Non è un guasto: il portale prepara il file entro 72 ore dalla consegna, e
    fino ad allora al posto del bottone di download mostra un avviso. Ha una
    sua eccezione perché va raccontato in modo diverso da un errore vero: qui
    non c'è niente da riparare, basta ripassare domani.
    """


class DetailOutcome(StrEnum):
    """Com'è andata l'apertura del dettaglio di una fattura."""

    OK = "ok"
    PENDING = "pending"
    FAILED = "failed"


def open_detail_safely(open_detail, page: Page, invoice: Invoice) -> DetailOutcome:
    """Apre il dettaglio di una fattura, senza sollevare eccezioni.

    Stessa scelta già fatta in `download_invoice`: in un giro da venti
    fatture, una che va male non deve far fallire le altre diciannove. Prima
    questo passo non era protetto, quindi un dettaglio che non si apriva
    interrompeva l'intero giro e faceva perdere la sessione del browser.

    Tre esiti e non due, perché "il file non è ancora pronto" non è un errore:
    chiamarlo tale manderebbe a cercare un guasto che non esiste.
    """
    try:
        open_detail(page, invoice)
        return DetailOutcome.OK
    except PortalDownError:
        # L'unica che lasciamo passare. Non è un esito della fattura: è il
        # portale che non ci sta rispondendo, e chi ci chiama deve poter
        # decidere di aspettare e riprovare invece di passare alla prossima.
        # Catturarla qui la ridurrebbe a un fallimento come gli altri, che è
        # esattamente l'errore che questo giro di modifiche sta correggendo.
        raise
    except FilePendingError:
        print(f"  ~ {invoice.number}: XML non ancora disponibile")
        print("    (il portale lo prepara entro 72 ore dalla consegna)")
        return DetailOutcome.PENDING
    except PlaywrightTimeout:
        print(f"  ! {invoice.number}: dettaglio non aperto entro il tempo massimo")
        return DetailOutcome.FAILED
    except Exception as error:  # noqa: BLE001 - vogliamo proseguire comunque
        print(f"  ! {invoice.number}: dettaglio non aperto ({error})")
        return DetailOutcome.FAILED


def pause_before_retry(attempt: int) -> float:
    """Quanto aspettare prima di riproporre una fattura rifiutata.

    Raddoppia a ogni tentativo e si ferma a `MAX_PAUSE_SECONDS`. Funzione a
    sé perché è l'unica aritmetica di tutto il modulo, ed è l'unica cosa qui
    dentro che si possa provare senza un browser.
    """
    return min(RECOVERY_PAUSE_SECONDS * 2 ** (attempt - 1), MAX_PAUSE_SECONDS)


@dataclass
class FetchResult:
    """Com'è andata una fattura: l'esito, il file, e se il portale ha detto di no.

    Tre informazioni invece di una perché servono a tre cose diverse: l'esito
    per i conti, il file per l'indice, il rifiuto per decidere se rallentare
    il resto del giro.
    """

    outcome: DetailOutcome
    path: Path | None = None
    refused: bool = False


def fetch_invoice(
    open_detail,
    recover,  # callable(page) -> None: riporta il browser sull'elenco
    page: Page,
    invoice: Invoice,
    folder: Path,
    label: str,
) -> FetchResult:
    """Apre il dettaglio e scarica l'XML, riprovando se il portale rifiuta.

    Apertura e download stanno nello STESSO tentativo, e non è un dettaglio
    organizzativo. Il 2026-10-05 il portale ha cominciato a rifiutare mentre
    una fattura era già aperta in dettaglio: col controllo solo sull'apertura,
    quel rifiuto arrivava troppo tardi per essere ripreso e la fattura era
    persa. Il tentativo deve coprire tutto il tratto in cui il portale può
    dire di no.

    Questo è il comportamento che il proprietario faceva già a mano: quando
    compariva la pagina di fuori servizio tornava all'elenco, le fatture
    ricomparivano e il download ripartiva.

    Fra un tentativo e l'altro si aspetta PRIMA e si rinaviga DOPO: se il
    portale sta limitando la frequenza, rinavigare subito è un'altra
    richiesta sullo stesso minuto, cioè la mossa che ha causato il rifiuto.
    """
    refused = False

    for attempt in range(1, PORTAL_RETRIES + 1):
        try:
            outcome = open_detail_safely(open_detail, page, invoice)
            if outcome is not DetailOutcome.OK:
                return FetchResult(outcome, None, refused)

            path = download_invoice(page, invoice, folder)
            if path is None:
                return FetchResult(DetailOutcome.FAILED, None, refused)
            return FetchResult(DetailOutcome.OK, path, refused)

        except PortalDownError as error:
            refused = True
            print(f"  ~ {label}: il portale ha risposto «{error}»")

            if attempt == PORTAL_RETRIES:
                print(f"    rifiutata {attempt} volte: passo alla prossima")
                return FetchResult(DetailOutcome.FAILED, None, refused)

            wait = pause_before_retry(attempt)
            print(f"    non è un guasto del programma: aspetto {wait:.0f}s e riprovo")
            time.sleep(wait)

            try:
                recover(page)
            except PortalDownError:
                # Il portale è ancora giù, e anche il ritorno all'elenco si è
                # preso un rifiuto. È il caso previsto, non un motivo per
                # rinunciare: proseguiamo con i tentativi che restano, e il
                # prossimo aspetta il doppio.
                continue
            except Exception as problem:  # noqa: BLE001 - il giro deve continuare
                print(f"    non sono riuscito a tornare all'elenco ({problem})")
                return FetchResult(DetailOutcome.FAILED, None, refused)

    return FetchResult(DetailOutcome.FAILED, None, refused)


class InvoiceOutcome(StrEnum):
    """Com'è finita una fattura in questo giro. Una sola per fattura."""

    SKIPPED = "skipped"  # già nell'indice, nemmeno provata
    SAVED = "saved"
    PENDING = "pending"  # XML non ancora predisposto dal portale
    FAILED = "failed"


@dataclass
class Progress:
    """I conti di un giro, che sopravvivono ai due passaggi.

    L'esito si registra PER FATTURA invece di tenere dei contatori e delle
    liste separate. Non è pignoleria: col secondo passaggio la stessa fattura
    può avere due esiti in momenti diversi, e con i contatori ci si ritrova a
    sommare due volte o a perdere per strada chi è fallito nel primo
    passaggio e non è stato riprovato nel secondo. Qui l'ultimo esito
    sovrascrive il precedente e i conti tornano da soli: chi non compare nel
    dizionario non è stato provato, e nessuna sottrazione può inventarlo.
    """

    saved: list[Path] = field(default_factory=list)
    outcomes: dict[str, InvoiceOutcome] = field(default_factory=dict)
    refused_once: bool = False
    slow_pace: bool = False
    stopped: bool = False

    def record(self, invoice: Invoice, outcome: InvoiceOutcome) -> None:
        self.outcomes[invoice.detail_id] = outcome

    def count(self, outcome: InvoiceOutcome) -> int:
        return sum(1 for value in self.outcomes.values() if value is outcome)

    def failed_among(self, invoices: list[Invoice]) -> list[Invoice]:
        """Le fatture di questo elenco che risultano non riuscite."""
        return [
            invoice
            for invoice in invoices
            if self.outcomes.get(invoice.detail_id) is InvoiceOutcome.FAILED
        ]


def _download_pass(
    page: Page,
    invoices: list[Invoice],
    folder: Path,
    index: dict[str, str],
    open_detail,
    recover,
    progress: Progress,
    prefix: str = "",
) -> None:
    """Un passaggio su un elenco di fatture. Aggiorna `progress` man mano.

    Esiste come funzione a sé perché il giro ne fa due: il secondo sulle
    fatture non riuscite nel primo. Duplicare questo corpo per il secondo
    passaggio avrebbe significato due posti da tenere allineati.
    """
    failures_in_a_row = 0
    clean_in_a_row = 0

    for number, invoice in enumerate(invoices, start=1):
        label = f"{prefix}[{number}/{len(invoices)}] {invoice.number}"

        registered = registered_as(invoice, index)
        if registered:
            # Saltata in entrambi i casi: cambia solo cosa ti diciamo, perché
            # "ce l'ho qui" e "l'ho già consegnata" sono due situazioni che a
            # te interessa distinguere.
            progress.record(invoice, InvoiceOutcome.SKIPPED)
            existing = stored_file(folder, registered)
            if existing:
                print(f"  = {label}: già presente ({existing.name})")
                progress.saved.append(existing)
            else:
                print(f"  = {label}: già scaricata ({registered}), non più in archivio")
            continue

        print(f"  . {label}: apro il dettaglio...")
        result = fetch_invoice(open_detail, recover, page, invoice, folder, label)
        if result.refused:
            progress.refused_once = True
            progress.slow_pace = True

        if result.outcome is DetailOutcome.PENDING:
            progress.record(invoice, InvoiceOutcome.PENDING)
            failures_in_a_row = 0
        elif result.path is None:
            progress.record(invoice, InvoiceOutcome.FAILED)
            failures_in_a_row += 1
            clean_in_a_row = 0
        else:
            print(f"  + {label}: salvata in {result.path}")
            progress.record(invoice, InvoiceOutcome.SAVED)
            progress.saved.append(result.path)
            failures_in_a_row = 0

            # L'indice si aggiorna subito, non alla fine: se interrompi a
            # metà, quello che è stato scaricato resta registrato come
            # scaricato.
            index[invoice.detail_id] = result.path.name
            save_index(folder, index)

        if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
            print(f"\n  STOP: {failures_in_a_row} fatture di fila non riuscite.")
            print("  Mi fermo invece di continuare a provare: quando sbagliano")
            print("  tutte di seguito, il problema non è la singola fattura.")
            print("  Quello che era già stato scaricato resta registrato.")
            progress.stopped = True
            return

        # Il passo lento si molla dopo un po' che tutto va bene.
        #
        # Senza questo, un solo rifiuto sulla seconda fattura di 241 terrebbe
        # il passo a cinque secondi per le altre 239: un quarto d'ora in più
        # per un inciampo finito da un pezzo. Il passo lento serve finché il
        # portale è nervoso, non per il resto della giornata.
        if not result.refused and result.path is not None:
            clean_in_a_row += 1
            if progress.slow_pace and clean_in_a_row >= CLEAN_STREAK_TO_SPEED_UP:
                print(f"    ({clean_in_a_row} fatture di fila senza intoppi: riprendo il passo normale)")
                progress.slow_pace = False

        # La pausa vale per OGNI tentativo, riuscito o no.
        #
        # Era il difetto più serio di questo ciclo: i fallimenti saltavano la
        # pausa con un `continue`, quindi davanti a un portale che si
        # rifiutava il programma accelerava invece di rallentare. Il
        # 2026-10-05 sono entrati 31 tentativi dentro lo stesso minuto.
        time.sleep(SLOW_PAUSE_SECONDS if progress.slow_pace else PAUSE_SECONDS)


def download_all(
    page: Page,
    invoices: list[Invoice],
    kind: str,
    open_detail,  # callable(page, invoice) -> None
    recover,  # callable(page) -> None: ritorno forzato all'elenco
) -> list[Path]:
    """Scarica un elenco di fatture, una alla volta, in due passaggi.

    `open_detail` e `recover` arrivano da fuori invece di essere importate
    qui: `consultation` usa questo modulo, e importarlo a sua volta creerebbe
    un import circolare.

    Una fattura registrata nell'indice non si riscarica MAI, nemmeno se il suo
    file non è più nella cartella: gli XML vengono consegnati alla contabilità
    e l'archivio locale si svuota di proposito. Se il file mancante contasse
    come "da riprendere", ogni giro dopo una consegna riscaricherebbe tutto lo
    storico, e sulle ricevute rifarebbe la presa visione.

    Per riscaricare una fattura di proposito: cancella la sua riga da
    `.scaricate.json` (è un file leggibile, una riga per fattura). È la via
    d'uscita, ed è volutamente manuale.

    **Il secondo passaggio** riprende le fatture non riuscite nel primo, una
    volta sola. Il 2026-10-05 le due fallite erano cadute in una finestra di
    rifiuto di circa cinque minuti, e il portale rispondeva già di nuovo una
    fattura più in là: un secondo passaggio le avrebbe prese entrambe senza
    che nessuno rilanciasse niente. Una volta sola e non di più: se una
    fattura fallisce due volte a minuti di distanza, non è un inciampo e
    insistere non la raddrizza.
    """
    folder = destination_dir(kind)
    folder.mkdir(parents=True, exist_ok=True)
    index = load_index(folder)

    progress = Progress()
    _download_pass(page, invoices, folder, index, open_detail, recover, progress)

    retry = progress.failed_among(invoices)
    if retry and not progress.stopped:
        print(f"\n  Secondo passaggio su {len(retry)} non riuscite.")

        # L'attesa serve solo se a far fallire è stato il portale. Se non ci
        # ha mai rifiutati, le fatture sono fallite per conto loro e un minuto
        # di pausa non cambia niente: sarebbe un minuto buttato a ogni giro
        # che contiene una fattura non più nell'elenco.
        if progress.refused_once:
            print(f"  Aspetto {SECOND_PASS_PAUSE_SECONDS:.0f}s: se era il portale a")
            print("  rifiutare, il tempo è l'unica cosa che serve.")
            time.sleep(SECOND_PASS_PAUSE_SECONDS)

        _download_pass(
            page, retry, folder, index, open_detail, recover, progress, prefix="ritento "
        )

    _print_summary(len(invoices), progress, folder)
    return progress.saved


def _print_summary(requested: int, progress: Progress, folder: Path) -> None:
    """Riepiloga il giro: quante saltate, quante prese, quante non riuscite.

    Il conto va esposto per esteso perché "3 file su 20" senza spiegazione
    sembra un guasto, mentre di norma significa che le altre 17 erano già
    state prese in passato.

    Le fatture in attesa di predisposizione si contano a parte: finivano fra
    le "NON riuscite" e mandavano a cercare un guasto che non c'è. Non sono
    registrate nell'indice, quindi il prossimo giro le riprende da solo.

    I numeri si contano dagli esiti registrati, non per sottrazione. La
    sottrazione valeva finché il giro arrivava sempre in fondo in un
    passaggio solo; ora può fermarsi a metà e ne fa due, e darebbe per
    "fallite" fatture mai provate o viceversa.
    """
    skipped = progress.count(InvoiceOutcome.SKIPPED)
    new_files = progress.count(InvoiceOutcome.SAVED)
    pending = progress.count(InvoiceOutcome.PENDING)
    failed = progress.count(InvoiceOutcome.FAILED)
    not_attempted = requested - len(progress.outcomes)

    print(f"\nRichieste {requested}:")
    print(f"  {skipped} già scaricate in passato (saltate)")
    print(f"  {new_files} scaricate ora in {folder}")
    if pending:
        print(f"  {pending} non ancora disponibili sul portale (riprova fra 72 ore)")
    if failed:
        print(f"  {failed} NON riuscite: rilancia per riprovare solo queste")
    if not_attempted:
        print(f"  {not_attempted} mai provate: il giro si è fermato prima")

    if progress.refused_once:
        print("\n  Nota: durante il giro il portale ha rifiutato almeno una")
        print("  richiesta con una sua pagina di indisponibilità. Non è un")
        print("  guasto del programma e non c'è niente da sistemare nel")
        print("  codice: rilancia più tardi, l'indice fa riprendere da qui.")


def load_index(folder: Path) -> dict[str, str]:
    """Legge l'indice delle fatture già scaricate in questa cartella.

    Un indice illeggibile (troncato, modificato a mano) non deve bloccare il
    programma: si riparte da vuoto, al massimo si riscarica qualcosa.
    """
    path = folder / INDEX_FILENAME
    if not path.exists():
        return {}

    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        print(f"  ! indice illeggibile ({error}): riparto da vuoto")
        return {}

    return content if isinstance(content, dict) else {}


def save_index(folder: Path, index: dict[str, str]) -> None:
    """Scrive l'indice. Va richiamato dopo ogni download, non alla fine.

    Se il programma viene interrotto a metà di venti fatture, quelle già prese
    devono risultare prese: altrimenti al giro dopo si riscaricano, e sulle
    fatture ricevute si rifarebbe la presa visione.
    """
    path = folder / INDEX_FILENAME
    path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def registered_as(invoice: Invoice, index: dict[str, str]) -> str | None:
    """Nome del file con cui questa fattura risulta già scaricata, se risulta.

    L'indice è la fonte di verità su cosa abbiamo già preso, e il controllo
    deve avvenire PRIMA di aprire il dettaglio: è l'apertura stessa a
    registrare la presa visione sulle fatture ricevute.

    Perché serve un indice invece di guardare i file: il nome lo sceglie il
    portale (partita IVA del trasmittente + un progressivo dello SdI) e non è
    ricavabile dai dati che abbiamo in mano.
    """
    return index.get(invoice.detail_id)


def stored_file(folder: Path, filename: str) -> Path | None:
    """Il file, se è ancora nell'archivio locale.

    Che non ci sia non vuol dire che non sia stato scaricato: gli XML vengono
    consegnati al commercialista e la cartella si svuota. Chi chiama distingue
    i due casi solo per dirlo a schermo, non per decidere se riscaricare.
    """
    path = folder / filename
    return path if path.exists() else None
