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
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout

from extr_ade.constants import (
    CONSULTATION_LINK_NAME,
    CONSULTATION_PATH,
    DATE_FROM_SELECTOR,
    DOWNLOAD_BUTTON_NAME,
    DATE_TO_SELECTOR,
    DETAIL_OPEN_TIMEOUT_MS,
    FILE_PENDING_SELECTOR,
    ISSUED_LINK_NAME,
    ISSUED_ROUTE,
    RECEIVED_LINK_NAME,
    RECEIVED_ROUTE,
    SEARCH_BUTTON_NAME,
    SEARCH_HEADING_TIMEOUT_MS,
    SEARCHED_PERIOD_TEXT,
)
from extr_ade.downloader import FilePendingError, download_all, safe_filename
from extr_ade.invoices import (
    Invoice,
    detail_button,
    filter_invoices,
    format_invoices,
    parse_date,
    parse_selection,
    read_invoices,
    wait_for_table,
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
    """Sposta l'applicazione su una rotta interna.

    Perché non clicchiamo la voce di menu, che sarebbe la cosa più naturale:
    dalla pagina di dettaglio il menu di sinistra è richiuso, e il link "Le tue
    fatture emesse" non è cliccabile. Il click funzionava solo finché
    restavamo sull'elenco.

    2026-09-22: il portale ha sostituito l'applicazione di consultazione.
    Quella vecchia era AngularJS e navigava per hash (`#/fatture/emesse`),
    quella nuova è una SPA con routing a *path* (`/cons/cons-web/fatture/
    emesse`, come si legge negli href del menu). Scrivere `location.hash` non
    naviga più: l'hash viene appiccicato alla home e basta, e siccome scrivere
    l'hash riesce sempre, la vecchia attesa si diceva soddisfatta mentre la
    pagina era rimasta ferma. Il guasto si vedeva più avanti, come "tabella
    non comparsa".

    Ora navighiamo al path e aspettiamo l'indirizzo. Le rotte in `constants`
    sono rimaste nella vecchia forma con il `#`: lo togliamo qui, in un punto
    solo, invece di cambiare le costanti e dover toccare anche i richiami.
    """
    path = CONSULTATION_PATH + route.lstrip("#")
    page.goto(urljoin(page.url, path))
    page.wait_for_url(f"**{path}**")
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


def to_input_date(text: str) -> str:
    """Converte una data all'italiana nel formato che vuole il campo del sito.

    "01/07/2026" -> "2026-07-01"

    Dal 2026-09-22 `#dal` e `#al` sono `<input type="date">`. Un campo di quel
    tipo accetta SOLO la forma `aaaa-mm-gg`: scriverci dentro "01/07/2026" non
    dà una data sbagliata, fa proprio fallire la scrittura.

    All'utente continuiamo a chiedere gg/mm/aaaa, che è come le date si
    scrivono qui: la traduzione è un problema del programma, non suo.
    """
    return parse_date(text).isoformat()


class DateOutOfRangeError(ValueError):
    """La data chiesta è fuori dall'intervallo che il campo del sito accetta."""


def fill_date(page: Page, selector: str, text: str) -> None:
    """Scrive una data nel form, dopo aver controllato che il sito la accetti.

    Il controllo non è pignoleria. I campi dichiarano `min` e `max` (il 2026
    -09-22: dal 2015-01-01 a oggi), e una data fuori da lì viene scritta senza
    proteste ma **la ricerca viene ignorata in silenzio**: il portale risponde
    col periodo di default e tu lavori su 241 fatture credendo di averne
    chieste 82. È successo, ed è costata mezz'ora di diagnosi.

    Leggiamo `min` e `max` dal campo invece di scriverli qui: sono decisi dal
    portale e cambiano ogni giorno (il `max` è la data odierna).
    """
    wanted = to_input_date(text)

    lowest = page.get_attribute(selector, "min")
    highest = page.get_attribute(selector, "max")
    if lowest and wanted < lowest:
        raise DateOutOfRangeError(
            f"{text}: il portale accetta date a partire dal "
            f"{date.fromisoformat(lowest):%d/%m/%Y}."
        )
    if highest and wanted > highest:
        raise DateOutOfRangeError(
            f"{text}: il portale accetta date fino al "
            f"{date.fromisoformat(highest):%d/%m/%Y}."
        )

    page.fill(selector, wanted)


def search_by_date(page: Page, date_from: str | None, date_to: str | None) -> None:
    """Compila il form di ricerca per data e avvia la ricerca.

    Se non è stata indicata nessuna data non tocchiamo niente: la sezione
    arriva già con un risultato di default, e cliccare "Cerca" a vuoto
    significherebbe solo aspettare una risposta identica.

    NOTA, da verificare sul campo: nel form la data "Dal" è marcata come
    obbligatoria ("Data di emissione Dal : *"). Cercare indicando solo "al"
    potrebbe non funzionare, ma non l'abbiamo provato.
    """
    if not (date_from or date_to):
        return

    if date_from:
        fill_date(page, DATE_FROM_SELECTOR, date_from)
    if date_to:
        fill_date(page, DATE_TO_SELECTOR, date_to)

    # `exact=True` non è un vezzo: senza, il nome viene cercato come
    # sottostringa e nella pagina nuova quattro bottoni contengono "Cerca"
    # ("Ricerca", "Ricerca avanzata", l'aiuto, e quello buono). Playwright si
    # ferma invece di tirare a indovinare, e fa bene.
    #
    # Attenzione a non generalizzare: altrove il match parziale serve, perché
    # il nome accessibile continua oltre la parte che conosciamo (il bottone
    # del dettaglio porta l'identificativo, quello di download ripete il
    # numero Sdi). Lì `exact=True` non troverebbe più niente.
    page.get_by_role("button", name=SEARCH_BUTTON_NAME, exact=True).click()
    page.wait_for_load_state("networkidle")

    print(f"  {searched_period(page)}")


def searched_period(page: Page) -> str:
    """Il periodo che il portale dichiara di aver cercato.

    Nell'intestazione della sezione il sito scrive, per esempio, "Fatture
    individuate (238) nel periodo 01/07/2026 - 22/09/2026". È il portale a
    dirci su cosa ha lavorato: se non coincide con le date chieste, la ricerca
    non è andata dove credevamo.

    Lo stampiamo a ogni ricerca e non solo in diagnostica: una ricerca che
    fallisce in silenzio ti lascia a lavorare sul periodo sbagliato senza
    nessun segnale, ed è il tipo di errore che ti accorgi di aver fatto molto
    dopo.

    Se l'intestazione non si trova non solleviamo niente: è un'informazione in
    più, non deve poter far fallire una ricerca che magari funziona.
    """
    heading = page.get_by_text(SEARCHED_PERIOD_TEXT).first
    try:
        # Va ASPETTATA, non letta al volo: al ritorno dal "Cerca" la pagina si
        # sta ancora ridisegnando, e il primo tentativo (2026-09-22) leggeva il
        # vuoto proprio mentre la ricerca era andata a buon fine. Una riga di
        # controllo che dice il falso è peggio di una riga che manca.
        heading.wait_for(timeout=SEARCH_HEADING_TIMEOUT_MS)
    except PlaywrightTimeout:
        return "(il portale non dichiara il periodo cercato)"

    return " ".join(heading.inner_text().split())


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


class DetailDidNotOpenError(RuntimeError):
    """Il click c'è stato ma il dettaglio non si è aperto.

    Tenuta separata dal caso "il dettaglio è aperto ma manca il bottone di
    download": sono due guasti con due rimedi diversi, e finché il programma
    non li distingueva, il secondo (che un giorno ci dirà che il selettore
    dell'avviso delle 72 ore è da rifare) restava nascosto sotto il primo.
    """


def open_by_click(page: Page, button: Locator, invoice: Invoice) -> None:
    """Clicca il bottone del dettaglio e si assicura che si sia davvero aperto.

    Il 2026-09-22, su un giro di 28 fatture, il primo click non ha avuto alcun
    effetto: siamo rimasti sull'elenco e abbiamo passato i 60 secondi di attesa
    a cercare il bottone di download su una pagina che non era il dettaglio.
    Le due fatture successive, identiche in tutto (stesso giorno, stessa
    consegna, bottone con lo stesso markup), si sono aperte senza problemi.

    Perché quel click si sia perso NON lo sappiamo. L'unica particolarità è che
    era il primo dopo una ricerca per date, cioè su una tabella appena
    ridisegnata. Resta un sospetto, non una diagnosi.

    Per questo il rimedio non prova a indovinare la causa: controlla l'effetto.
    Se l'indirizzo non diventa quello del dettaglio, il click è andato perso e
    se ne fa un altro. Uno solo: se non funziona due volte, il problema non è
    un click perso e insistere non aiuta.
    """
    for attempt in (1, 2):
        button.click()
        try:
            page.wait_for_url(
                lambda url: not on_list_url(url), timeout=DETAIL_OPEN_TIMEOUT_MS
            )
            return
        except PlaywrightTimeout:
            if attempt == 1:
                print(f"    {invoice.number}: il dettaglio non si è aperto, riprovo...")

    raise DetailDidNotOpenError(
        f"{invoice.number}: il dettaglio non si è aperto nemmeno al secondo "
        f"click. Siamo rimasti sull'elenco, quindi non è il bottone di "
        f"download a mancare: è il click a non avere effetto."
    )


class DetailNotOnPageError(RuntimeError):
    """Il bottone del dettaglio non è cliccabile: la fattura non è a schermo.

    Distinta dalle altre perché la causa è nostra (stiamo guardando la pagina
    di tabella sbagliata), non del portale: confonderla con un guasto del sito
    manderebbe a cercare dalla parte sbagliata.
    """


def on_list(page: Page) -> bool:
    """Dice se il browser è sull'elenco delle fatture (e non su un dettaglio).

    Guardiamo l'INDIRIZZO, non il contenuto. Il primo tentativo controllava se
    esistesse una tabella, ed era sbagliato: anche il dettaglio ha tabelle (i
    dati contabili), quindi il controllo rispondeva sempre di sì e il ritorno
    all'elenco non veniva mai eseguito. Il giro scaricava la prima fattura e
    dava tutte le altre per "non più nell'elenco".

    L'elenco sta su `/cons/cons-web/fatture/emesse` (o `/ricevute`); il
    dettaglio aggiunge in fondo l'identificativo interno della fattura. Quella
    differenza è netta e l'abbiamo vista sul portale vero.
    """
    return on_list_url(page.url)


def on_list_url(url: str) -> bool:
    """Come `on_list`, ma su un indirizzo già in mano.

    Serve separata perché `wait_for_url` passa l'indirizzo come stringa, non
    la pagina: senza questa, la stessa regola finirebbe scritta due volte.
    """
    path = urlsplit(url).path.rstrip("/")
    return path.endswith(("/fatture/emesse", "/fatture/ricevute"))


def back_to_list(page: Page) -> None:
    """Riporta il browser sull'elenco, se siamo rimasti su un dettaglio.

    Dopo aver scaricato un file restiamo fermi sulla pagina di dettaglio
    (l'indirizzo diventa `/cons/cons-web/fatture/emesse/<id-interno>`). Lì non
    esiste nessuna tabella, quindi la fattura successiva non ha nessun bottone
    da cliccare: è il motivo per cui un giro da tre fatture ne scaricava una
    sola e dava le altre due per "non più nell'elenco".

    Proviamo prima il tasto indietro del browser, e solo se non basta
    rinavighiamo. Non è pignoleria: rinavigare ricarica l'applicazione e
    riporta la tabella alla prima pagina, mentre il tasto indietro ha almeno
    una possibilità di riconsegnarci la pagina dove eravamo. Con le fatture
    sparse su cinque pagine, la differenza è fra sfogliare una volta sola o
    sfogliare da capo per ogni fattura.

    Se siamo già sull'elenco non facciamo niente: la funzione si può chiamare
    sempre, anche sulla prima fattura del giro.
    """
    if on_list(page):
        return

    page.go_back()
    page.wait_for_load_state("networkidle")
    if on_list(page):
        return

    # Il tasto indietro non ci ha riportati sull'elenco: ci andiamo per
    # indirizzo. Costa una ripartenza dell'applicazione, ma è l'unica strada
    # che sappiamo funzionare, ed è meglio di un giro che fallisce.
    go_to_route(page, ISSUED_ROUTE if "/ricevute" not in page.url else RECEIVED_ROUTE)
    wait_for_table(page)


def open_detail(page: Page, invoice: Invoice) -> None:
    """Apre la pagina di dettaglio di una fattura.

    Si apre CLICCANDO il bottone della riga. Dal 2026-09-22 non c'è
    alternativa: andando all'indirizzo `/cons/cons-web/fatture/dettaglio/<id>`
    la SPA rimbalza sull'elenco, come si vede dall'URL stampato quando fallisce.

    Cerchiamo il bottone per nome completo ("Dettaglio Fattura <id>") e non per
    posizione. La posizione sarebbe sbagliata: dopo aver sfogliato la tabella,
    "il terzo bottone" non è la terza fattura dell'elenco che hai davanti.

    Il bottone giusto lo trova `detail_button`, che sfoglia la tabella se la
    fattura non è nella pagina visualizzata: il click arriva solo alle righe
    presenti in quel momento nel DOM, e dopo la lettura la tabella torna alla
    prima pagina. `DetailNotOnPageError` resta, ma ora significa che la
    fattura non è in NESSUNA pagina, non che stavamo guardando quella
    sbagliata.

    Aspettiamo il bottone di download e non il caricamento generico: il
    contenuto del dettaglio arriva dopo, e senza questa attesa si finisce a
    guardare una pagina ancora vuota (già successo in diagnostica).

    Solleva `FilePendingError` quando il portale dice che l'XML non è ancora
    pronto: il dettaglio in quel caso si apre benissimo, è il file a non
    esistere ancora.

    Se non compare né il bottone né l'avviso solleva l'errore di Playwright,
    ma prima salva
    l'HTML della pagina in `data/`: chi chiama decide se fermarsi o passare
    alla fattura successiva, noi ci assicuriamo che resti una traccia.
    """
    if not invoice.detail_id:
        raise DetailNotOnPageError(
            f"{invoice.number}: nella riga non c'è il bottone del dettaglio, "
            f"quindi non so cosa cliccare."
        )

    back_to_list(page)

    button = detail_button(page, invoice.detail_id)
    if button is None:
        # Salviamo dove siamo finiti PRIMA di arrenderci.
        #
        # Qui "non trovato" ha almeno due significati molto diversi: la fattura
        # non è più nell'elenco, oppure non siamo affatto sull'elenco. Senza
        # l'HTML del momento i due casi si confondono, e in questo progetto è
        # già costato due diagnosi sbagliate. L'URL stampato basta a separarli.
        name = safe_filename(f"dettaglio_non_trovato_{invoice.detail_id}.html")
        describe_page(page, save_html_as=name)
        raise DetailNotOnPageError(
            f"{invoice.number}: bottone del dettaglio non trovato in nessuna "
            f"pagina della tabella. Guarda l'HTML appena salvato: dice se siamo "
            f"sull'elenco (allora la fattura non c'è più) o su un'altra pagina "
            f"(allora è il ritorno all'elenco che non funziona)."
        )

    open_by_click(page, button, invoice)

    # Aspettiamo il primo dei due esiti possibili, non solo quello buono: o il
    # bottone, o l'avviso che il file non è ancora pronto. Aspettare solo il
    # bottone costava un minuto di timeout per poi dire "non aperto", quando
    # la risposta era a schermo dal primo istante.
    button = page.locator(f"button:has-text('{DOWNLOAD_BUTTON_NAME}')")
    pending = page.locator(FILE_PENDING_SELECTOR)

    try:
        button.or_(pending).first.wait_for(state="visible")
    except PlaywrightTimeout:
        # Salviamo l'HTML del momento esatto e poi lasciamo passare l'errore:
        # `download_all` lo intercetta e prosegue con le altre fatture.
        #
        # Serve la prova, non l'ipotesi. Alla prima occorrenza restava solo un
        # timeout, che non dice se la pagina di dettaglio sia arrivata vuota,
        # disegnata senza il bottone, o con un errore del portale: tre cause
        # diverse che si riparano in tre modi diversi. Stessa scelta fatta in
        # `wait_for_table` per l'elenco, e lì è ciò che ha sbloccato la diagnosi.
        #
        # Il nome del file porta l'identificativo della fattura: in un giro da
        # dodici, due guasti non devono sovrascriversi a vicenda.
        name = safe_filename(f"dettaglio_senza_bottone_{invoice.detail_id}.html")
        describe_page(page, save_html_as=name)
        raise

    if pending.count():
        raise FilePendingError(invoice.number)


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
                        try:
                            search_by_date(
                                consultation, options.date_from, options.date_to
                            )
                        except DateOutOfRangeError as error:
                            # Non è un guasto, è una data che il sito non
                            # accetta: lo diciamo e restiamo sull'elenco di
                            # prima, invece di far cadere tutto il programma
                            # dopo che il login è già stato fatto.
                            print(f"  -> {error}")
                            continue
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
                        # Il riepilogo lo stampa `download_all`: distingue le
                        # già scaricate dalle nuove, cosa che un "N su M" qui
                        # non saprebbe fare.
                        download_all(consultation, chosen, kind.value, open_detail)

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
