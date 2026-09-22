"""Test della consultazione che non richiedono il portale.

Dati inventati: nessun identificativo qui dentro è reale.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from extr_ade.consultation import on_list, on_list_url

BASE = "https://ivaservizi.agenziaentrate.gov.it/cons/cons-web"


@dataclass
class FakePage:
    """Il minimo che serve a `on_list`: un indirizzo.

    `on_list` guarda solo `page.url`, quindi un finto con quel solo attributo
    basta a provarla per davvero. È l'unico pezzo del lavoro sul portale nuovo
    che si può verificare senza aprire un browser: vale la pena avercelo.
    """

    url: str


# --- siamo sull'elenco --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/fatture/emesse",
        f"{BASE}/fatture/emesse/",  # con la barra finale
        f"{BASE}/fatture/ricevute",
        f"{BASE}/fatture/emesse?x=1",  # eventuale coda nell'indirizzo
    ],
)
def test_on_list_riconosce_elenco(url: str) -> None:
    assert on_list(FakePage(url)) is True


# --- NON siamo sull'elenco ----------------------------------------------------


def test_on_list_riconosce_dettaglio() -> None:
    """Il caso che conta: il dettaglio aggiunge l'identificativo interno.

    È la distinzione su cui si regge `back_to_list`. Sbagliarla significa non
    tornare mai all'elenco e dare tutte le fatture dopo la prima per "non più
    presenti": è successo il 2026-09-22.
    """
    assert on_list(FakePage(f"{BASE}/fatture/emesse/0FPR00000000001")) is False


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/fatture/ricevute/0FPR00000000002",
        f"{BASE}/home",
        f"{BASE}/fatture/bollo",
        "https://portale.agenziaentrate.gov.it/PortaleWeb/home",
    ],
)
def test_on_list_riconosce_altre_pagine(url: str) -> None:
    assert on_list(FakePage(url)) is False


# --- la stessa regola, su un indirizzo gia' in mano ---------------------------


def test_on_list_url_concorda_con_on_list() -> None:
    """`wait_for_url` riceve una stringa, non la pagina: le due devono coincidere.

    Se divergessero, il programma potrebbe credere di essere sul dettaglio
    mentre `back_to_list` lo considera ancora sull'elenco, o viceversa.
    """
    indirizzi = [
        f"{BASE}/fatture/emesse",
        f"{BASE}/fatture/emesse/0FPR00000000001",
        f"{BASE}/fatture/ricevute",
        f"{BASE}/home",
    ]
    for url in indirizzi:
        assert on_list_url(url) is on_list(FakePage(url))
