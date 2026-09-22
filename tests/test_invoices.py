"""Test della logica di lettura delle fatture.

Tutti i dati qui dentro sono INVENTATI: partite IVA, nomi e importi non
corrispondono a nessun soggetto reale. Nessun test si collega al portale.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from extr_ade.invoices import (
    Invoice,
    filter_invoices,
    format_invoices,
    parse_amount,
    parse_date,
    parse_selection,
    split_client,
)


# --- importi ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("testo", "atteso"),
    [
        ("21,34", Decimal("21.34")),
        ("1.234,56", Decimal("1234.56")),
        ("1.234.567,89", Decimal("1234567.89")),
        ("0,00", Decimal("0")),
        ("", Decimal("0")),  # cella vuota: succede, non è un errore
        ("  18,42  ", Decimal("18.42")),
        ("-5,00", Decimal("-5.00")),  # note di credito
        # Dal 2026-09-22 la tabella scrive anche la valuta. Lo spazio prima
        # dell'euro nel primo caso è quello unificatore (`\xa0`), che è
        # proprio quello usato dal portale e a occhio non si distingue.
        ("20,90\xa0€", Decimal("20.90")),
        ("20,90 €", Decimal("20.90")),
        ("1.234,56 €", Decimal("1234.56")),
        ("-5,00 €", Decimal("-5.00")),
    ],
)
def test_parse_amount(testo: str, atteso: Decimal) -> None:
    assert parse_amount(testo) == atteso


def test_parse_amount_rifiuta_testo_non_numerico() -> None:
    with pytest.raises(ValueError, match="Importo non riconosciuto"):
        parse_amount("ventuno virgola trentaquattro")


def test_somma_senza_errori_di_arrotondamento() -> None:
    """Il motivo per cui usiamo Decimal invece di float.

    Con i float questa somma darebbe 0.30000000000000004 e i totali non
    quadrerebbero.
    """
    totale = parse_amount("0,10") + parse_amount("0,20")
    assert totale == Decimal("0.30")


# --- date ---------------------------------------------------------------------


def test_parse_date() -> None:
    assert parse_date("28/07/2026") == date(2026, 7, 28)


def test_parse_date_accetta_spazi() -> None:
    assert parse_date("  01/01/2025 ") == date(2025, 1, 1)


@pytest.mark.parametrize("testo", ["2026-07-28", "28-07-2026", "", "32/01/2026"])
def test_parse_date_rifiuta_formati_sbagliati(testo: str) -> None:
    with pytest.raises(ValueError, match="Data non riconosciuta"):
        parse_date(testo)


# --- cliente ------------------------------------------------------------------


def test_split_client() -> None:
    assert split_client("01234567890 - Rossi srl") == ("01234567890", "Rossi srl")


def test_split_client_con_trattino_nel_nome() -> None:
    """Il nome può contenere trattini: si divide solo alla prima occorrenza."""
    identificativo, nome = split_client("01234567890 - Rossi - Bianchi snc")
    assert identificativo == "01234567890"
    assert nome == "Rossi - Bianchi snc"


def test_split_client_a_capo() -> None:
    """Forma della tabella nuova (2026-09-22): identificativo e nome su due righe."""
    assert split_client("01234567890\nRossi srl") == ("01234567890", "Rossi srl")


def test_split_client_a_capo_con_trattino_nel_nome() -> None:
    """Col separatore nuovo, un trattino nel nome non deve tagliare niente."""
    identificativo, nome = split_client("01234567890\nRossi - Bianchi snc")
    assert identificativo == "01234567890"
    assert nome == "Rossi - Bianchi snc"


def test_split_client_senza_denominazione() -> None:
    assert split_client("01234567890") == ("01234567890", "")


# --- filtri -------------------------------------------------------------------


def _fattura(
    numero: str = "IT001",
    giorno: date = date(2026, 1, 15),
    identificativo: str = "01234567890",
    nome: str = "Rossi srl",
) -> Invoice:
    """Costruisce una fattura finta per i test."""
    return Invoice(
        number=numero,
        issue_date=giorno,
        client_id=identificativo,
        client_name=nome,
        taxable=Decimal("100.00"),
        tax=Decimal("22.00"),
        sdi_id="12345678901",
        detail_id="0FPR00000000001",
    )


@pytest.fixture
def fatture() -> list[Invoice]:
    return [
        _fattura(numero="IT001", nome="Rossi srl", giorno=date(2026, 1, 15)),
        _fattura(numero="IT002", nome="Bianchi spa", giorno=date(2026, 2, 20)),
        _fattura(numero="IT003", nome="Lacona gestioni srls", giorno=date(2026, 2, 20)),
    ]


def test_filtro_per_nome_cliente_ignora_maiuscole(fatture: list[Invoice]) -> None:
    trovate = filter_invoices(fatture, client="lacona")
    assert [f.number for f in trovate] == ["IT003"]


def test_filtro_per_cliente_cerca_anche_nella_partita_iva(fatture: list[Invoice]) -> None:
    assert len(filter_invoices(fatture, client="01234567890")) == 3


def test_filtro_per_numero(fatture: list[Invoice]) -> None:
    assert [f.number for f in filter_invoices(fatture, number="IT002")] == ["IT002"]


def test_filtro_per_data(fatture: list[Invoice]) -> None:
    trovate = filter_invoices(fatture, issue_date=date(2026, 2, 20))
    assert [f.number for f in trovate] == ["IT002", "IT003"]


def test_filtri_combinati(fatture: list[Invoice]) -> None:
    trovate = filter_invoices(fatture, client="bianchi", issue_date=date(2026, 2, 20))
    assert [f.number for f in trovate] == ["IT002"]


def test_nessun_filtro_restituisce_tutto(fatture: list[Invoice]) -> None:
    assert filter_invoices(fatture) == fatture


def test_filtro_senza_risultati(fatture: list[Invoice]) -> None:
    assert filter_invoices(fatture, client="inesistente") == []


# --- totale e stampa ----------------------------------------------------------


def test_totale_fattura() -> None:
    assert _fattura().total == Decimal("122.00")


def test_format_invoices_numera_da_zero(fatture: list[Invoice]) -> None:
    righe = format_invoices(fatture).splitlines()
    assert righe[2].startswith("   0")
    assert righe[4].startswith("   2")


def test_format_invoices_elenco_vuoto() -> None:
    assert format_invoices([]) == "Nessuna fattura trovata."


# --- selezione ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("testo", "atteso"),
    [
        ("3", [3]),
        ("0-4", [0, 1, 2, 3, 4]),
        ("0,4,7", [0, 4, 7]),
        ("0-2,8", [0, 1, 2, 8]),
        ("5-5", [5]),
        ("  2 , 4  ", [2, 4]),  # spazi sparsi
        ("", []),  # Invio a vuoto: nessuna selezione
        ("0-2,1", [0, 1, 2]),  # sovrapposizioni: niente doppioni
        ("4,1", [1, 4]),  # ordinati
    ],
)
def test_parse_selection(testo: str, atteso: list[int]) -> None:
    assert parse_selection(testo, count=10) == atteso


def test_parse_selection_rifiuta_indice_troppo_grande() -> None:
    """Chiedere "0-100" avendo 10 fatture è un errore, non una richiesta da
    accontentare in parte: chi lo scrive si aspetta 101 file."""
    with pytest.raises(ValueError, match="fuori intervallo"):
        parse_selection("0-100", count=10)


def test_parse_selection_rifiuta_intervallo_al_contrario() -> None:
    with pytest.raises(ValueError, match="al contrario"):
        parse_selection("7-2", count=10)


def test_parse_selection_rifiuta_testo() -> None:
    with pytest.raises(ValueError, match="Non è un numero"):
        parse_selection("prime venti", count=10)
