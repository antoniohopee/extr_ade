"""Test della logica di scaricamento che non richiede il portale.

Dati inventati: nessun numero di fattura o identificativo qui dentro è reale.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from extr_ade.downloader import (
    INDEX_FILENAME,
    destination_dir,
    load_index,
    registered_as,
    safe_filename,
    save_index,
    stored_file,
)
from extr_ade.invoices import Invoice


@pytest.mark.parametrize(
    ("nome", "atteso"),
    [
        ("IT00000000001_AbCdE.xml", "IT00000000001_AbCdE.xml"),
        ("2026/123.xml", "2026_123.xml"),  # le barre creerebbero cartelle
        (r"fatt\123.xml", "fatt_123.xml"),
        ('strano:*?"<>|.xml', "strano_______.xml"),
        ("  spazi.xml  ", "spazi.xml"),
        ("", "fattura"),  # nome vuoto: serve comunque un file
    ],
)
def test_safe_filename(nome: str, atteso: str) -> None:
    assert safe_filename(nome) == atteso


def test_destination_dir_valore_predefinito(monkeypatch: pytest.MonkeyPatch) -> None:
    """Senza configurazione: `fatture/emesse` e `fatture/ricevute`."""
    monkeypatch.delenv("EXTR_ADE_INVOICES_DIR", raising=False)
    assert destination_dir("emesse") == Path("fatture/emesse")
    assert destination_dir("ricevute") == Path("fatture/ricevute")


def test_destination_dir_si_puo_spostare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTR_ADE_INVOICES_DIR", "/mnt/archivio")
    assert destination_dir("emesse") == Path("/mnt/archivio/emesse")


def test_destination_dir_ignora_variabile_vuota(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una variabile presente ma vuota nel .env non deve far salvare in "/"."""
    monkeypatch.setenv("EXTR_ADE_INVOICES_DIR", "   ")
    assert destination_dir("emesse") == Path("fatture/emesse")


def _fattura(detail_id: str = "0FPR00000000001") -> Invoice:
    return Invoice(
        number="IT001",
        issue_date=date(2026, 7, 28),
        client_id="01234567890",
        client_name="Rossi srl",
        taxable=Decimal("100.00"),
        tax=Decimal("22.00"),
        sdi_id="11111111111",
        detail_id=detail_id,
    )


def test_riconosce_una_fattura_gia_scaricata() -> None:
    index = {"0FPR00000000001": "IT01234567890_AbCdE.xml"}
    assert registered_as(_fattura(), index) == "IT01234567890_AbCdE.xml"


def test_fattura_non_presente_nell_indice() -> None:
    index = {"0FPR99999999999": "altra.xml"}
    assert registered_as(_fattura(), index) is None


def test_indice_vuoto() -> None:
    assert registered_as(_fattura(), {}) is None


def test_file_consegnato_resta_scaricato() -> None:
    """L'indice dice che c'è, il file non c'è più: NON va riscaricata.

    È il caso normale, non un'anomalia: gli XML vengono consegnati alla
    contabilità e la cartella si svuota. Se il file mancante contasse come "da
    riprendere", ogni giro dopo una consegna riscaricherebbe tutto lo storico.

    Per riprenderla di proposito si cancella la sua riga da `.scaricate.json`,
    ed è il caso coperto dal test qui sopra sull'indice vuoto.
    """
    index = {"0FPR00000000001": "consegnata.xml"}
    assert registered_as(_fattura(), index) == "consegnata.xml"


def test_file_ancora_in_archivio(tmp_path: Path) -> None:
    (tmp_path / "IT01234567890_AbCdE.xml").write_text("finto", encoding="utf-8")

    trovato = stored_file(tmp_path, "IT01234567890_AbCdE.xml")

    assert trovato is not None
    assert trovato.name == "IT01234567890_AbCdE.xml"


def test_file_non_piu_in_archivio(tmp_path: Path) -> None:
    """Serve solo a scegliere il messaggio da stampare, non a riscaricare."""
    assert stored_file(tmp_path, "sparito.xml") is None


def test_indice_salvato_e_riletto(tmp_path: Path) -> None:
    save_index(tmp_path, {"0FPR00000000001": "IT01234567890_AbCdE.xml"})
    assert load_index(tmp_path) == {"0FPR00000000001": "IT01234567890_AbCdE.xml"}


def test_indice_inesistente_e_vuoto(tmp_path: Path) -> None:
    assert load_index(tmp_path) == {}


def test_indice_rovinato_non_blocca_il_programma(tmp_path: Path) -> None:
    """Un JSON troncato (interruzione, modifica a mano) non deve far esplodere
    niente: si riparte da vuoto e al massimo si riscarica."""
    (tmp_path / INDEX_FILENAME).write_text('{"a": "b"', encoding="utf-8")
    assert load_index(tmp_path) == {}


def test_indice_con_contenuto_inatteso(tmp_path: Path) -> None:
    (tmp_path / INDEX_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert load_index(tmp_path) == {}
