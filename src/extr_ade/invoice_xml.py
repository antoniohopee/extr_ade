"""Lettura degli XML delle fatture elettroniche (formato FatturaPA 1.2).

Trasforma un file scaricato dal portale in dati strutturati: chi ha emesso,
chi ha ricevuto, le righe di dettaglio, il riepilogo IVA.

Nessuna dipendenza esterna: il formato è XML e la libreria standard basta.
Nessun collegamento al portale: si lavora su file già scaricati, quindi tutto
qui dentro è testabile senza rete.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Attenzione: negli XML gli importi usano il PUNTO come separatore decimale
# ("21.34"), mentre nella tabella HTML del portale usano la virgola ("21,34").
# Sono due formati diversi e vanno letti con due funzioni diverse: usare qui
# `invoices.parse_amount` leggerebbe "1.234" come milleduecentotrentaquattro.


@dataclass(frozen=True)
class Party:
    """Un soggetto della fattura: chi emette o chi riceve."""

    vat_country: str  # es. "IT"
    vat_id: str  # partita IVA
    fiscal_code: str  # codice fiscale, quando presente
    name: str  # denominazione o nome e cognome

    @property
    def identifier(self) -> str:
        """L'identificativo più utile: P.IVA se c'è, altrimenti il CF."""
        return self.vat_id or self.fiscal_code


@dataclass(frozen=True)
class InvoiceLine:
    """Una riga di dettaglio della fattura."""

    number: int
    description: str
    quantity: Decimal | None
    unit_price: Decimal
    total_price: Decimal
    vat_rate: Decimal


@dataclass(frozen=True)
class VatSummary:
    """Una riga del riepilogo IVA: per ogni aliquota, imponibile e imposta."""

    vat_rate: Decimal
    taxable: Decimal
    tax: Decimal
    rounding: Decimal  # Arrotondamento, spesso zero ma non sempre


@dataclass(frozen=True)
class ParsedInvoice:
    """Una fattura letta dall'XML."""

    document_type: str  # es. "TD01"
    number: str
    issue_date: date
    currency: str
    declared_total: Decimal | None  # ImportoTotaleDocumento, se indicato
    supplier: Party  # CedentePrestatore
    customer: Party  # CessionarioCommittente
    lines: list[InvoiceLine] = field(default_factory=list)
    vat_summary: list[VatSummary] = field(default_factory=list)
    source_file: Path | None = None

    @property
    def taxable(self) -> Decimal:
        """Totale imponibile, sommato dal riepilogo IVA."""
        return sum((row.taxable for row in self.vat_summary), Decimal("0"))

    @property
    def tax(self) -> Decimal:
        """Totale imposta, sommato dal riepilogo IVA."""
        return sum((row.tax for row in self.vat_summary), Decimal("0"))

    @property
    def rounding(self) -> Decimal:
        """Somma degli arrotondamenti dichiarati nel riepilogo IVA."""
        return sum((row.rounding for row in self.vat_summary), Decimal("0"))

    @property
    def computed_total(self) -> Decimal:
        """Imponibile + imposta, cioè il totale ricostruito da noi.

        Attenzione: NON coincide sempre con il totale del documento. Sulle
        fatture vere si è visto uno scarto di qualche centesimo, dovuto agli
        arrotondamenti (vedi `rounding`). Poche decine di centesimi su undici
        fatture, ma su un anno diventano una riconciliazione che non torna mai.
        """
        return self.taxable + self.tax

    @property
    def total(self) -> Decimal:
        """Il totale della fattura.

        Se l'emittente ha dichiarato `ImportoTotaleDocumento` usiamo quello:
        è il valore ufficiale del documento, mentre il nostro è una
        ricostruzione. Solo se manca ripieghiamo sul calcolo.

        Non applichiamo una formula nostra per far quadrare i due valori:
        dai file veri sembra che il totale dichiarato corrisponda a
        `imponibile + imposta - arrotondamenti`, ma è una regola dedotta da
        pochi documenti di un solo emittente. Se un domani non valesse per
        qualcun altro, produrremmo totali sbagliati senza nessun segnale.
        Meglio dichiarare la differenza (vedi `has_total_mismatch`) che
        nasconderla.
        """
        return self.declared_total if self.declared_total is not None else self.computed_total

    @property
    def has_total_mismatch(self) -> bool:
        """Vero se il totale dichiarato e quello ricostruito non coincidono.

        Non è di per sé un errore: spesso è solo l'arrotondamento. Serve a
        poter controllare, invece di scoprire uno scarto mesi dopo.
        """
        return self.declared_total is not None and self.declared_total != self.computed_total


# --- lettura del file ---------------------------------------------------------


def parse_file(path: Path) -> list[ParsedInvoice]:
    """Legge un XML e restituisce le fatture che contiene.

    Restituisce una LISTA e non una sola fattura perché il formato prevede un
    header e più `FatturaElettronicaBody` (fatturazione a lotti): leggerne
    solo uno perderebbe le altre in silenzio.
    """
    root = ET.parse(path).getroot()
    return parse_root(root, source_file=path)


def parse_root(root: ET.Element, *, source_file: Path | None = None) -> list[ParsedInvoice]:
    """Come `parse_file`, ma partendo da un albero già caricato (usata dai test)."""
    header = _child(root, "FatturaElettronicaHeader")
    if header is None:
        raise ValueError("Non è una fattura elettronica: manca FatturaElettronicaHeader")

    supplier = _read_party(_child(header, "CedentePrestatore"))
    customer = _read_party(_child(header, "CessionarioCommittente"))

    bodies = _children(root, "FatturaElettronicaBody")
    if not bodies:
        raise ValueError("Fattura senza corpo: manca FatturaElettronicaBody")

    return [
        _read_body(body, supplier, customer, source_file) for body in bodies
    ]


def parse_folder(folder: Path) -> list[ParsedInvoice]:
    """Legge tutti gli XML di una cartella.

    Un file illeggibile non ferma gli altri: viene segnalato e si prosegue.
    Con centinaia di fatture, fermarsi al primo file storto renderebbe
    inutilizzabile tutto il resto.
    """
    invoices: list[ParsedInvoice] = []

    for path in sorted(folder.glob("*.xml")):
        try:
            invoices.extend(parse_file(path))
        except (ET.ParseError, ValueError) as error:
            print(f"  ! {path.name}: non leggibile ({error})")

    return invoices


# --- pezzi della fattura ------------------------------------------------------


def _read_party(node: ET.Element | None) -> Party:
    """Legge un soggetto (cedente o cessionario)."""
    if node is None:
        return Party(vat_country="", vat_id="", fiscal_code="", name="")

    registry = _child(node, "DatiAnagrafici")
    vat = _child(registry, "IdFiscaleIVA") if registry is not None else None
    personal = _child(registry, "Anagrafica") if registry is not None else None

    # La denominazione c'è per le società; le persone fisiche hanno invece
    # nome e cognome separati.
    name = _text(personal, "Denominazione")
    if not name and personal is not None:
        name = " ".join(
            part for part in (_text(personal, "Nome"), _text(personal, "Cognome")) if part
        )

    return Party(
        vat_country=_text(vat, "IdPaese"),
        vat_id=_text(vat, "IdCodice"),
        fiscal_code=_text(registry, "CodiceFiscale"),
        name=name,
    )


def _read_body(
    body: ET.Element,
    supplier: Party,
    customer: Party,
    source_file: Path | None,
) -> ParsedInvoice:
    """Legge un corpo fattura: dati del documento, righe e riepilogo IVA."""
    general = _child(body, "DatiGenerali")
    document = _child(general, "DatiGeneraliDocumento") if general is not None else None

    if document is None:
        raise ValueError("Manca DatiGeneraliDocumento")

    goods = _child(body, "DatiBeniServizi")

    return ParsedInvoice(
        document_type=_text(document, "TipoDocumento"),
        number=_text(document, "Numero"),
        issue_date=parse_xml_date(_text(document, "Data")),
        currency=_text(document, "Divisa"),
        declared_total=_decimal_or_none(_text(document, "ImportoTotaleDocumento")),
        supplier=supplier,
        customer=customer,
        lines=[_read_line(node) for node in _children(goods, "DettaglioLinee")],
        vat_summary=[_read_vat(node) for node in _children(goods, "DatiRiepilogo")],
        source_file=source_file,
    )


def _read_line(node: ET.Element) -> InvoiceLine:
    """Legge una riga di dettaglio."""
    return InvoiceLine(
        number=int(_text(node, "NumeroLinea") or 0),
        description=_text(node, "Descrizione"),
        quantity=_decimal_or_none(_text(node, "Quantita")),
        unit_price=_decimal(_text(node, "PrezzoUnitario")),
        total_price=_decimal(_text(node, "PrezzoTotale")),
        vat_rate=_decimal(_text(node, "AliquotaIVA")),
    )


def _read_vat(node: ET.Element) -> VatSummary:
    """Legge una riga del riepilogo IVA."""
    return VatSummary(
        vat_rate=_decimal(_text(node, "AliquotaIVA")),
        taxable=_decimal(_text(node, "ImponibileImporto")),
        tax=_decimal(_text(node, "Imposta")),
        rounding=_decimal(_text(node, "Arrotondamento")),
    )


# --- funzioni di base ---------------------------------------------------------


def parse_xml_date(text: str) -> date:
    """Converte una data dell'XML (formato ISO, "2026-07-28") in `date`.

    Diversa da `invoices.parse_date`, che legge il formato gg/mm/aaaa della
    tabella HTML. Sono due formati diversi nella stessa applicazione: tenerli
    separati evita conversioni sbagliate che passerebbero inosservate.
    """
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError) as error:
        raise ValueError(f"Data XML non riconosciuta: {text!r}") from error


def _decimal(text: str) -> Decimal:
    """Converte un importo dell'XML in Decimal. Vuoto vale zero."""
    value = _decimal_or_none(text)
    return value if value is not None else Decimal("0")


def _decimal_or_none(text: str) -> Decimal | None:
    """Come `_decimal`, ma distingue "assente" da "zero".

    La distinzione serve sui campi facoltativi: `ImportoTotaleDocumento` non
    indicato non è la stessa cosa di un totale pari a zero.
    """
    if not text or not text.strip():
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation as error:
        raise ValueError(f"Importo XML non riconosciuto: {text!r}") from error


def _local_name(tag: str) -> str:
    """Nome del tag senza il namespace.

    La radice del documento ha un namespace, i figli no. Confrontare i nomi
    ignorando il namespace rende il parser tollerante a un eventuale cambio di
    versione dello schema.
    """
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element | None, name: str) -> list[ET.Element]:
    """Tutti i figli diretti con questo nome."""
    if node is None:
        return []
    return [child for child in node if _local_name(child.tag) == name]


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    """Il primo figlio diretto con questo nome, se c'è."""
    children = _children(node, name)
    return children[0] if children else None


def _text(node: ET.Element | None, name: str) -> str:
    """Il testo di un figlio diretto. Stringa vuota se manca."""
    child = _child(node, name)
    return (child.text or "").strip() if child is not None else ""
