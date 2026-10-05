"""Test del riconoscimento delle pagine con cui il portale dice di no.

Dati inventati: nessun identificativo qui dentro è reale.

Questi test girano su HTML scritto a mano, non sul portale. La verifica sugli
HTML veri salvati in `data/` è stata fatta a parte (e andava fatta: quei file
contengono fatture reali, quindi non possono diventare fixture).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from extr_ade.portal import PortalDownError, portal_message, raise_if_portal_down


@dataclass
class FakeLocator:
    """Un elemento che c'è o non c'è, con il suo testo e la sua visibilità."""

    text: str | None = None
    visible: bool = True

    @property
    def first(self) -> FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self.text is not None and self.visible

    def inner_text(self) -> str:
        assert self.text is not None
        return self.text


@dataclass
class FakePage:
    """Il minimo che serve a `portal_message`: rispondere a `locator`.

    Le chiavi sono i selettori; quelli assenti restituiscono un elemento che
    non c'è, che è esattamente come si comporta Playwright.
    """

    elements: dict[str, str] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(
            self.elements.get(selector), visible=selector not in self.hidden
        )


DOWN = "h1:has-text('non è al momento disponibile')"
ERROR = "#error-msg .alert-danger p"


# --- il portale dice di no ----------------------------------------------------


def test_riconosce_la_pagina_di_fuori_servizio() -> None:
    page = FakePage({DOWN: "Il sistema fatture e corrispettivi non è al momento disponibile"})
    assert portal_message(page) == (
        "Il sistema fatture e corrispettivi non è al momento disponibile"
    )


def test_riconosce_l_errore_dell_applicazione() -> None:
    page = FakePage({ERROR: "Si è verificato un errore sconosciuto"})
    assert portal_message(page) == "Si è verificato un errore sconosciuto"


def test_il_fuori_servizio_ha_la_precedenza() -> None:
    """Se ci fossero entrambi, vince quello che descrive meglio la situazione."""
    page = FakePage({DOWN: "Non è al momento disponibile", ERROR: "Errore generico"})
    assert portal_message(page) == "Non è al momento disponibile"


def test_il_testo_viene_ridotto_a_una_riga() -> None:
    page = FakePage({DOWN: "  Non è al\n\n  momento   disponibile  "})
    assert portal_message(page) == "Non è al momento disponibile"


# --- il portale sta bene ------------------------------------------------------


def test_una_pagina_normale_non_e_un_rifiuto() -> None:
    """Il caso che conta di più: non deve gridare al lupo.

    Se questo test si rompe, ogni giro si ferma credendo il portale fuori
    servizio mentre funziona: molto peggio del problema che stiamo curando.
    """
    assert portal_message(FakePage()) is None
    assert raise_if_portal_down(FakePage()) is None


# --- l'eccezione porta con sé le parole del portale ---------------------------


def test_l_eccezione_riporta_il_messaggio_del_portale() -> None:
    """Il testo è la differenza fra "guarda il codice" e "guarda l'orologio"."""
    page = FakePage({DOWN: "Il sistema non è al momento disponibile"})
    with pytest.raises(PortalDownError, match="non è al momento disponibile"):
        raise_if_portal_down(page)


# --- le due cautele che impediscono il falso allarme --------------------------
#
# Sono la parte più importante del modulo. Se una di queste cede, ogni
# fallimento qualunque diventa "il portale è giù": l'elenco smette di
# ricaricare, ogni fattura brucia tre tentativi, e a schermo compare «».


def test_un_elemento_nascosto_non_e_un_rifiuto() -> None:
    """Una SPA si porta dietro elementi nascosti che non vogliono dire niente."""
    page = FakePage({DOWN: "Non è al momento disponibile"}, hidden={DOWN})
    assert portal_message(page) is None


def test_un_riquadro_ancora_vuoto_non_e_un_rifiuto() -> None:
    """Il contenitore può essere disegnato un istante prima del suo testo."""
    page = FakePage({ERROR: "   \n  "})
    assert portal_message(page) is None
    assert raise_if_portal_down(page) is None


def test_un_riquadro_vuoto_non_diventa_un_eccezione_vuota() -> None:
    """Prima di questo controllo usciva `PortalDownError('')`, cioè «» a schermo."""
    page = FakePage({ERROR: ""})
    raise_if_portal_down(page)  # non deve sollevare niente
