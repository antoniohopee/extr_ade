"""Login headless al portale dell'Agenzia delle Entrate.

Headless vuol dire che il browser gira come processo invisibile: non si apre
nessuna finestra, tutto passa dal terminale. Serve comunque un browser vero
(e non un semplice client HTTP) perché le pagine del portale sono costruite
da JavaScript.

Questo modulo fa due cose:

- `perform_login()`: il login vero e proprio, che riuseremo nei passi
  successivi;
- `describe_page()`: un inventario di quello che c'è nella pagina in cui
  siamo atterrati. Serve a ricavare i selettori dei passaggi successivi
  (Me stesso / Incaricato, tendina del delegante) guardando la pagina reale
  invece di indovinarli.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from extr_ade.constants import (
    LOGIN_PANEL_SELECTOR,
    LOGIN_PATH,
    LOGIN_TAB_NAME,
    LOGIN_URL,
    PASSWORD_SELECTOR,
    PIN_SELECTOR,
    SUBMIT_BUTTON_NAME,
    USERNAME_SELECTOR,
)
from extr_ade.credentials import Credentials, get_credentials

DATA_DIR = Path("data")

# Il portale non è velocissimo e il login passa per più redirect: 60 secondi
# sono generosi di proposito. Meglio aspettare che fallire per fretta.
TIMEOUT_MS = 60_000


@contextmanager
def browser_page(*, headless: bool = True) -> Iterator[Page]:
    """Apre un browser e restituisce una pagina, chiudendo tutto alla fine.

    `headless` è un parametro e non una costante solo perché un domani, se un
    passaggio si rompe e non si capisce perché, poterlo rimettere a False per
    cinque minuti è il modo più veloce di capirci qualcosa. In uso normale
    resta True.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        # `accept_downloads` esplicito: senza, il browser scarterebbe i file
        # che il portale ci manda. È il valore predefinito di Playwright, ma
        # scriverlo evita che un domani un cambiamento silenzioso rompa il
        # download senza che si capisca perché.
        context = browser.new_context(accept_downloads=True)
        context.set_default_timeout(TIMEOUT_MS)
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def perform_login(page: Page, credentials: Credentials) -> None:
    """Compila il form di accesso e clicca "Accedi".

    Non conosciamo (e non ci serve conoscere) l'URL a cui il form fa POST:
    compiliamo i campi e clicchiamo, esattamente come farebbe una persona.
    Un URL in meno da indovinare è un URL in meno che si può rompere.
    """
    page.goto(LOGIN_URL, wait_until="networkidle")

    # Prima va aperta la scheda "Fisconline/Entratel": i campi esistono nel DOM
    # fin dall'inizio ma sono dentro un pannello con `display: none`, quindi
    # non compilabili. Senza questo click il passo successivo va in timeout.
    page.get_by_role("tab", name=LOGIN_TAB_NAME).click()
    page.wait_for_selector(USERNAME_SELECTOR, state="visible")

    page.fill(USERNAME_SELECTOR, credentials.username)
    page.fill(PASSWORD_SELECTOR, credentials.password)
    page.fill(PIN_SELECTOR, credentials.pin)

    # Cerchiamo il bottone dentro il pannello, non nella pagina intera: il
    # pannello Sister ha anche lui un "Accedi", solo che è nascosto.
    panel = page.locator(LOGIN_PANEL_SELECTOR)
    panel.get_by_role("button", name=SUBMIT_BUTTON_NAME).click()

    # Aspettiamo di aver LASCIATO la pagina di login, non "che la rete si
    # calmi": `networkidle` tornava mentre il bottone diceva ancora
    # "Caricamento in corso...", e ci ritrovavamo a fotografare una pagina a
    # metà. Se le credenziali sono sbagliate l'URL non cambia mai e questa
    # attesa va in timeout: è il modo in cui ce ne accorgiamo.
    page.wait_for_url(lambda url: LOGIN_PATH not in url, timeout=TIMEOUT_MS)
    page.wait_for_load_state("networkidle")


def login_failed(page: Page) -> bool:
    """Vero se siamo ancora sulla pagina di login, cioè non siamo entrati.

    Controlliamo il percorso e non l'host: dopo il login si resta sullo stesso
    dominio, quindi guardare l'host darebbe un falso fallimento su un login
    perfettamente riuscito (errore preso durante la prima prova reale).
    """
    return LOGIN_PATH in page.url


def describe_page(page: Page, *, save_html_as: str | None = None) -> None:
    """Stampa un inventario degli elementi interattivi visibili nella pagina.

    Serve a capire com'è fatta una schermata che non conosciamo ancora, per
    ricavarne i selettori. Stampiamo solo gli elementi VISIBILI perché queste
    pagine contengono molti elementi nascosti che non useremo mai.
    """
    # L'HTML si salva alla FINE, dentro un `finally`.
    #
    # Salvarlo all'inizio sembrava più prudente (resiste al Ctrl+C) ma dava un
    # file stantio: queste pagine si costruiscono da sole un pezzo alla volta,
    # e lo scatto iniziale conteneva solo l'intestazione mentre a schermo si
    # vedeva la pagina intera. Il `finally` ottiene tutte e due le cose: il
    # contenuto più fresco possibile, e il file scritto comunque anche se
    # interrompi a metà.
    try:
        print("\n" + "=" * 70)
        print("URL    :", page.url)
        print("Titolo :", page.title())
        print("=" * 70)

        _dump_headings(page)
        _dump_fields(page)
        _dump(page, "BOTTONI", "button, input[type=submit], input[type=button], a[role=button]")
        _dump_choices(page)
        _dump_selects(page)
        _dump(page, "LINK", "a[href]", limit=40)

        frames = [f for f in page.frames if f != page.main_frame]
        if frames:
            print(f"\n--- IFRAME ({len(frames)}) ---")
            for frame in frames:
                print(f"  name={frame.name!r} url={frame.url}")
            print("  ATTENZIONE: se il contenuto è dentro un iframe, i selettori")
            print("  vanno cercati nel frame, non nella pagina principale.")
    finally:
        if save_html_as:
            DATA_DIR.mkdir(exist_ok=True)
            destination = DATA_DIR / save_html_as
            destination.write_text(page.content(), encoding="utf-8")
            print(f"\nHTML completo salvato in {destination}")
            print("(può contenere dati personali: è locale, non va condiviso)")


def _dump_fields(page: Page, *, limit: int = 25) -> None:
    """Elenca i campi da compilare: testo, date, numeri, ricerca.

    Mancavano del tutto: l'inventario mostrava bottoni e tendine ma non i
    campi, quindi su un form di ricerca non si vedeva quasi niente.

    Stampiamo anche il testo vicino al campo, perché su queste pagine
    l'etichetta ("Data fattura", "Numero") sta in un elemento a fianco e non
    è collegata al campo da alcun attributo.
    """
    selector = (
        "input[type=text], input[type=date], input[type=number], "
        "input[type=search], input[type=tel], input[type=email], "
        "input:not([type]), textarea"
    )
    elements = [e for e in page.locator(selector).all() if e.is_visible()]

    print(f"\n--- CAMPI DA COMPILARE ({len(elements)}) ---")
    if not elements:
        print("  nessuno")
        return

    for element in elements[:limit]:
        nearby = element.evaluate(
            "e => (e.closest('label') || e.parentElement)?.innerText || ''"
        )
        print(
            f"  id={element.get_attribute('id')!r}"
            f"  name={element.get_attribute('name')!r}"
            f"  type={element.get_attribute('type')!r}"
            f"  placeholder={element.get_attribute('placeholder')!r}"
            f"  etichetta={' '.join(nearby.split())[:50]!r}"
        )

    if len(elements) > limit:
        print(f"  ... e altri {len(elements) - limit} (troncato)")


def _dump_headings(page: Page, *, limit: int = 20) -> None:
    """Elenca titoli ed etichette visibili, cioè "a che punto siamo".

    Questo wizard cambia contenuto senza cambiare URL: l'indirizzo resta lo
    stesso dal passo 1 all'ultimo. Il testo in pagina ("Scegli utenza di
    lavoro", "Scegli per chi operare", "Riepilogo e conferma") è quindi
    l'unico modo affidabile di sapere dove siamo finiti.
    """
    elements = [
        e for e in page.locator("h1, h2, h3, h4, legend, label").all() if e.is_visible()
    ]

    print(f"\n--- TITOLI ED ETICHETTE ({len(elements)}) ---")
    if not elements:
        print("  nessuno")
        return

    seen: set[str] = set()
    for element in elements[:limit]:
        text = " ".join((element.inner_text() or "").split())[:80]
        if text and text not in seen:
            seen.add(text)
            print(f"  {text!r}")


def _dump_selects(page: Page, *, limit: int = 15) -> None:
    """Elenca le tendine e, soprattutto, le loro opzioni.

    Elencare la tendina senza il suo contenuto non serve a niente: il dato che
    ci interessa sono i `value` delle opzioni, perché è quello che poi useremo
    per selezionare il soggetto giusto.
    """
    selects = page.locator("select").all()

    print(f"\n--- SELECT / TENDINE ({len(selects)}) ---")
    if not selects:
        print("  nessuna")
        return

    for element in selects:
        options = element.locator("option").all()
        print(
            f"  <select id={element.get_attribute('id')!r}"
            f" name={element.get_attribute('name')!r}>"
            f"  opzioni={len(options)}"
        )
        for option in options[:limit]:
            text = " ".join((option.inner_text() or "").split())[:60]
            print(f"      value={option.get_attribute('value')!r}  testo={text!r}")
        if len(options) > limit:
            print(f"      ... e altre {len(options) - limit} (troncato)")


def _dump_choices(page: Page) -> None:
    """Elenca radio e checkbox, con il testo che li accompagna.

    Due differenze rispetto a `_dump`, imparate sbagliando:

    - qui stampiamo anche gli elementi NON visibili. I radio dei form moderni
      sono quasi sempre nascosti dietro un'etichetta grafica: filtrarli via
      significa non vederli mai;
    - stampiamo il testo del contenitore, perché in queste pagine le `<label>`
      non hanno l'attributo `for` e quindi non si possono associare al campo
      per via diretta.
    """
    elements = page.locator("input[type=radio], input[type=checkbox]").all()

    print(f"\n--- RADIO / CHECKBOX ({len(elements)}) ---")
    if not elements:
        print("  nessuno")
        return

    for element in elements:
        nearby = element.evaluate(
            "e => (e.closest('label') || e.parentElement)?.innerText || ''"
        )
        print(
            f"  id={element.get_attribute('id')!r}"
            f"  name={element.get_attribute('name')!r}"
            f"  value={element.get_attribute('value')!r}"
            f"  visibile={element.is_visible()}"
            f"  selezionato={element.is_checked()}"
            f"  testo={' '.join(nearby.split())[:60]!r}"
        )


def _dump(page: Page, title: str, selector: str, *, limit: int = 25) -> None:
    """Elenca gli elementi visibili che corrispondono a un selettore."""
    elements = [e for e in page.locator(selector).all() if e.is_visible()]

    print(f"\n--- {title} ({len(elements)}) ---")
    if not elements:
        print("  nessuno")
        return

    for element in elements[:limit]:
        text = " ".join((element.inner_text() or "").split())[:60]
        identifier = element.get_attribute("id")
        name = element.get_attribute("name")
        value = element.get_attribute("value")
        href = element.get_attribute("href")

        details = [f"testo={text!r}"]
        if identifier:
            details.append(f"id={identifier!r}")
        if name:
            details.append(f"name={name!r}")
        if value:
            details.append(f"value={value!r}")
        if href:
            details.append(f"href={href[:130]!r}")
        print("  " + "  ".join(details))

    if len(elements) > limit:
        print(f"  ... e altri {len(elements) - limit} (troncato)")


def main() -> None:
    credentials = get_credentials()

    with browser_page() as page:
        print("\nLogin in corso (headless, nessuna finestra si aprirà)...")
        try:
            perform_login(page, credentials)
        except PlaywrightTimeout:
            # Non decidiamo niente qui: il timeout dice solo "non è successo
            # quello che aspettavo". Il perché lo stabiliscono i controlli
            # sotto, guardando dove siamo finiti davvero.
            print("\n(attesa scaduta, controllo dove siamo finiti)")

        if login_failed(page):
            print("\nLOGIN NON RIUSCITO: siamo ancora sulla pagina di accesso.")
            print("Testo visibile della pagina (potrebbe spiegare il motivo):")
            print(" ", " ".join(page.locator("body").inner_text().split())[:500])
            describe_page(page, save_html_as="login_fallito.html")
            raise SystemExit(1)

        print("\nLOGIN RIUSCITO.")
        describe_page(page, save_html_as="dopo_login.html")

        print("\nDa guardare in questo inventario:")
        print("  - c'è un campo o una richiesta di OTP?")
        print("  - c'è la scelta 'Me stesso / Incaricato'? come si chiamano i bottoni?")
        print("  - c'è una tendina (select) con i codici fiscali deleganti?")


if __name__ == "__main__":
    main()
