"""Test della logica di scaricamento che non richiede il portale.

Dati inventati: nessun numero di fattura o identificativo qui dentro è reale.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from extr_ade.downloader import (
    DetailOutcome,
    INDEX_FILENAME,
    MAX_PAUSE_SECONDS,
    PORTAL_RETRIES,
    RECOVERY_PAUSE_SECONDS,
    destination_dir,
    load_index,
    CLEAN_STREAK_TO_SPEED_UP,
    InvoiceOutcome,
    PAUSE_SECONDS,
    Progress,
    SECOND_PASS_PAUSE_SECONDS,
    SLOW_PAUSE_SECONDS,
    download_all,
    fetch_invoice,
    pause_before_retry,
    registered_as,
    safe_filename,
    save_index,
    stored_file,
)
from extr_ade.invoices import Invoice
from extr_ade.portal import PortalDownError


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


# --- quando il portale rifiuta la richiesta -----------------------------------
#
# Qui si prova il comportamento aggiunto il 2026-10-05: riconoscere il rifiuto
# del portale, tornare all'elenco e riproporre la STESSA fattura, invece di
# darla per persa. Prima il rifiuto veniva scambiato per "fattura non più
# nell'elenco" e il giro proseguiva accelerando: 31 fallimenti in un minuto.


def test_attesa_raddoppia_a_ogni_tentativo() -> None:
    assert pause_before_retry(1) == RECOVERY_PAUSE_SECONDS
    assert pause_before_retry(2) == RECOVERY_PAUSE_SECONDS * 2
    assert pause_before_retry(3) == RECOVERY_PAUSE_SECONDS * 4


def test_attesa_non_cresce_oltre_il_tetto() -> None:
    """Senza tetto, al decimo tentativo aspetteremmo più di due ore."""
    assert pause_before_retry(50) == MAX_PAUSE_SECONDS


@pytest.fixture
def senza_attese(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toglie le pause vere e il download vero.

    Le pause perché i test non devono aspettare quindici secondi. Il download
    perché `fetch_invoice` ora copre anche quello, e qui stiamo provando i
    tentativi, non il salvataggio del file.
    """
    monkeypatch.setattr("extr_ade.downloader.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "extr_ade.downloader.download_invoice",
        lambda page, invoice, folder: folder / f"{invoice.number}.xml",
    )


def test_riprova_la_stessa_fattura_dopo_un_rifiuto(senza_attese: None) -> None:
    """Il caso che prima costava una fattura persa a ogni rifiuto."""
    tentativi = []
    recuperi = []

    def open_detail(page, invoice):
        tentativi.append(invoice.detail_id)
        if len(tentativi) == 1:
            raise PortalDownError("Il sistema non è al momento disponibile")

    risultato = fetch_invoice(
        open_detail, recuperi.append, None, _fattura(), Path("x"), "[1/1]"
    )

    assert risultato.outcome is DetailOutcome.OK
    assert risultato.refused is True  # chi chiama deve saperlo, per rallentare il resto
    assert len(tentativi) == 2  # la stessa fattura, non la successiva
    assert len(recuperi) == 1  # ed è tornato all'elenco prima di riprovare


def test_rinuncia_dopo_i_tentativi_previsti(senza_attese: None) -> None:
    """Se insiste a dire di no, la fattura si perde: ma dopo aver provato."""
    tentativi = []

    def open_detail(page, invoice):
        tentativi.append(invoice.detail_id)
        raise PortalDownError("Il sistema non è al momento disponibile")

    risultato = fetch_invoice(
        open_detail, lambda page: None, None, _fattura(), Path("x"), "[1/1]"
    )

    assert risultato.outcome is DetailOutcome.FAILED
    assert risultato.refused is True
    assert len(tentativi) == PORTAL_RETRIES


def test_senza_rifiuti_non_cambia_niente(senza_attese: None) -> None:
    """Il percorso normale deve restare quello di prima: un tentativo, zero attese."""
    recuperi = []

    risultato = fetch_invoice(
        lambda page, invoice: None, recuperi.append, None, _fattura(), Path("x"), "[1/1]"
    )

    assert risultato.outcome is DetailOutcome.OK
    assert risultato.refused is False
    assert recuperi == []


def test_un_ritorno_all_elenco_fallito_non_fa_cadere_il_giro(senza_attese: None) -> None:
    """Se nemmeno il recupero funziona, questa fattura si perde e si prosegue.

    L'alternativa sarebbe far esplodere il giro e perdere la sessione del
    browser, cioè anche tutte le fatture ancora da prendere.
    """

    def open_detail(page, invoice):
        raise PortalDownError("Il sistema non è al momento disponibile")

    def recover(page):
        raise RuntimeError("elenco irraggiungibile")

    risultato = fetch_invoice(
        open_detail, recover, None, _fattura(), Path("x"), "[1/1]"
    )

    assert risultato.outcome is DetailOutcome.FAILED
    assert risultato.refused is True


def test_un_recupero_ancora_rifiutato_usa_i_tentativi_rimasti(senza_attese: None) -> None:
    """Se il portale è giù anche durante il recupero, non si rinuncia subito.

    Prima questo ramo finiva nel catch generico e bruciava la fattura al primo
    tentativo, rendendo inutili gli altri due proprio nel caso per cui
    esistono.
    """
    tentativi = []

    def open_detail(page, invoice):
        tentativi.append(invoice.detail_id)
        raise PortalDownError("Il sistema non è al momento disponibile")

    def recover(page):
        raise PortalDownError("Il sistema non è al momento disponibile")

    risultato = fetch_invoice(
        open_detail, recover, None, _fattura(), Path("x"), "[1/1]"
    )

    assert risultato.outcome is DetailOutcome.FAILED
    assert risultato.refused is True
    assert len(tentativi) == PORTAL_RETRIES  # tutti, non uno solo


def test_un_rifiuto_durante_il_download_viene_ripreso(senza_attese: None) -> None:
    """Il buco in cui è caduta la fattura 21 su 33 del 2026-10-05.

    Lì il dettaglio si apriva, il click partiva, e il rifiuto arrivava nel
    download: con il controllo solo sull'apertura la fattura era persa dopo
    sessanta secondi di attesa. Ora il tentativo copre anche il download,
    quindi il rifiuto si riconosce e la fattura si riprende.
    """
    tentativi = []

    def download(page, invoice, folder):
        tentativi.append(invoice.number)
        if len(tentativi) == 1:
            raise PortalDownError("Il sistema non è al momento disponibile")
        return folder / "scaricata.xml"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("extr_ade.downloader.download_invoice", download)
        risultato = fetch_invoice(
            lambda page, invoice: None,
            lambda page: None,
            None,
            _fattura(),
            Path("x"),
            "[1/1]",
        )

    assert risultato.outcome is DetailOutcome.OK
    assert risultato.path == Path("x/scaricata.xml")
    assert risultato.refused is True
    assert len(tentativi) == 2


# --- il secondo passaggio -----------------------------------------------------


def _download_all_finto(monkeypatch, tmp_path, invoices, open_detail):
    """Fa girare `download_all` senza browser, restituendo i file salvati."""
    monkeypatch.setattr("extr_ade.downloader.time.sleep", lambda _: None)
    monkeypatch.setattr("extr_ade.downloader.destination_dir", lambda kind: tmp_path / kind)
    monkeypatch.setattr(
        "extr_ade.downloader.download_invoice",
        lambda page, invoice, folder: folder / f"{invoice.number}.xml",
    )
    return download_all(None, invoices, "emesse", open_detail, lambda page: None)


def test_il_secondo_passaggio_recupera_una_fattura(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lo scenario vero del 2026-10-05: il portale si riprende poco dopo.

    Una fattura viene rifiutata per tutti e tre i tentativi del primo
    passaggio, e funziona al primo tentativo del secondo. Prima restava fra
    le "NON riuscite" e toccava rilanciare a mano.
    """
    rifiuti = {"rimasti": PORTAL_RETRIES}

    def open_detail(page, invoice):
        if invoice.number == "IT001" and rifiuti["rimasti"] > 0:
            rifiuti["rimasti"] -= 1
            raise PortalDownError("Il sistema non è al momento disponibile")

    fatture = [_fattura("0FPR1"), _fattura("0FPR2"), _fattura("0FPR3")]
    for fattura, numero in zip(fatture, ("IT000", "IT001", "IT002")):
        object.__setattr__(fattura, "number", numero)

    salvati = _download_all_finto(monkeypatch, tmp_path, fatture, open_detail)

    # Tutte e tre, nonostante una fosse fallita in tutto il primo passaggio.
    assert len(salvati) == 3
    assert rifiuti["rimasti"] == 0


def test_senza_fallimenti_non_c_e_secondo_passaggio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Il giro normale non deve diventare più lento né più chiacchierone."""
    aperture = []
    fatture = [_fattura("0FPR1"), _fattura("0FPR2")]
    object.__setattr__(fatture[1], "number", "IT002")

    salvati = _download_all_finto(
        monkeypatch, tmp_path, fatture, lambda page, invoice: aperture.append(invoice)
    )

    assert len(salvati) == 2
    assert len(aperture) == 2  # una volta ciascuna, nessun ripasso


# --- i conti del riepilogo ----------------------------------------------------
#
# Il riepilogo è il prodotto principale di un giro da cron: è l'unica cosa che
# il proprietario legge. Un numero sbagliato lì manda a cercare un guasto che
# non c'è, che è il problema da cui è nato tutto questo lavoro.


def test_una_fattura_fallita_e_poi_ripresa_conta_una_volta_sola() -> None:
    """Due esiti nello stesso giro per la stessa fattura: vale l'ultimo."""
    progress = Progress()
    fattura = _fattura("0FPR1")

    progress.record(fattura, InvoiceOutcome.FAILED)
    progress.record(fattura, InvoiceOutcome.SAVED)

    assert progress.count(InvoiceOutcome.SAVED) == 1
    assert progress.count(InvoiceOutcome.FAILED) == 0
    assert len(progress.outcomes) == 1


def test_chi_fallisce_e_non_viene_riprovato_resta_fallito() -> None:
    """Il caso che la vecchia contabilità sbagliava.

    Se il secondo passaggio si ferma a metà, le fatture del primo che non ha
    fatto in tempo a riprendere sono fallite una volta: finivano fra le "mai
    provate", che è falso e per giunta rassicurante.
    """
    progress = Progress()
    fatture = [_fattura(f"0FPR{i}") for i in range(3)]
    for fattura in fatture:
        progress.record(fattura, InvoiceOutcome.FAILED)
    progress.record(fatture[0], InvoiceOutcome.SAVED)  # ripresa nel secondo giro

    assert progress.count(InvoiceOutcome.FAILED) == 2
    assert [f.detail_id for f in progress.failed_among(fatture)] == ["0FPR1", "0FPR2"]


def test_le_mai_provate_sono_solo_quelle_mai_toccate() -> None:
    progress = Progress()
    progress.record(_fattura("0FPR1"), InvoiceOutcome.SAVED)
    # 10 richieste, 1 con un esito -> 9 mai provate
    assert 10 - len(progress.outcomes) == 9


# --- il passo lento non resta appiccicato a tutto il giro ---------------------


def test_il_passo_torna_normale_dopo_una_serie_pulita(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Un rifiuto all'inizio non deve rallentare le 200 fatture successive."""
    rifiutata_una_volta = {"fatto": False}

    def open_detail(page, invoice):
        if not rifiutata_una_volta["fatto"]:
            rifiutata_una_volta["fatto"] = True
            raise PortalDownError("Il sistema non è al momento disponibile")

    pause = []
    monkeypatch.setattr("extr_ade.downloader.time.sleep", pause.append)
    monkeypatch.setattr("extr_ade.downloader.destination_dir", lambda kind: tmp_path / kind)
    monkeypatch.setattr(
        "extr_ade.downloader.download_invoice",
        lambda page, invoice, folder: folder / f"{invoice.detail_id}.xml",
    )

    fatture = [_fattura(f"0FPR{i}") for i in range(CLEAN_STREAK_TO_SPEED_UP + 3)]
    download_all(None, fatture, "emesse", open_detail, lambda page: None)

    # All'inizio il passo è lento, alla fine è tornato quello normale.
    assert SLOW_PAUSE_SECONDS in pause
    assert pause[-1] == PAUSE_SECONDS


def test_senza_rifiuti_il_secondo_passaggio_non_aspetta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Una fattura sparita dall'elenco non deve costare un minuto a ogni giro."""

    def open_detail(page, invoice):
        if invoice.detail_id == "0FPR1":
            raise RuntimeError("non più nell'elenco")

    pause = []
    monkeypatch.setattr("extr_ade.downloader.time.sleep", pause.append)
    monkeypatch.setattr("extr_ade.downloader.destination_dir", lambda kind: tmp_path / kind)
    monkeypatch.setattr(
        "extr_ade.downloader.download_invoice",
        lambda page, invoice, folder: folder / f"{invoice.detail_id}.xml",
    )

    download_all(
        None, [_fattura("0FPR1"), _fattura("0FPR2")], "emesse", open_detail, lambda p: None
    )

    assert SECOND_PASS_PAUSE_SECONDS not in pause
