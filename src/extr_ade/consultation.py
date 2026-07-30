"""Accesso alla consultazione delle fatture elettroniche.

Rifà l'intero percorso di ingresso riusando i moduli precedenti
(`credentials` -> `login` -> `identity`) e poi apre la sezione dove si
sfogliano le fatture emesse e ricevute.

Per ora si ferma lì e stampa cosa ha trovato: la scelta emesse/ricevute e la
lista delle fatture non sono ancora state osservate, quindi non c'è niente da
automatizzare senza inventare.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from playwright.sync_api import Page

from extr_ade.constants import (
    CONSULTATION_LINK_NAME,
    CONSULTATION_PATH,
    DATE_FROM_SELECTOR,
    DETAIL_ROUTE,
    DOWNLOAD_BUTTON_NAME,
    DATE_TO_SELECTOR,
    ISSUED_LINK_NAME,
    ISSUED_ROUTE,
    RECEIVED_LINK_NAME,
    RECEIVED_ROUTE,
    SEARCH_BUTTON_NAME,
)
from extr_ade.downloader import destination_dir, download_all
from extr_ade.invoices import (
    Invoice,
    filter_invoices,
    format_invoices,
    parse_date,
    parse_selection,
    read_invoices,
)
from extr_ade.credentials import get_credentials
from extr_ade.identity import (
    UserType,
    ask_user_type,
    choose_delegator,
    confirm_identity,
    select_user_type,
)
from extr_ade.login import browser_page, describe_page, login_failed, perform_login

# Quanto aspettiamo che si apra un'eventuale nuova scheda prima di concludere
# che il link si apre nella stessa. Basso di proposito: se la scheda non c'è,
# non ha senso stare fermi un minuto per scoprirlo.
NEW_TAB_TIMEOUT_MS = 5_000


def enter_workspace(page: Page) -> None:
    """Percorso completo di ingresso: login, scelta utenza, conferma.

    È il pezzo che verrà riusato da qualunque comando: scaricare le fatture,
    consultarle, o quello che aggiungeremo dopo.
    """
    credentials = get_credentials()
    user_type = ask_user_type()

    print("\nLogin in corso (headless)...")
    perform_login(page, credentials)

    if login_failed(page):
        describe_page(page, save_html_as="login_fallito.html")
        raise SystemExit("Login non riuscito.")

    print("Login riuscito. Configuro l'utenza di lavoro...")
    select_user_type(page, user_type)

    if user_type is UserType.DELEGATE:
        choose_delegator(page)

    confirm_identity(page)
    print("Utenza configurata.")


def open_consultation(page: Page) -> Page:
    """Apre "Fatture elettroniche e altri dati IVA".

    Restituisce la pagina in cui è finita la consultazione: se il link apre
    una scheda nuova, è una pagina diversa da quella ricevuta. Ignorarlo
    significherebbe continuare a interrogare la vecchia pagina e concludere
    "non trovo niente", che come messaggio d'errore manda fuori strada.
    """
    link = page.get_by_role("link", name=CONSULTATION_LINK_NAME)
    opens_new_tab = link.get_attribute("target") == "_blank"

    if opens_new_tab:
        with page.context.expect_page(timeout=NEW_TAB_TIMEOUT_MS) as new_tab:
            link.click()
        consultation = new_tab.value
        consultation.wait_for_load_state("networkidle")
    else:
        link.click()
        page.wait_for_url(f"**{CONSULTATION_PATH}**")
        page.wait_for_load_state("networkidle")
        consultation = page

    return consultation


class InvoiceKind(StrEnum):
    """Quali fatture guardare: quelle che hai emesso o quelle che hai ricevuto."""

    ISSUED = "emesse"
    RECEIVED = "ricevute"


# Per ogni tipo: come si chiama la voce di menu e quale rotta deve comparire
# nell'indirizzo dopo il click.
SECTIONS: dict[InvoiceKind, tuple[str, str]] = {
    InvoiceKind.ISSUED: (ISSUED_LINK_NAME, ISSUED_ROUTE),
    InvoiceKind.RECEIVED: (RECEIVED_LINK_NAME, RECEIVED_ROUTE),
}


def ask_invoice_kind() -> InvoiceKind:
    """Chiede da terminale quali fatture consultare."""
    kinds = list(SECTIONS)

    print("\nQuali fatture vuoi consultare?")
    for number, kind in enumerate(kinds):
        print(f"  {number}  Fatture {kind.value}")

    last = len(kinds) - 1
    while True:
        answer = input(f"Scegli [0-{last}]: ").strip()
        if answer.isdigit() and 0 <= int(answer) <= last:
            return kinds[int(answer)]
        print(f"  -> rispondi con un numero fra 0 e {last}.")


def go_to_route(page: Page, route: str) -> None:
    """Sposta l'applicazione su una rotta interna (la parte dopo il `#`).

    Perché non clicchiamo la voce di menu, che sarebbe la cosa più naturale:
    dalla pagina di dettaglio il menu di sinistra è richiuso, e il link "Le tue
    fatture emesse" non è cliccabile. Il click funzionava solo finché
    restavamo sull'elenco.

    Aspettiamo controllando l'indirizzo e non un evento di navigazione: qui la
    pagina non ricarica mai, quindi quell'evento non arriverebbe.
    """
    page.evaluate("hash => { window.location.hash = hash; }", route)
    page.wait_for_function("r => window.location.hash.startsWith(r)", arg=route)
    page.wait_for_load_state("networkidle")


def open_invoice_list(page: Page, kind: InvoiceKind) -> None:
    """Apre la sezione delle fatture emesse o ricevute."""
    _, route = SECTIONS[kind]
    go_to_route(page, route)


@dataclass(frozen=True)
class SearchOptions:
    """Cosa cercare. Le domande si fanno tutte prima di aprire il browser.

    I due gruppi non sono la stessa cosa:

    - `date_from` / `date_to` finiscono nel form del sito, che restringe i
      risultati alla fonte;
    - `number` e `client` sono filtri nostri, applicati alle righe già
      scaricate, perché il form dell'Agenzia non li offre.
    """

    date_from: str | None = None
    date_to: str | None = None
    number: str | None = None
    client: str | None = None
    issue_date: date | None = None


def ask_optional(prompt: str) -> str | None:
    """Domanda facoltativa: Invio a vuoto significa "nessun filtro"."""
    answer = input(f"{prompt} (Invio per saltare): ").strip()
    return answer or None


def ask_date(prompt: str) -> str | None:
    """Come `ask_optional`, ma controlla subito che la data sia scritta bene.

    Meglio accorgersene ora che dopo tre minuti di login, con il portale che
    risponde "nessun risultato" senza spiegare che la data era sbagliata.
    """
    while True:
        answer = ask_optional(f"{prompt} [gg/mm/aaaa]")
        if answer is None:
            return None
        try:
            parse_date(answer)
            return answer
        except ValueError as error:
            print(f"  -> {error}")


def ask_search_options() -> SearchOptions:
    """Raccoglie tutti i filtri da terminale."""
    print("\nRicerca (tutti i campi sono facoltativi)")
    print("  Le date restringono la ricerca sul sito: usale per non scaricare")
    print("  anni di fatture che poi butti via.")

    date_from = ask_date("  Data emissione dal")
    date_to = ask_date("  Data emissione al")

    print("\n  Filtri applicati ai risultati:")
    number = ask_optional("  Numero fattura")
    client = ask_optional("  Cliente (nome o P.IVA)")
    issue_date_text = ask_date("  Data fattura esatta")

    return SearchOptions(
        date_from=date_from,
        date_to=date_to,
        number=number,
        client=client,
        issue_date=parse_date(issue_date_text) if issue_date_text else None,
    )


def search_by_date(page: Page, date_from: str | None, date_to: str | None) -> None:
    """Compila il form di ricerca per data e avvia la ricerca.

    Se non è stata indicata nessuna data non tocchiamo niente: la sezione
    arriva già con un risultato di default, e cliccare "Cerca" a vuoto
    significherebbe solo aspettare una risposta identica.
    """
    if not (date_from or date_to):
        return

    if date_from:
        page.fill(DATE_FROM_SELECTOR, date_from)
    if date_to:
        page.fill(DATE_TO_SELECTOR, date_to)

    page.get_by_role("button", name=SEARCH_BUTTON_NAME).click()
    page.wait_for_load_state("networkidle")


class Action(StrEnum):
    """Cosa si può fare davanti all'elenco."""

    FILTER = "Filtra la lista"
    DOWNLOAD = "Scarica fatture"
    SWITCH = "Cambia sezione (emesse/ricevute)"
    RELOAD = "Ricarica l'elenco dal portale"
    QUIT = "Esci"


def ask_action() -> Action:
    """Menu principale, ripresentato dopo ogni operazione."""
    actions = list(Action)

    print("\nCosa vuoi fare?")
    for number, action in enumerate(actions):
        print(f"  {number}  {action.value}")

    last = len(actions) - 1
    while True:
        answer = input(f"Scegli [0-{last}]: ").strip()
        if answer.isdigit() and 0 <= int(answer) <= last:
            return actions[int(answer)]
        print(f"  -> rispondi con un numero fra 0 e {last}.")


def load_section(page: Page, kind: InvoiceKind) -> list[Invoice]:
    """Apre una sezione e ne legge tutte le fatture."""
    print(f"\nApro la sezione: fatture {kind.value}...")
    open_invoice_list(page, kind)

    print("Leggo l'elenco...")
    return read_invoices(page)


def ask_selection(invoices: list[Invoice]) -> list[Invoice]:
    """Chiede quali fatture scaricare, per indice."""
    if not invoices:
        print("Non c'è niente da scaricare.")
        return []

    print(f"\nIndica quali fatture (0-{len(invoices) - 1}).")
    print('Esempi: "0-20" per un intervallo, "3" per una sola, "0,4,7" per più fatture.')

    while True:
        answer = input("Selezione (Invio per annullare): ").strip()
        if not answer:
            return []
        try:
            return [invoices[index] for index in parse_selection(answer, len(invoices))]
        except ValueError as error:
            print(f"  -> {error}")


def open_detail(page: Page, invoice: Invoice) -> None:
    """Apre la pagina di dettaglio di una fattura.

    Andiamo diretti alla rotta invece di cliccare il link nella tabella:
    dopo aver letto tutte le pagine la tabella resta sull'ultima, quindi
    "il link numero 3" non è la fattura numero 3 dell'elenco che hai davanti.

    Aspettiamo il bottone di download e non il caricamento generico: il
    contenuto del dettaglio arriva dopo, e senza questa attesa si finisce a
    guardare una pagina ancora vuota (già successo in diagnostica).
    """
    go_to_route(page, f"{DETAIL_ROUTE}{invoice.detail_id}")
    page.wait_for_selector(
        f"button:has-text('{DOWNLOAD_BUTTON_NAME}')", state="visible"
    )


def main() -> None:
    with browser_page() as page:
        enter_workspace(page)

        print("Apro la consultazione delle fatture...")
        consultation = open_consultation(page)

        kind = ask_invoice_kind()
        invoices = load_section(consultation, kind)
        shown = invoices

        while True:
            print(f"\nFatture {kind.value}\n")
            print(format_invoices(shown))
            if len(shown) != len(invoices):
                print(f"({len(invoices)} lette dal portale, {len(shown)} dopo i filtri)")

            match ask_action():
                case Action.FILTER:
                    options = ask_search_options()
                    if options.date_from or options.date_to:
                        # Le date si applicano sul sito: va rifatta la ricerca
                        # e riletta la tabella, non basta filtrare in memoria.
                        search_by_date(consultation, options.date_from, options.date_to)
                        print("Rileggo l'elenco...")
                        invoices = read_invoices(consultation)
                    shown = filter_invoices(
                        invoices,
                        client=options.client,
                        number=options.number,
                        issue_date=options.issue_date,
                    )

                case Action.DOWNLOAD:
                    chosen = ask_selection(shown)
                    if chosen:
                        print(f"\nScarico {len(chosen)} fatture...")
                        saved = download_all(
                            consultation, chosen, kind.value, open_detail
                        )
                        print(f"\n{len(saved)} file su {len(chosen)} in {destination_dir(kind.value)}")

                        # Il download ci lascia sull'ultimo dettaglio aperto:
                        # per continuare a lavorare serve tornare all'elenco.
                        invoices = load_section(consultation, kind)
                        shown = invoices

                case Action.SWITCH:
                    kind = ask_invoice_kind()
                    invoices = load_section(consultation, kind)
                    shown = invoices

                case Action.RELOAD:
                    invoices = load_section(consultation, kind)
                    shown = invoices

                case Action.QUIT:
                    print("Ciao.")
                    return


if __name__ == "__main__":
    main()
