"""Lettura dell'elenco fatture: dalla tabella HTML a dati veri.

Il modulo è diviso in due parti, e la divisione è voluta:

- funzioni **pure** (`parse_amount`, `parse_date`, `split_client`, `filter_invoices`,
  `format_invoices`): non sanno niente del browser, prendono stringhe e
  restituiscono dati. Sono quelle coperte dai test, perché si possono provare
  in un millesimo di secondo senza collegarsi a niente;
- funzioni che **leggono la pagina** (`read_invoices`): quelle vanno provate
  sul portale, e non sono testabili in automatico.

Tenere la logica fuori dal browser è la ragione per cui un errore di calcolo
lo scopriamo con un test invece che con un login.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout

from extr_ade.constants import (
    CLIENT_FULL_TEXT_SELECTOR,
    COLUMN_CLIENT,
    COLUMN_DATE,
    COLUMN_NUMBER,
    COLUMN_SDI,
    COLUMN_TAX,
    COLUMN_TAXABLE,
    DETAIL_BUTTON_PREFIX,
    FIRST_PAGE_NAME,
    INVOICE_CELL_SELECTOR,
    INVOICE_ROW_SELECTOR,
    NEXT_PAGE_NAME,
    PAGINATION_SELECTOR,
)
from extr_ade.login import describe_page

# Limite di sicurezza sul numero di pagine da sfogliare. Se la paginazione si
# comportasse in modo imprevisto (per esempio un "avanti" che non avanza),
# senza questo il programma girerebbe per sempre in silenzio.
MAX_PAGES = 200

# Quanto aspettiamo che la tabella si riempia. La pagina è scritta in
# AngularJS: il server risponde, e solo dopo il browser costruisce le righe.
TABLE_TIMEOUT_MS = 30_000

# Quante volte aspettiamo prima di dichiarare il guasto. Vedi `wait_for_table`.
TABLE_ATTEMPTS = 2


@dataclass(frozen=True)
class Invoice:
    """Una riga dell'elenco fatture."""

    number: str
    issue_date: date
    client_id: str  # P.IVA o codice fiscale
    client_name: str
    taxable: Decimal  # imponibile
    tax: Decimal  # imposta
    sdi_id: str
    detail_id: str  # identificativo usato dal portale, es. "0FPR00000000001"

    @property
    def total(self) -> Decimal:
        """Imponibile + imposta."""
        return self.taxable + self.tax


# --- funzioni pure ------------------------------------------------------------


def parse_amount(text: str) -> Decimal:
    """Converte un importo scritto all'italiana in Decimal.

    "1.234,56" -> Decimal("1234.56")

    Usiamo Decimal e non float perché sui float 0.1 + 0.2 non fa 0.3: su
    importi fiscali gli errori si accumulano e i totali smettono di quadrare.

    Una cella vuota vale zero: nella tabella capita (per esempio l'imposta di
    una fattura senza IVA) e non è un errore.

    Dal 2026-09-22 la tabella scrive gli importi con la valuta ("20,90 €"),
    prima erano numeri nudi. Togliamo simbolo e spazi prima di convertire.
    Fra gli spazi c'è anche quello unificatore (`\xa0`), che il portale usa
    davanti all'euro: a schermo è identico a uno spazio normale, quindi un
    `replace(" ", "")` da solo non basterebbe e l'errore sarebbe di quelli che
    si guardano dieci minuti senza vedere niente di strano.
    """
    cleaned = text.strip()
    for junk in ("€", "\xa0", "\u202f", " "):
        cleaned = cleaned.replace(junk, "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    if not cleaned:
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError(f"Importo non riconosciuto: {text!r}") from error


def parse_date(text: str) -> date:
    """Converte una data gg/mm/aaaa in `date`."""
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except ValueError as error:
        raise ValueError(f"Data non riconosciuta: {text!r}") from error


# Come il portale separa l'identificativo dalla denominazione, in ordine di
# tentativo. L'a capo è la forma della tabella nuova (2026-09-22); il trattino
# era quella vecchia, e lo teniamo perché il testo del cliente può arrivare
# dallo span per screen reader (CLIENT_FULL_TEXT_SELECTOR), dove il formato
# non è stato verificato dopo la riscrittura.
CLIENT_SEPARATORS = ("\n", " - ")


def split_client(text: str) -> tuple[str, str]:
    """Divide "P.IVA + Denominazione" in identificativo e nome.

    Dividiamo alla PRIMA occorrenza del separatore: anche la denominazione può
    contenere trattini ("Rossi - Bianchi snc"), e dividere ovunque troncherebbe
    il nome.

    Se nessun separatore aggancia, restituiamo tutto come identificativo e nome
    vuoto. È un caso legittimo (a volte il portale ha solo la P.IVA), ma è anche
    come si manifesterebbe un terzo formato che non conosciamo: nell'elenco a
    terminale vedresti la P.IVA al posto della ragione sociale.
    """
    text = text.strip()
    for separator in CLIENT_SEPARATORS:
        identifier, found, name = text.partition(separator)
        if found:
            return identifier.strip(), name.strip()
    return text, ""


def filter_invoices(
    invoices: list[Invoice],
    *,
    client: str | None = None,
    number: str | None = None,
    issue_date: date | None = None,
) -> list[Invoice]:
    """Filtra le fatture già scaricate dal portale.

    Questi filtri agiscono sui RISULTATI, non sulla ricerca del sito: il form
    dell'Agenzia non permette di cercare per nome del cliente né per numero
    (accetta solo P.IVA o codice fiscale). Conseguenza pratica: restringi
    prima con le date, altrimenti il sito ci manda migliaia di righe che noi
    buttiamo via subito dopo.

    Il confronto su cliente e numero è per sottostringa e senza distinzione
    fra maiuscole e minuscole: cercando "lacona" trovi "Lacona gestioni srls".
    """
    result = invoices

    if client:
        needle = client.strip().lower()
        result = [
            invoice
            for invoice in result
            if needle in invoice.client_name.lower() or needle in invoice.client_id.lower()
        ]

    if number:
        needle = number.strip().lower()
        result = [invoice for invoice in result if needle in invoice.number.lower()]

    if issue_date:
        result = [invoice for invoice in result if invoice.issue_date == issue_date]

    return result


def format_invoices(invoices: list[Invoice]) -> str:
    """Compone l'elenco numerato da mostrare a terminale.

    Numerazione da 0: sono indici della lista, ed è con quelli che poi
    sceglierai cosa scaricare (per esempio "0-20").
    """
    if not invoices:
        return "Nessuna fattura trovata."

    lines = [
        f"{'#':>4}  {'DATA':<10}  {'NUMERO':<18}  {'IMPONIBILE':>12}  {'IMPOSTA':>9}  CLIENTE",
        "-" * 100,
    ]
    for index, invoice in enumerate(invoices):
        lines.append(
            f"{index:>4}  "
            f"{invoice.issue_date:%d/%m/%Y}  "
            f"{invoice.number[:18]:<18}  "
            f"{invoice.taxable:>12}  "
            f"{invoice.tax:>9}  "
            f"{invoice.client_name[:40] or invoice.client_id}"
        )
    lines.append("-" * 100)
    lines.append(f"{len(invoices)} fatture.")
    return "\n".join(lines)


# --- lettura dalla pagina -----------------------------------------------------


def wait_for_table(page: Page) -> bool:
    """Aspetta che la tabella dei risultati sia pronta.

    Restituisce True se ci sono righe, False se la ricerca non ha prodotto
    risultati.

    La distinzione conta: "nessuna fattura in questo periodo" e "la tabella non
    è mai comparsa" a schermo si assomigliano, ma il primo è un risultato
    legittimo e il secondo è un guasto da sistemare. Confonderli significa
    concludere "non ho fatture" quando invece il programma è rotto.

    Il guasto visto il 2026-08-06 era questo: la vista AngularJS della rotta
    non si abilitava (`vm.canShow` restava falsa) e la tabella non veniva
    proprio disegnata. Non era lentezza, e infatti riprovare non bastava.
    Ricaricando la pagina l'applicazione riparte da zero sulla stessa rotta,
    ed è il nostro ultimo tentativo prima di arrenderci.

    ATTENZIONE, e vale la pena saperlo prima di stupirsi: il ricaricamento
    riporta il form di ricerca ai valori predefiniti. Se avevi cercato per
    date, dopo il recupero l'elenco è quello del periodo di default. Per
    questo il recupero lo diciamo a schermo invece di farlo di nascosto: i
    dati che leggi dopo potrebbero non essere quelli che avevi chiesto.
    """
    result = _wait_for_rows(page, TABLE_ATTEMPTS)
    if result is not None:
        return result

    print("  la tabella non compare: ricarico la pagina e riprovo...")
    print("  (se avevi impostato delle date, vanno reimpostate)")
    page.reload(wait_until="domcontentloaded")

    result = _wait_for_rows(page, 1)
    if result is not None:
        print("  recuperata: la tabella c'è dopo il ricaricamento.")
        return result

    # Qui non c'è più niente da tentare. Salviamo l'HTML del momento esatto:
    # senza, resta solo un messaggio d'errore, e l'unica volta che è successo
    # ha mandato a controllare un selettore che era invece corretto.
    describe_page(page, save_html_as="elenco_senza_tabella.html")
    raise RuntimeError(
        "Tabella dei risultati non comparsa, nemmeno dopo aver ricaricato la "
        "pagina. Guarda l'HTML appena salvato: dice se la pagina è quella "
        "giusta (allora è il portale che non risponde) o un'altra (allora è "
        "la navigazione)."
    ) from None


def _wait_for_rows(page: Page, attempts: int) -> bool | None:
    """Aspetta le righe per un dato numero di tentativi.

    Tre esiti diversi, e servono tutti e tre distinti:
    True  = ci sono righe;
    False = c'è una tabella ma è vuota (nessuna fattura nel periodo);
    None  = non c'è proprio una tabella, cioè non è un risultato ma un guasto.
    """
    for attempt in range(1, attempts + 1):
        try:
            page.wait_for_selector(INVOICE_ROW_SELECTOR, timeout=TABLE_TIMEOUT_MS)
            return True
        except PlaywrightTimeout:
            if page.locator("table").count():
                return False  # la tabella c'è, semplicemente è vuota
            if attempt < attempts:
                print("  tabella non ancora comparsa, riprovo una volta...")
    return None


def parse_selection(text: str, count: int) -> list[int]:
    """Interpreta una selezione tipo "0-20", "3", "0,4,7" o "0-5,9".

    Gli indici sono quelli mostrati nell'elenco e partono da 0.

    Un indice fuori intervallo è un errore e non viene ignorato in silenzio:
    se scrivi "0-100" avendo 30 fatture, probabilmente ti aspetti 101 file e
    devi sapere che non è quello che otterrai.
    """
    if not text.strip():
        return []

    selected: list[int] = []
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue

        if "-" in piece.lstrip("-"):
            start_text, _, end_text = piece.partition("-")
            start, end = _to_index(start_text, count), _to_index(end_text, count)
            if start > end:
                raise ValueError(f"Intervallo al contrario: {piece!r}")
            selected.extend(range(start, end + 1))
        else:
            selected.append(_to_index(piece, count))

    # Ordinati e senza ripetizioni: scrivere "0-5,3" non deve scaricare due
    # volte la fattura 3.
    return sorted(set(selected))


def _to_index(text: str, count: int) -> int:
    """Converte un pezzo di selezione in indice valido."""
    text = text.strip()
    if not text.isdigit():
        raise ValueError(f"Non è un numero: {text!r}")

    index = int(text)
    if not 0 <= index < count:
        raise ValueError(f"Indice {index} fuori intervallo (ce ne sono {count}: 0-{count - 1})")
    return index


def read_invoices(page: Page) -> list[Invoice]:
    """Legge tutte le fatture, sfogliando anche le pagine successive.

    La tabella mostra un blocco di righe per volta. Fermarsi alla prima pagina
    darebbe un elenco incompleto senza nessun segnale di errore: crederesti di
    aver scaricato tutto.
    """
    if not wait_for_table(page):
        return []

    invoices: list[Invoice] = []

    for page_number in range(1, MAX_PAGES + 1):
        invoices.extend(_read_current_page(page))
        print(f"  pagina {page_number}: {len(invoices)} fatture lette finora")

        if not _go_to_next_page(page):
            break
    else:
        raise RuntimeError(
            f"Superate {MAX_PAGES} pagine: la paginazione non si comporta come "
            f"previsto, mi fermo invece di continuare all'infinito."
        )

    # Torniamo alla prima pagina prima di restituire il risultato.
    #
    # Senza questo la tabella resta ferma sull'ultima pagina sfogliata, mentre
    # l'elenco che restituiamo le contiene tutte: chi poi cerca "la riga
    # numero 3" nella pagina ne trova una diversa da quella che ha davanti.
    # È già successo, e l'errore era incomprensibile.
    _go_to_first_page(page)

    return invoices


def _read_current_page(page: Page) -> list[Invoice]:
    """Trasforma in `Invoice` le righe visibili in questo momento."""
    return [_read_row(row) for row in page.locator(INVOICE_ROW_SELECTOR).all()]


def read_detail_id(row: Locator) -> str:
    """Legge l'identificativo della fattura dal bottone "Dettaglio Fattura".

    Dal 2026-09-22 è l'unico posto della riga dove l'identificativo compare in
    forma utilizzabile: il link con l'indirizzo non esiste più.

    Restituisce stringa vuota se il bottone non c'è. Non è un errore da fermare
    tutto: la riga si legge lo stesso, semplicemente quella fattura non si potrà
    aprire. Chi scarica se ne accorge, perché senza identificativo non c'è nulla
    da cliccare.
    """
    button = row.get_by_role("button", name=DETAIL_BUTTON_PREFIX)
    if not button.count():
        return ""

    # Il nome accessibile può venire dall'aria-label oppure dal testo dentro al
    # bottone: proviamo il primo e ripieghiamo sul secondo, invece di dare per
    # scontato quale dei due usi il portale.
    label = button.first.get_attribute("aria-label") or button.first.inner_text()
    return label.strip().removeprefix(DETAIL_BUTTON_PREFIX).strip()


def _read_row(row) -> Invoice:  # type: ignore[no-untyped-def]  # Locator di Playwright
    """Legge una singola riga della tabella."""
    cells = row.locator(INVOICE_CELL_SELECTOR)

    client_cell = cells.nth(COLUMN_CLIENT)
    screen_reader_text = client_cell.locator(CLIENT_FULL_TEXT_SELECTOR)
    # Lo span per screen reader è l'unico che contiene anche la denominazione;
    # se mancasse, ripieghiamo sul testo della cella (solo P.IVA).
    raw_client = (
        screen_reader_text.first.inner_text()
        if screen_reader_text.count()
        else client_cell.inner_text()
    )
    client_id, client_name = split_client(raw_client)

    detail_id = read_detail_id(row)

    return Invoice(
        number=cells.nth(COLUMN_NUMBER).inner_text().strip(),
        issue_date=parse_date(cells.nth(COLUMN_DATE).inner_text()),
        client_id=client_id,
        client_name=client_name,
        taxable=parse_amount(cells.nth(COLUMN_TAXABLE).inner_text()),
        tax=parse_amount(cells.nth(COLUMN_TAX).inner_text()),
        sdi_id=cells.nth(COLUMN_SDI).inner_text().strip(),
        detail_id=detail_id,
    )


def detail_button(page: Page, detail_id: str) -> Locator | None:
    """Trova il bottone del dettaglio, sfogliando la tabella se serve.

    Il click funziona solo sui bottoni della pagina di tabella visualizzata in
    quel momento: le altre righe non sono nascoste, non esistono proprio nel
    DOM. Siccome dopo la lettura la tabella torna alla prima pagina, tutto
    quello che sta oltre la cinquantesima riga era irraggiungibile.

    Cerchiamo prima dove siamo, poi in avanti, e solo se non basta
    ricominciamo dalla prima pagina. Nell'uso normale le fatture arrivano
    nell'ordine dell'elenco, quindi si avanza di una pagina per volta e il
    giro completo sfoglia la tabella una volta sola.

    Il giro da capo non è un lusso: mentre lavoriamo il portale riceve fatture
    nuove (durante una sola sessione l'elenco è passato da 238 a 241), le righe
    slittano fra le pagine, e una fattura può finire INDIETRO rispetto a dove
    siamo arrivati. Cercare per nome ci difende dallo slittamento; ripartire da
    capo ci difende dal cercarla solo in avanti.

    Restituisce None se non c'è in nessuna pagina: a quel punto vuol dire
    davvero che non è più nell'elenco.
    """
    name = f"{DETAIL_BUTTON_PREFIX} {detail_id}"

    def here() -> Locator | None:
        button = page.get_by_role("button", name=name, exact=True)
        return button.first if button.count() else None

    def scan_forward() -> Locator | None:
        while _go_to_next_page(page):
            found = here()
            if found is not None:
                return found
        return None

    found = here() or scan_forward()
    if found is not None:
        return found

    _go_to_first_page(page)
    return here() or scan_forward()


def _pagination_button(page: Page, name: str) -> Locator | None:
    """Il bottone di paginazione con quel nome, se c'è ed è utilizzabile.

    Restituisce None quando la paginazione non esiste (una pagina sola), quando
    il bottone non c'è, o quando è disabilitato — cioè siamo già al capo della
    fila. Dal 2026-09-22 la disabilitazione è l'attributo `disabled` sul bottone
    stesso: prima bisognava leggere la classe del <li> genitore.
    """
    navigation = page.locator(PAGINATION_SELECTOR)
    if not navigation.count():
        return None

    button = navigation.first.get_by_role("button", name=name, exact=True)
    if not button.count() or button.first.is_disabled():
        return None

    return button.first


def _go_to_first_page(page: Page) -> None:
    """Riporta la tabella alla prima pagina, se c'è una paginazione."""
    button = _pagination_button(page, FIRST_PAGE_NAME)
    if button is None:
        return  # una pagina sola, oppure ci siamo già

    button.click()
    page.wait_for_load_state("networkidle")


def _go_to_next_page(page: Page) -> bool:
    """Va alla pagina successiva. Restituisce False se eravamo all'ultima.

    Dopo il click aspettiamo che la PRIMA RIGA cambi, non solo che la rete si
    calmi. La tabella si ridisegna da sola senza ricaricare la pagina, quindi
    `networkidle` può dirsi soddisfatto mentre a schermo ci sono ancora le
    righe di prima: le rileggeremmo, e il giro finirebbe con dei doppioni e
    delle fatture mai viste. Aspettare un cambiamento vero toglie il dubbio.
    """
    button = _pagination_button(page, NEXT_PAGE_NAME)
    if button is None:
        return False

    rows = page.locator(INVOICE_ROW_SELECTOR)
    before = rows.first.inner_text() if rows.count() else ""

    button.click()
    page.wait_for_load_state("networkidle")

    if before:
        page.wait_for_function(
            """([selector, before]) => {
                const row = document.querySelector(selector);
                return row && row.innerText !== before;
            }""",
            arg=[INVOICE_ROW_SELECTOR, before],
        )

    return True
