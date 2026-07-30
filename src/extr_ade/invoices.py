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

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from extr_ade.constants import (
    CLIENT_FULL_TEXT_SELECTOR,
    COLUMN_CLIENT,
    COLUMN_DATE,
    COLUMN_NUMBER,
    COLUMN_SDI,
    COLUMN_TAX,
    COLUMN_TAXABLE,
    DETAIL_LINK_SELECTOR,
    FIRST_PAGE_NAME,
    INVOICE_CELL_SELECTOR,
    INVOICE_ROW_SELECTOR,
    NEXT_PAGE_NAME,
    PAGINATION_SELECTOR,
)

# Limite di sicurezza sul numero di pagine da sfogliare. Se la paginazione si
# comportasse in modo imprevisto (per esempio un "avanti" che non avanza),
# senza questo il programma girerebbe per sempre in silenzio.
MAX_PAGES = 200

# Quanto aspettiamo che la tabella si riempia. La pagina è scritta in
# AngularJS: il server risponde, e solo dopo il browser costruisce le righe.
TABLE_TIMEOUT_MS = 30_000


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
    """
    cleaned = text.strip().replace(".", "").replace(",", ".")
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


def split_client(text: str) -> tuple[str, str]:
    """Divide "P.IVA - Denominazione" in identificativo e nome.

    Il separatore è " - ", ma anche la denominazione può contenere trattini
    ("Rossi - Bianchi snc"): dividiamo quindi solo alla PRIMA occorrenza, o il
    nome verrebbe troncato.
    """
    identifier, separator, name = text.strip().partition(" - ")
    if not separator:
        # Nessun nome: capita quando il portale ha solo l'identificativo.
        return identifier.strip(), ""
    return identifier.strip(), name.strip()


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
    """
    try:
        page.wait_for_selector(INVOICE_ROW_SELECTOR, timeout=TABLE_TIMEOUT_MS)
        return True
    except PlaywrightTimeout:
        if page.locator("table").count():
            return False  # la tabella c'è, semplicemente è vuota
        raise RuntimeError(
            "Tabella dei risultati non trovata. La pagina potrebbe essere "
            "cambiata: ricontrolla INVOICE_ROW_SELECTOR in constants.py."
        ) from None


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

    detail_link = row.locator(DETAIL_LINK_SELECTOR)
    detail_id = ""
    if detail_link.count():
        href = detail_link.first.get_attribute("href") or ""
        detail_id = href.rsplit("/", 1)[-1]

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


def _go_to_first_page(page: Page) -> None:
    """Riporta la tabella alla prima pagina, se c'è una paginazione."""
    navigation = page.locator(PAGINATION_SELECTOR)
    if not navigation.count():
        return

    first_link = navigation.get_by_role("link", name=FIRST_PAGE_NAME)
    if not first_link.count():
        return

    container_class = first_link.first.locator("xpath=..").get_attribute("class") or ""
    if "disabled" in container_class:
        return  # già sulla prima pagina

    first_link.first.click()
    page.wait_for_load_state("networkidle")


def _go_to_next_page(page: Page) -> bool:
    """Va alla pagina successiva. Restituisce False se eravamo all'ultima."""
    navigation = page.locator(PAGINATION_SELECTOR)
    if not navigation.count():
        return False

    next_link = navigation.get_by_role("link", name=NEXT_PAGE_NAME)
    if not next_link.count():
        return False

    # Il sito segna l'ultima pagina mettendo la classe `disabled` sul <li> che
    # contiene il link, non sul link stesso: guardiamo il genitore.
    container_class = next_link.first.locator("xpath=..").get_attribute("class") or ""
    if "disabled" in container_class:
        return False

    next_link.first.click()
    page.wait_for_load_state("networkidle")
    return True
