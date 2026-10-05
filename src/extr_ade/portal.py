"""Riconoscere quando è il portale a dire di no, e non il programma a sbagliare.

Il portale ha due modi di rifiutare una richiesta, e nessuno dei due è un
guasto nostro:

1. la **pagina di fuori servizio** del sistema Fatture e Corrispettivi, che
   sostituisce l'intera applicazione ("Il sistema fatture e corrispettivi non
   è al momento disponibile");
2. l'**avviso di errore** dell'applicazione, che resta in piedi e scrive un
   riquadro rosso al posto del contenuto ("Si è verificato un errore
   sconosciuto").

Perché questo modulo esiste. Senza, quelle due pagine arrivavano a chi legge
sotto mentite spoglie: l'elenco diceva "tabella non comparsa" e il dettaglio
diceva "bottone del dettaglio non trovato in nessuna pagina", cioè due accuse
al nostro codice per una porta chiusa dall'altra parte. Il 2026-09-28 e il
2026-10-05 sono costati due diagnosi a vuoto esattamente per questo: in
`data/` restavano 24 e 31 file identici, tutti con quella pagina dentro.

Perché un modulo a sé e non una funzione dentro `invoices` o `downloader`:
serve a tutti e due, e `downloader` importa `invoices`. Mettendola in uno dei
due avremmo avuto un import circolare o una funzione nel posto sbagliato.
Qui dipende solo da `constants`, quindi la può chiamare chiunque.
"""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError, Page

from extr_ade.constants import PORTAL_DOWN_SELECTOR, PORTAL_ERROR_SELECTOR

# Quanto del messaggio del portale riportiamo. Serve solo a non stampare mezza
# pagina se un domani il selettore agganciasse un contenitore più grande.
MESSAGE_LIMIT = 200


class PortalDownError(Exception):
    """Il portale ha dichiarato di non poter rispondere.

    Tenuta distinta da qualunque altro errore perché richiede una reazione
    diversa: non c'è niente da riparare e non serve cambiare selettori, serve
    rallentare e riprovare. Chi la cattura sa che il programma stava
    funzionando.

    Il testo dell'eccezione è il messaggio del portale, parola per parola: è
    la differenza fra "guarda il codice" e "guarda l'orologio".
    """


def _clean(text: str) -> str:
    """Riduce il testo di un elemento a una riga stampabile."""
    return " ".join(text.split())[:MESSAGE_LIMIT]


def portal_message(page: Page) -> str | None:
    """Il messaggio con cui il portale dichiara di non essere disponibile.

    Restituisce `None` quando il portale non dice niente del genere, che è il
    caso normale: in quel caso chi chiama prosegue come ha sempre fatto.

    Leggere la pagina può a sua volta fallire (navigazione in corso, scheda
    chiusa). Qui non è il momento di aggiungere un guasto a un guasto: se non
    riusciamo a guardare, rispondiamo "non so" e lasciamo che sia il controllo
    di prima a decidere.

    Due cautele, e sono LA cosa da non sbagliare in questo modulo. Guardiamo
    se l'elemento è VISIBILE e non se esiste nel documento, perché una SPA si
    porta dietro elementi nascosti che non rappresentano niente; e un
    messaggio vuoto non vale, perché il riquadro può essere disegnato un
    istante prima del testo che lo riempie.

    Se una di queste due sbagliasse, ogni fallimento qualunque diventerebbe
    "il portale è giù": l'elenco smetterebbe di ricaricare, ogni fattura
    brucerebbe tre tentativi, e a schermo comparirebbe «» al posto del
    messaggio. Cioè il danno esatto che questo modulo esiste per evitare,
    fatto al contrario.
    """
    for selector in (PORTAL_DOWN_SELECTOR, PORTAL_ERROR_SELECTOR):
        try:
            element = page.locator(selector).first
            if element.is_visible():
                message = _clean(element.inner_text())
                if message:
                    return message
        except PlaywrightError:
            return None
    return None


def raise_if_portal_down(page: Page) -> None:
    """Solleva `PortalDownError` se la pagina è una delle due di rifiuto.

    Va chiamata nei punti in cui stiamo già per dichiarare un fallimento: a
    quel punto il controllo è vero per costruzione, perché se il messaggio è
    lì ed è lì che ci siamo fermati, il messaggio è il motivo. Chiamarla
    prima, mentre la pagina si sta ancora costruendo, rischierebbe invece di
    leggere uno stato intermedio.
    """
    message = portal_message(page)
    if message:
        raise PortalDownError(message)
