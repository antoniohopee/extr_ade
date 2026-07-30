"""Raccolta delle credenziali di accesso, da terminale.

Tre modi di ottenerle, in ordine di preferenza:

1. sono già in `.env` e vanno bene   -> non chiediamo niente;
2. sono in `.env` ma vuoi cambiarle  -> le richiediamo (caso tipico: la
   password dell'AdE scade ogni sei mesi e va rifatta);
3. non ci sono                       -> le chiediamo e proponiamo di salvarle.

Password e PIN si inseriscono con `getpass`: non compaiono a schermo mentre
li digiti e non finiscono nella cronologia del terminale.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from dotenv import load_dotenv

from extr_ade.constants import ENV_PASSWORD, ENV_PIN, ENV_USERNAME

ENV_PATH = Path(".env")


@dataclass(frozen=True)
class Credentials:
    """Le tre cose che servono per entrare: utente, password, PIN."""

    username: str
    password: str
    pin: str

    def __repr__(self) -> str:
        """Nasconde password e PIN.

        Serve davvero: senza questo, un `print(creds)` o anche solo un
        traceback che capita di incollare da qualche parte stamperebbe le
        credenziali in chiaro. La dataclass di default fa esattamente quello.
        """
        return f"Credentials(username={self.username!r}, password='***', pin='***')"


def load_from_env() -> Credentials | None:
    """Legge le credenziali dal file .env. Restituisce None se incomplete."""
    load_dotenv(ENV_PATH, override=True)

    username = os.getenv(ENV_USERNAME, "").strip()
    password = os.getenv(ENV_PASSWORD, "").strip()
    pin = os.getenv(ENV_PIN, "").strip()

    # Servono tutte e tre: due su tre non è "quasi fatto", è un login fallito.
    if not (username and password and pin):
        return None
    return Credentials(username=username, password=password, pin=pin)


def ask_interactively() -> Credentials:
    """Chiede le tre credenziali da terminale, rifiutando i valori vuoti."""
    print("\nInserisci le credenziali Entratel/Fisconline.")
    print("(password e PIN non compaiono mentre li digiti: è normale)\n")

    username = _ask_non_empty("Utente / Codice fiscale: ", hidden=False)
    password = _ask_non_empty("Password              : ", hidden=True)
    pin = _ask_non_empty("PIN                   : ", hidden=True)

    return Credentials(username=username, password=password, pin=pin)


def _ask_non_empty(prompt: str, *, hidden: bool) -> str:
    """Ripete la domanda finché non arriva una risposta non vuota."""
    while True:
        value = (getpass(prompt) if hidden else input(prompt)).strip()
        if value:
            return value
        print("  -> non può essere vuoto, riprova.")


def save_to_env(credentials: Credentials) -> None:
    """Scrive le credenziali in .env preservando il resto del file.

    Non riscriviamo il file da zero: `.env` contiene anche altre variabili
    (cartella dati, livello di log) e i commenti che le spiegano. Sostituiamo
    solo le righe delle tre chiavi, e aggiungiamo quelle che mancano.
    """
    values = {
        ENV_USERNAME: credentials.username,
        ENV_PASSWORD: credentials.password,
        ENV_PIN: credentials.pin,
    }

    existing = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pending = dict(values)
    result: list[str] = []

    for line in existing:
        key = _key_of(line)
        if key in pending:
            result.append(f"{key}={pending.pop(key)}")
        else:
            result.append(line)

    # Chiavi che nel file non c'erano ancora.
    result.extend(f"{key}={value}" for key, value in pending.items())

    ENV_PATH.write_text("\n".join(result) + "\n", encoding="utf-8")

    # Leggibile e scrivibile solo dal proprietario: dentro ci sono credenziali
    # fiscali, non deve poterlo aprire ogni utente della macchina.
    ENV_PATH.chmod(0o600)

    print(f"\nCredenziali salvate in {ENV_PATH} (permessi 600).")


def _key_of(line: str) -> str | None:
    """Estrae il nome della variabile da una riga di .env, se ce l'ha."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def get_credentials() -> Credentials:
    """Punto d'ingresso: restituisce le credenziali da usare per il login.

    Se il terminale non è interattivo (tipico del cron) non facciamo domande:
    o le credenziali sono nel .env, o ci fermiamo con un errore chiaro. Uno
    script schedulato che si blocca su un input() è un guasto silenzioso, il
    peggior tipo di guasto.
    """
    stored = load_from_env()

    if not sys.stdin.isatty():
        if stored is None:
            raise SystemExit(
                f"Nessun terminale interattivo e credenziali mancanti in {ENV_PATH}. "
                f"Riempi {ENV_USERNAME}, {ENV_PASSWORD} e {ENV_PIN}."
            )
        return stored

    if stored is None:
        credentials = ask_interactively()
        if _ask_yes_no("Salvare queste credenziali in .env per la prossima volta?"):
            save_to_env(credentials)
        return credentials

    # Mostriamo solo l'utente: serve a farti riconoscere l'account. Password e
    # PIN non li stampiamo mai, nemmeno accorciati.
    print(f"\nTrovate credenziali salvate per l'utente: {stored.username}")
    if _ask_yes_no("Usare queste credenziali?"):
        return stored

    # Caso password scaduta: ne inseriamo di nuove e proponiamo di aggiornare
    # il .env, così al giro dopo tornano ad andare bene.
    credentials = ask_interactively()
    if _ask_yes_no("Aggiornare il .env con queste nuove credenziali?"):
        save_to_env(credentials)
    return credentials


def _ask_yes_no(question: str) -> bool:
    """Domanda sì/no. Invio a vuoto = sì, che è la risposta giusta quasi sempre."""
    while True:
        answer = input(f"{question} [S/n] ").strip().lower()
        if answer in ("", "s", "si", "sì", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  -> rispondi S oppure N.")


if __name__ == "__main__":
    # Prova manuale del modulo, senza toccare il portale.
    credentials = get_credentials()
    print("\nOK, userei:", credentials)
