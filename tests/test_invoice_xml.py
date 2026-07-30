"""Test del parser degli XML delle fatture.

La fixture `fattura_esempio.xml` contiene dati inventati e due corpi fattura,
per verificare anche il caso della fatturazione a lotti.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from extr_ade.invoice_xml import (
    ParsedInvoice,
    parse_file,
    parse_folder,
    parse_xml_date,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fattura_esempio.xml"


@pytest.fixture
def fatture() -> list[ParsedInvoice]:
    return parse_file(FIXTURE)


# --- struttura ----------------------------------------------------------------


def test_legge_tutti_i_corpi_fattura(fatture: list[ParsedInvoice]) -> None:
    """Un file può contenere più fatture: fermarsi alla prima ne perderebbe."""
    assert [f.number for f in fatture] == ["FT001", "FT002"]


def test_dati_del_documento(fatture: list[ParsedInvoice]) -> None:
    prima = fatture[0]
    assert prima.document_type == "TD01"
    assert prima.issue_date == date(2026, 7, 28)
    assert prima.currency == "EUR"
    assert prima.source_file == FIXTURE


def test_header_condiviso_fra_i_corpi(fatture: list[ParsedInvoice]) -> None:
    """Cedente e cessionario stanno nell'header: valgono per tutti i corpi."""
    assert fatture[0].supplier.name == fatture[1].supplier.name == "Studio Esempio Srl"
    assert fatture[0].customer.name == fatture[1].customer.name == "Cliente Finto Snc"


def test_soggetti(fatture: list[ParsedInvoice]) -> None:
    prima = fatture[0]
    assert prima.supplier.vat_id == "00000000002"
    assert prima.supplier.vat_country == "IT"
    assert prima.customer.vat_id == "00000000003"
    assert prima.customer.identifier == "00000000003"


# --- righe e importi ----------------------------------------------------------


def test_righe_di_dettaglio(fatture: list[ParsedInvoice]) -> None:
    righe = fatture[0].lines
    assert len(righe) == 2
    assert righe[0].description == "Servizio di esempio"
    assert righe[0].quantity == Decimal("2.00")
    assert righe[0].unit_price == Decimal("40.00")
    assert righe[0].total_price == Decimal("80.00")
    assert righe[0].vat_rate == Decimal("22.00")


def test_quantita_assente_resta_none(fatture: list[ParsedInvoice]) -> None:
    """La quantità è facoltativa: assente non è la stessa cosa di zero."""
    assert fatture[0].lines[1].quantity is None


def test_riepilogo_iva(fatture: list[ParsedInvoice]) -> None:
    riepilogo = fatture[0].vat_summary
    assert len(riepilogo) == 1
    assert riepilogo[0].taxable == Decimal("100.00")
    assert riepilogo[0].tax == Decimal("22.00")


def test_totali_calcolati(fatture: list[ParsedInvoice]) -> None:
    prima = fatture[0]
    assert prima.taxable == Decimal("100.00")
    assert prima.tax == Decimal("22.00")
    assert prima.computed_total == Decimal("122.00")


def test_fattura_senza_iva(fatture: list[ParsedInvoice]) -> None:
    seconda = fatture[1]
    assert seconda.tax == Decimal("0.00")
    assert seconda.computed_total == Decimal("50.00")


def test_totale_dichiarato_facoltativo(fatture: list[ParsedInvoice]) -> None:
    """Il primo corpo dichiara il totale, il secondo no: assente resta None."""
    assert fatture[0].declared_total == Decimal("122.00")
    assert fatture[1].declared_total is None


def test_total_preferisce_il_valore_dichiarato(fatture: list[ParsedInvoice]) -> None:
    """Il totale ufficiale è quello dell'emittente, non la nostra ricostruzione."""
    assert fatture[0].total == fatture[0].declared_total


def test_total_ripiega_sul_calcolo_se_non_dichiarato(fatture: list[ParsedInvoice]) -> None:
    assert fatture[1].total == fatture[1].computed_total == Decimal("50.00")


def test_arrotondamenti(fatture: list[ParsedInvoice]) -> None:
    """L'arrotondamento è il motivo per cui dichiarato e calcolato divergono
    sulle fatture vere: va letto, non ignorato."""
    assert fatture[0].rounding == Decimal("0")
    assert fatture[1].rounding == Decimal("0.03")


def test_segnala_la_discordanza_fra_totali(tmp_path: Path) -> None:
    """Un totale dichiarato diverso dal calcolato va segnalato, non corretto
    con una formula dedotta da pochi documenti."""
    testo = FIXTURE.read_text(encoding="utf-8").replace(
        "<ImportoTotaleDocumento>122.00</ImportoTotaleDocumento>",
        "<ImportoTotaleDocumento>121.97</ImportoTotaleDocumento>",
    )
    path = tmp_path / "scarto.xml"
    path.write_text(testo, encoding="utf-8")

    prima = parse_file(path)[0]

    assert prima.has_total_mismatch
    assert prima.total == Decimal("121.97")  # vince il documento
    assert prima.computed_total == Decimal("122.00")


def test_nessuna_discordanza_quando_i_totali_coincidono(fatture: list[ParsedInvoice]) -> None:
    assert not fatture[0].has_total_mismatch
    # Senza totale dichiarato non c'è niente da confrontare.
    assert not fatture[1].has_total_mismatch


def test_importi_sono_decimal_non_float(fatture: list[ParsedInvoice]) -> None:
    assert isinstance(fatture[0].total, Decimal)
    # Con i float questa uguaglianza fallirebbe.
    assert fatture[0].taxable + fatture[0].tax == Decimal("122.00")


# --- date ---------------------------------------------------------------------


def test_parse_xml_date() -> None:
    assert parse_xml_date("2026-07-28") == date(2026, 7, 28)


@pytest.mark.parametrize("testo", ["28/07/2026", "", "non una data"])
def test_parse_xml_date_rifiuta_formati_sbagliati(testo: str) -> None:
    """Il formato della tabella HTML (gg/mm/aaaa) qui non è valido: sono due
    formati diversi e non vanno confusi."""
    with pytest.raises(ValueError, match="Data XML non riconosciuta"):
        parse_xml_date(testo)


# --- file mal formati ---------------------------------------------------------


def test_file_che_non_e_una_fattura(tmp_path: Path) -> None:
    path = tmp_path / "sbagliato.xml"
    path.write_text("<qualcosa><altro/></qualcosa>", encoding="utf-8")
    with pytest.raises(ValueError, match="Non è una fattura elettronica"):
        parse_file(path)


def test_fattura_senza_corpo(tmp_path: Path) -> None:
    path = tmp_path / "vuota.xml"
    path.write_text(
        "<FatturaElettronica><FatturaElettronicaHeader/></FatturaElettronica>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="senza corpo"):
        parse_file(path)


def test_parse_folder_salta_i_file_rotti(tmp_path: Path, capsys) -> None:
    """Un file illeggibile non deve far perdere tutti gli altri."""
    (tmp_path / "buona.xml").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "rotta.xml").write_text("<non chiuso", encoding="utf-8")

    fatture = parse_folder(tmp_path)

    assert [f.number for f in fatture] == ["FT001", "FT002"]
    assert "rotta.xml" in capsys.readouterr().out


def test_parse_folder_cartella_vuota(tmp_path: Path) -> None:
    assert parse_folder(tmp_path) == []


def test_namespace_ignorato() -> None:
    """Il parser confronta i nomi senza namespace: un cambio di schema non
    deve mandare tutto in pezzi."""
    root = ET.parse(FIXTURE).getroot()
    assert root.tag.startswith("{")  # la radice ha davvero un namespace
    assert len(parse_file(FIXTURE)) == 2
