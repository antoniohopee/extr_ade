"""Scelta dell'utenza di lavoro: "Me stesso" oppure "Incaricato".

È il wizard che l'Agenzia delle Entrate mostra subito dopo il login, prima di
farti entrare in Fatture e Corrispettivi.

Il modulo si chiama `identity` (identificatori in inglese, per convenzione di
progetto) ma nel portale la stessa cosa si chiama "utenza di lavoro": se cerchi
questa schermata nel sito, cerca quel nome.
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum

from playwright.sync_api import Page

from extr_ade.constants import (
    CONFIRM_BUTTON_NAME,
    DELEGATE_RADIO_SELECTOR,
    DELEGATOR_SELECT_SELECTOR,
    ENV_FISCAL_CODE,
    PROCEED_BUTTON_NAME,
    SELF_RADIO_SELECTOR,
)
from extr_ade.credentials import get_credentials
from extr_ade.login import browser_page, describe_page, login_failed, perform_login


class UserType(StrEnum):
    """Le due modalità di lavoro previste dal portale."""

    SELF = "meStesso"
    DELEGATE = "incaricato"


# Testo da mostrare a te nel terminale, e selettore da cliccare nella pagina.
CHOICES: dict[UserType, tuple[str, str]] = {
    UserType.SELF: ("Me stesso (operi per conto tuo)", SELF_RADIO_SELECTOR),
    UserType.DELEGATE: ("Incaricato (operi per un soggetto che ti ha delegato)", DELEGATE_RADIO_SELECTOR),
}


def ask_user_type() -> UserType:
    """Chiede da terminale come si vuole entrare."""
    options = list(CHOICES.items())

    print("\nCome vuoi accedere a Fatture e Corrispettivi?")
    for number, (_, (label, _selector)) in enumerate(options):
        print(f"  {number}  {label}")

    last = len(options) - 1
    while True:
        answer = input(f"Scegli [0-{last}]: ").strip()
        if answer.isdigit() and 0 <= int(answer) <= last:
            return options[int(answer)][0]
        print(f"  -> rispondi con un numero fra 0 e {last}.")


def select_user_type(page: Page, user_type: UserType) -> None:
    """Seleziona l'opzione richiesta e clicca "Procedi".

    I radio di questa pagina potrebbero essere nascosti dietro un'etichetta
    grafica: in quel caso `check()` di Playwright si rifiuta di agire, perché
    per progetto interagisce solo con ciò che un utente potrebbe cliccare
    davvero. Se succede ripieghiamo su un click via JavaScript, e in ogni caso
    verifichiamo dopo che il radio sia davvero selezionato: se non lo fosse,
    proseguiremmo con l'utenza sbagliata senza accorgercene, che è molto
    peggio di fermarsi.
    """
    _, selector = CHOICES[user_type]
    radio = page.locator(selector)

    if radio.is_visible():
        radio.check()
    else:
        radio.evaluate("e => e.click()")

    if not radio.is_checked():
        raise RuntimeError(
            f"Non sono riuscito a selezionare l'opzione {user_type.value!r} "
            f"({selector}). La pagina potrebbe essere cambiata: ricontrolla i "
            f"selettori in constants.py."
        )

    page.get_by_role("button", name=PROCEED_BUTTON_NAME).click()

    if user_type is UserType.DELEGATE:
        # Il wizard cambia contenuto senza cambiare URL: aspettare il
        # caricamento della pagina non serve, perché la pagina non ricarica
        # mai. Aspettiamo che compaia l'elemento che caratterizza il passo 2.
        page.wait_for_selector(DELEGATOR_SELECT_SELECTOR, state="visible")
    else:
        # TODO: non sappiamo ancora cosa compare scegliendo "Me stesso" —
        # probabilmente si va dritti al riepilogo. Da verificare provando quel
        # ramo, poi mettere qui l'attesa giusta al posto di questa generica.
        page.wait_for_load_state("networkidle")


def list_delegators(page: Page) -> list[str]:
    """Restituisce i codici fiscali dei soggetti che ti hanno delegato.

    Leggiamo il TESTO delle opzioni, non il loro `value`: il value è un blob
    JSON che descrive il rapporto (`{"incaricante":{"cf":...,"sede":...}}`) e
    non è un dato che possiamo comporre noi. Lo lasciamo dov'è e selezioniamo
    per etichetta, che oltretutto è l'unica cosa leggibile da una persona.

    La prima opzione, quella vuota, è il segnaposto "scegli..." e va scartata.
    """
    options = page.locator(f"{DELEGATOR_SELECT_SELECTOR} option").all()
    return [
        text
        for option in options
        if (text := " ".join((option.inner_text() or "").split()))
        and option.get_attribute("value")
    ]


def choose_delegator(page: Page) -> str:
    """Sceglie il delegante e manda avanti il wizard. Restituisce il CF scelto.

    L'ordine di preferenza è: quello indicato nel .env (per il cron), poi
    l'unico disponibile se ce n'è uno solo, poi la domanda a te.
    """
    delegators = list_delegators(page)

    if not delegators:
        raise RuntimeError(
            "Nessun soggetto delegante nella tendina. Se invece dovresti "
            "averne, il selettore in constants.py potrebbe non essere più "
            "valido: ricontrolla la pagina."
        )

    chosen = _pick_delegator(delegators)

    page.select_option(DELEGATOR_SELECT_SELECTOR, label=chosen)
    page.get_by_role("button", name=PROCEED_BUTTON_NAME).click()

    # Non sappiamo ancora com'è fatto il riepilogo, quindi non possiamo
    # aspettare un suo elemento. Aspettiamo invece che SPARISCA la tendina:
    # è un segnale altrettanto affidabile di "abbiamo lasciato il passo 2", e
    # non richiede di indovinare niente sul passo 3.
    page.wait_for_selector(DELEGATOR_SELECT_SELECTOR, state="hidden")
    return chosen


def show_delegators(delegators: list[str]) -> None:
    """Stampa l'elenco numerato dei soggetti deleganti.

    Lo stampiamo SEMPRE, anche quando il soggetto è uno solo e non c'è niente
    da scegliere: devi poter vedere per conto di chi il programma sta per
    operare. Una scelta fatta in silenzio è una scelta che non puoi
    controllare.

    Numerazione da 0 perché questi sono indici di una lista, come lo saranno
    gli intervalli con cui selezionerai le fatture ("da 0 a 20").
    """
    print("\nSoggetti deleganti:")
    for index, delegator in enumerate(delegators):
        print(f"  {index}  {delegator}")


def _pick_delegator(delegators: list[str]) -> str:
    """Decide quale delegante usare, chiedendo solo se necessario."""
    show_delegators(delegators)

    wanted = os.getenv(ENV_FISCAL_CODE, "").strip().upper()
    if wanted:
        for delegator in delegators:
            if delegator.upper() == wanted:
                print(f"\nDelegante da {ENV_FISCAL_CODE}: {delegator}")
                return delegator
        print(
            f"\nAttenzione: {ENV_FISCAL_CODE}={wanted} non corrisponde a nessun "
            f"soggetto fra quelli disponibili."
        )

    if not sys.stdin.isatty():
        raise SystemExit(
            f"Terminale non interattivo: valorizza {ENV_FISCAL_CODE} nel .env "
            f"con uno di questi soggetti: {', '.join(delegators)}"
        )

    if len(delegators) == 1:
        print("Uno solo disponibile: lo uso senza chiedere.")
        return delegators[0]

    last = len(delegators) - 1
    while True:
        answer = input(f"Per quale soggetto vuoi operare? [0-{last}]: ").strip()
        if answer.isdigit() and 0 <= int(answer) <= last:
            return delegators[int(answer)]
        print(f"  -> rispondi con un numero fra 0 e {last}.")


def confirm_identity(page: Page) -> None:
    """Conferma il riepilogo, chiudendo il wizard di scelta utenza.

    Dopo questo click siamo dentro Fatture e Corrispettivi con l'utenza scelta.
    """
    confirm = page.get_by_role("button", name=CONFIRM_BUTTON_NAME)
    confirm.click()

    # Stesso ragionamento della tendina: aspettiamo che il bottone SPARISCA.
    # È un segnale di "il wizard è finito" che non richiede di sapere niente
    # sulla pagina in cui stiamo per atterrare — e finora, ogni volta che ho
    # dato per scontato com'era fatta la pagina dopo, mi sbagliavo.
    confirm.wait_for(state="hidden")
    page.wait_for_load_state("networkidle")


def main() -> None:
    credentials = get_credentials()
    user_type = ask_user_type()

    with browser_page() as page:
        print("\nLogin in corso (headless)...")
        perform_login(page, credentials)

        if login_failed(page):
            print("\nLOGIN NON RIUSCITO.")
            describe_page(page, save_html_as="login_fallito.html")
            raise SystemExit(1)

        print("Login riuscito. Seleziono l'utenza...")
        select_user_type(page, user_type)

        if user_type is UserType.DELEGATE:
            choose_delegator(page)

        print("Confermo il riepilogo...")
        confirm_identity(page)

        print("\nWizard completato: siamo dentro Fatture e Corrispettivi.")
        describe_page(page, save_html_as="home_fatture.html")

        print("\nDa guardare in questo inventario:")
        print("  - come si arriva a 'Consultazione fatture elettroniche e")
        print("    altri dati IVA'? è un link, una voce di menu, un bottone?")


if __name__ == "__main__":
    main()
