"""Scaricamento dei file XML delle fatture.

Il portale non espone un indirizzo da cui prelevare l'XML: il file è prodotto
da una funzione JavaScript della pagina di dettaglio. Quindi si passa dal
browser, cliccando il bottone e intercettando il file che ne esce.

Conseguenza pratica: per ogni fattura bisogna aprire il suo dettaglio. Sono
uno o due secondi a fattura. Se un domani diventasse troppo lento, l'alternativa
è il servizio "Consultazioni e download massivi" (`/cons/mass-web/`), che
prepara pacchetti in modo asincrono.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from extr_ade.constants import (
    DOWNLOAD_BUTTON_NAME,
    ENV_INVOICES_DIR,
    INVOICES_DIR,
)
from extr_ade.invoices import Invoice

# Quanto aspettiamo che il file arrivi dopo il click.
DOWNLOAD_TIMEOUT_MS = 60_000

# Pausa fra un download e il successivo. Non è prudenza eccessiva: venti
# richieste consecutive a raffica su un portale pubblico sono maleducate e
# rischiano di farci limitare o bloccare.
PAUSE_SECONDS = 1.0

# Caratteri che non possono comparire in un nome di file.
UNSAFE_CHARACTERS = re.compile(r'[/\\:*?"<>|]')

# Indice delle fatture già scaricate, uno per cartella. Collega
# l'identificativo della fattura sul portale al nome del file salvato.
#
# Serve perché il nome del file lo decide il portale e non lo conosciamo prima
# di aver cliccato "Download": senza indice non potremmo sapere se una fattura
# è già stata presa, e la riscaricheremmo ogni volta.
INDEX_FILENAME = ".scaricate.json"


def safe_filename(name: str) -> str:
    """Rende un nome utilizzabile come nome di file.

    I numeri di fattura contengono spesso barre ("2026/123"), che in un nome
    di file verrebbero interpretate come cartelle: il file finirebbe altrove,
    o la scrittura fallirebbe.
    """
    cleaned = UNSAFE_CHARACTERS.sub("_", name).strip()
    return cleaned or "fattura"


def destination_dir(kind: str) -> Path:
    """Cartella dove salvare gli XML: `fatture/emesse` o `fatture/ricevute`.

    Sta nella radice del progetto e viene creata al primo download.

    `EXTR_ADE_INVOICES_DIR` permette di spostarla altrove senza toccare il
    codice: serve sul server del cron, dove i file vanno tipicamente su un
    disco diverso da quello del progetto.
    """
    base = Path(os.getenv(ENV_INVOICES_DIR, "").strip() or INVOICES_DIR)
    return base / kind


def download_invoice(page: Page, invoice: Invoice, folder: Path) -> Path | None:
    """Scarica l'XML di una fattura già aperta in dettaglio.

    Restituisce il percorso del file, o None se il download non è riuscito.
    Non solleva eccezioni: in uno scaricamento di venti fatture, una che va
    male non deve far fallire le altre diciannove.
    """
    try:
        button = page.get_by_role("button", name=DOWNLOAD_BUTTON_NAME)
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            button.first.click()
        download = download_info.value

        # Il portale propone già un nome sensato; se mancasse ripieghiamo sul
        # numero della fattura.
        suggested = download.suggested_filename or f"{invoice.number}.xml"
        destination = folder / safe_filename(suggested)

        download.save_as(destination)
        return destination

    except PlaywrightTimeout:
        print(f"  ! {invoice.number}: il file non è arrivato entro il tempo massimo")
        return None
    except Exception as error:  # noqa: BLE001 - vogliamo proseguire comunque
        print(f"  ! {invoice.number}: download non riuscito ({error})")
        return None


def download_all(
    page: Page,
    invoices: list[Invoice],
    kind: str,
    open_detail,  # callable(page, invoice) -> None
) -> list[Path]:
    """Scarica un elenco di fatture, una alla volta.

    `open_detail` arriva da fuori invece di essere importata qui: `consultation`
    usa questo modulo, e importarlo a sua volta creerebbe un import circolare.

    Le fatture già scaricate vengono saltate: se ieri hai preso le prime venti
    e oggi ne chiedi trenta, riscarichiamo solo le dieci nuove. Serve anche al
    cron, che altrimenti riscaricherebbe ogni notte tutto lo storico.
    """
    folder = destination_dir(kind)
    folder.mkdir(parents=True, exist_ok=True)
    index = load_index(folder)

    saved: list[Path] = []

    for number, invoice in enumerate(invoices, start=1):
        label = f"[{number}/{len(invoices)}] {invoice.number}"

        existing = already_downloaded(folder, invoice, index)
        if existing:
            print(f"  = {label}: già presente ({existing.name})")
            saved.append(existing)
            continue

        print(f"  . {label}: apro il dettaglio...")
        open_detail(page, invoice)

        path = download_invoice(page, invoice, folder)
        if path:
            print(f"  + {label}: salvata in {path}")
            saved.append(path)

            # L'indice si aggiorna subito, non alla fine: se interrompi a metà,
            # quello che è stato scaricato resta registrato come scaricato.
            index[invoice.detail_id] = path.name
            save_index(folder, index)

        time.sleep(PAUSE_SECONDS)

    return saved


def load_index(folder: Path) -> dict[str, str]:
    """Legge l'indice delle fatture già scaricate in questa cartella.

    Un indice illeggibile (troncato, modificato a mano) non deve bloccare il
    programma: si riparte da vuoto, al massimo si riscarica qualcosa.
    """
    path = folder / INDEX_FILENAME
    if not path.exists():
        return {}

    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        print(f"  ! indice illeggibile ({error}): riparto da vuoto")
        return {}

    return content if isinstance(content, dict) else {}


def save_index(folder: Path, index: dict[str, str]) -> None:
    """Scrive l'indice. Va richiamato dopo ogni download, non alla fine.

    Se il programma viene interrotto a metà di venti fatture, quelle già prese
    devono risultare prese: altrimenti al giro dopo si riscaricano, e sulle
    fatture ricevute si rifarebbe la presa visione.
    """
    path = folder / INDEX_FILENAME
    path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def already_downloaded(
    folder: Path, invoice: Invoice, index: dict[str, str]
) -> Path | None:
    """Dice se questa fattura è già stata scaricata, PRIMA di cliccare.

    Il controllo deve avvenire prima del click perché è il click stesso a
    registrare la presa visione sulle fatture ricevute.

    Il nome del file lo sceglie il portale e non è ricavabile dai dati che
    abbiamo (è partita IVA del trasmittente + un progressivo dello SdI): per
    questo teniamo un indice che collega l'identificativo della fattura al
    nome del file.

    Se l'indice cita un file che non c'è più (l'hai cancellato o spostato),
    la fattura va considerata da riscaricare: fidarsi dell'indice e basta
    lascerebbe un buco nell'archivio senza dirlo a nessuno.
    """
    filename = index.get(invoice.detail_id)
    if not filename:
        return None

    path = folder / filename
    return path if path.exists() else None
