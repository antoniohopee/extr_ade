"""Costanti del portale dell'Agenzia delle Entrate.

REGOLA: qui dentro finisce solo roba VERIFICATA, cioè letta dalla pagina vera.
Niente URL o selettori ipotizzati. Ogni voce dice quando è stata controllata.

Questo è il primo file da riaprire quando qualcosa smette di funzionare: gli
endpoint dell'AdE non sono documentati né stabili, quindi è normale che prima
o poi cambino. Tenerli tutti qui significa che la riparazione è una modifica
in un punto solo, non una caccia al selettore sparso per il codice.
"""

from __future__ import annotations

# Pagina di login con credenziali Entratel/Fisconline.
# Il parametro `goto` dice al portale dove mandarci DOPO il login: qui punta
# già all'area Fatture e Corrispettivi (`to=FATBTB`), così saltiamo un
# passaggio di navigazione.
# Verificato il 2026-07-30: risponde HTTP 200 e contiene il form credenziali.
LOGIN_URL = (
    "https://iampe.agenziaentrate.gov.it/sam/UI/Login"
    "?realm=/agenziaentrate"
    "&goto=https%3A%2F%2Fportale.agenziaentrate.gov.it%3A443"
    "%2FPortaleWeb%2Fhome%3Fto%3DFATBTB"
)

# La pagina di login è a schede: SPID, CIE, CNS, Fisconline/Entratel, Sister.
# La nostra è la quarta, e finché non la si clicca il pannello che la contiene
# ha `display: none` e i campi non sono compilabili. Sono presenti nel DOM ma
# nascosti: è il motivo per cui non basta aspettare il caricamento.
#
# La cerchiamo per ruolo ARIA + testo invece che per `#tab-4`: il numero
# dipende dall'ordine delle schede e cambierebbe se ne aggiungessero una, il
# testo no.
LOGIN_TAB_NAME = "Fisconline/Entratel"

# Il pannello che contiene i campi (l'`href` della scheda punta qui).
LOGIN_PANEL_SELECTOR = "#tab-4"

# Selettori dei tre campi.
#
# Usiamo gli `id` e NON gli attributi `name`: nella stessa pagina il pannello
# Sister riusa gli stessi name `IDToken1` e `IDToken2`. Compilare per name
# significherebbe scrivere le credenziali nel posto sbagliato, con un errore
# di login incomprensibile da diagnosticare.
#
# Verificati il 2026-07-30 ispezionando la pagina.
USERNAME_SELECTOR = "#username-fo-ent"  # name=IDToken1
PASSWORD_SELECTOR = "#password-fo-ent-1"  # name=IDToken2
PIN_SELECTOR = "#pin-fo-ent"  # name=IDToken3

# Il bottone di invio. Attenzione: i tre campi NON stanno dentro un <form>
# (verificato), quindi non si può risalire al form per trovare il bottone:
# lo cerchiamo dentro il pannello, che è l'unico contenitore che li raggruppa.
SUBMIT_BUTTON_NAME = "Accedi"

# Come si riconosce che il login NON è passato: siamo ancora su questo
# percorso.
#
# Va confrontato il PERCORSO e non l'host: dopo il login riuscito si resta su
# `iampe.agenziaentrate.gov.it`, perché anche la schermata successiva (la
# scelta dell'utenza) sta lì. Guardare l'host farebbe scambiare un login
# riuscito per un fallimento.
LOGIN_PATH = "/sam/UI/Login"


# --- Wizard "Scelta utenza" ---------------------------------------------------
#
# Dopo il login si atterra qui: la pagina che chiede se lavori per te stesso o
# come incaricato di qualcun altro. È un wizard a più passi ("Scegli utenza di
# lavoro" -> "Riepilogo e conferma").
#
# Verificati il 2026-07-30 sull'HTML della pagina reale.

# Le due opzioni sono radio con lo stesso name `tipoutenza`.
# Usiamo gli id perché sono univoci; i `value` (`meStesso` / `incaricato`) li
# lasciamo annotati qui perché servono a capire cosa fa cosa leggendo il file.
SELF_RADIO_SELECTOR = "#tipoutenza-0"  # value=meStesso
DELEGATE_RADIO_SELECTOR = "#tipoutenza-1"  # value=incaricato

# Bottone che manda avanti il wizard.
PROCEED_BUTTON_NAME = "Procedi"

# Passo 2 del ramo "Incaricato": la tendina con i soggetti che ti hanno
# delegato. Comparire questa tendina è il segnale che il passo 2 è pronto:
# il wizard cambia i contenuti senza cambiare URL, quindi aspettare il
# caricamento della pagina non serve a niente (verificato: si finiva a
# fotografare ancora il passo 1).
DELEGATOR_SELECT_SELECTOR = "#incaricante"

# Ultimo passo del wizard: "Riepilogo e conferma". Qui il bottone non si
# chiama più "Procedi" ma "Conferma" (verificato: cambia davvero nome).
CONFIRM_BUTTON_NAME = "Conferma"


# --- Home di Fatture e Corrispettivi ------------------------------------------
#
# Verificati il 2026-07-30.

# Il link che porta alla consultazione delle fatture (quella dove si sfogliano
# emesse e ricevute e si scaricano una alla volta).
#
# Da non confondere con "Consultazioni e download massivi" (`/cons/mass-web/`),
# che è un servizio diverso: prepara pacchetti di fatture in modo asincrono.
# Potrebbe tornarci utile più avanti se lo scaricamento uno-per-uno risultasse
# troppo lento.
CONSULTATION_LINK_NAME = "Fatture elettroniche e altri dati IVA"

# Come si riconosce di essere arrivati nella consultazione. Confrontiamo un
# pezzo di percorso e non l'indirizzo intero perché l'href contiene un
# parametro `?v=<numero>` che cambia a ogni visita.
CONSULTATION_PATH = "/cons/cons-web"


# --- Sezioni della consultazione ----------------------------------------------
#
# La consultazione è una applicazione a pagina singola: le sezioni si
# raggiungono cambiando la parte dopo il `#`, senza mai ricaricare la pagina.
# Verificati il 2026-07-30.
#
# Nota: esiste anche "#/fatture/mc" ("Le tue FE passive messe a disposizione"),
# che NON coincide con le fatture ricevute: sono le passive conservate ma non
# in consultazione piena. Se un domani mancano delle fatture di acquisto, è lì
# che vanno cercate. Per ora resta fuori.
ISSUED_LINK_NAME = "Le tue fatture emesse"
ISSUED_ROUTE = "#/fatture/emesse"

RECEIVED_LINK_NAME = "Le tue fatture ricevute"
RECEIVED_ROUTE = "#/fatture/ricevute"


# --- Elenco delle fatture -----------------------------------------------------
#
# La pagina è scritta in AngularJS: la tabella viene riempita dal browser dopo
# la risposta del server. Verificati il 2026-07-30.

# Form di ricerca per data di emissione. I due campi sono di tipo testo con
# formato gg/mm/aaaa (non sono `input[type=date]`, quindi si compilano come
# testo normale).
DATE_FROM_SELECTOR = "#dal"
DATE_TO_SELECTOR = "#al"
SEARCH_BUTTON_NAME = "Cerca"

# L'intestazione dove il portale dichiara cosa ha cercato, per esempio
# "Fatture individuate (238) nel periodo 01/07/2026 - 22/09/2026". È l'unico
# posto in cui il sito dice quale periodo ha usato davvero: serve a non
# lavorare in silenzio su un periodo diverso da quello chiesto.
SEARCHED_PERIOD_TEXT = "Fatture individuate"

# Quanto aspettare quell'intestazione dopo aver cliccato "Cerca". Breve: è una
# riga informativa, non deve rallentare il giro se il portale cambia il testo.
SEARCH_HEADING_TIMEOUT_MS = 10_000

# Quanto aspettare che il dettaglio si apra dopo il click. Corto di proposito:
# se il click ha funzionato l'indirizzo cambia subito, e aspettare di più
# significherebbe solo ritardare il secondo tentativo.
DETAIL_OPEN_TIMEOUT_MS = 5_000

# Righe della tabella dei risultati. La prima cella di ogni riga è un <th>,
# non un <td>: leggendo solo i <td> si perderebbe una colonna e tutte le altre
# risulterebbero spostate di uno.
INVOICE_ROW_SELECTOR = "table tbody tr"
INVOICE_CELL_SELECTOR = "th, td"

# Posizione delle colonne che ci servono, nell'ordine in cui il sito le mostra.
# Se un giorno il sito aggiunge una colonna, è qui che si aggiusta.
#
# Rimappate il 2026-09-22 dopo la riscrittura del portale: tutte scalate di
# uno, perché la colonna che apre la riga non è più la stessa. La mappa non è
# dedotta, è letta dalle intestazioni della tabella vera:
#
#   [0] Tipo fattura / Tipo documento      [5] Imposta
#   [1] Numero fattura / Documento         [6] Sdi / file
#   [2] Data emissione                     [7] Fatture consegnate e data consegna
#   [3] Identificativo cliente             [8] Bollo virtuale
#   [4] Imponibile / Importo               [9] Dettaglio Fattura
COLUMN_NUMBER = 1  # Numero fattura / Documento
COLUMN_DATE = 2  # Data emissione
COLUMN_CLIENT = 3  # Identificativo cliente (P.IVA/CF + denominazione)
COLUMN_TAXABLE = 4  # Imponibile / Importo
COLUMN_TAX = 5  # Imposta
COLUMN_SDI = 6  # Sdi / file

# Dentro la cella del cliente ci sono due span: uno visibile con la sola
# P.IVA, e uno per screen reader con "P.IVA - Denominazione". Leggiamo il
# secondo, che è l'unico a contenere anche il nome.
CLIENT_FULL_TEXT_SELECTOR = ".sr-only"

# Come si apre il dettaglio di una fattura, dal 2026-09-22.
#
# Prima era un link e bastava il suo indirizzo. Adesso nella riga c'è un
# BOTTONE, e nella pagina non esiste più nessun href con `/fatture/dettaglio/`:
# per questo `detail_id` risultava vuoto e i file di diagnostica finivano tutti
# in `dettaglio_senza_bottone_.html`, sovrascrivendosi a vicenda.
#
# Il nome accessibile del bottone è "Dettaglio Fattura <identificativo>", quindi
# serve a due cose: leggere l'identificativo dalla riga, e ritrovare il bottone
# giusto quando è il momento di cliccarlo.
DETAIL_BUTTON_PREFIX = "Dettaglio Fattura"

# Paginazione: i risultati arrivano a blocchi. Ignorarla significherebbe
# scaricare solo le prime fatture credendo di averle prese tutte.
#
# Rifatta il 2026-09-22. Le differenze con la versione vecchia sono minime da
# leggere e letali da ignorare: due sono solo maiuscole ("elenco" -> "Elenco",
# "successiva" -> "Successiva"), e in un selettore per attributo le maiuscole
# contano. Il risultato era che il `nav` non veniva trovato, la lettura si
# fermava alla prima pagina e il programma NON lo diceva: 50 fatture su 238.
#
# La pagina contiene DUE nav di paginazione, uno per schermo grande e uno per
# schermo piccolo, e solo uno dei due è a schermo. Il `:visible` serve a non
# cliccare quello nascosto.
PAGINATION_SELECTOR = "nav[aria-label='Paginazione Elenco fatture']:visible"

# Ora sono <button class="page-link">, non più link. I nomi sono quelli
# letti dall'HTML: "Prima Pagina", "Pagina Precedente", "Pagina 1", ...,
# "Pagina Successiva", "Ultima Pagina".
NEXT_PAGE_NAME = "Pagina Successiva"
FIRST_PAGE_NAME = "Prima Pagina"


# --- Dettaglio della singola fattura ------------------------------------------
#
# Verificati il 2026-07-30 sull'HTML della pagina di dettaglio.

# NON USATA dal 2026-09-22, tenuta solo come promemoria.
#
# Nella SPA nuova il dettaglio non è raggiungibile per indirizzo: andando a
# `/cons/cons-web/fatture/dettaglio/<id>` si viene rimbalzati sull'elenco.
# Si apre solo cliccando il bottone nella riga (DETAIL_BUTTON_PREFIX).
DETAIL_ROUTE = "#/fatture/dettaglio/"

# Bottone che salva il file della fattura. La pagina stessa spiega che questo
# scarica "il file della fattura (compresi eventuali allegati)", mentre
# "Visualizza file fattura" apre solo un'anteprima e "Download meta-dati" dà
# altro.
#
# Non esiste un indirizzo da chiamare: il file è generato da una funzione
# JavaScript (`vm.downloadFileFattura(...)`). Per questo il download passa dal
# browser e non da un client HTTP.
DOWNLOAD_BUTTON_NAME = "Download file fattura"

# Avviso che compare AL POSTO del bottone quando l'XML non è ancora pronto:
# "Il file è in corso di predisposizione. Sarà disponibile entro 72 ore dalla
# data di consegna."
#
# Verificato il 2026-09-01 sull'HTML di una fattura consegnata lo stesso
# giorno (`data/dettaglio_senza_bottone_*.html`). Nel template il bottone sta
# nel ramo `fileDownload===1` e questo avviso nel ramo `fileDownload===2`: o
# c'è l'uno o c'è l'altro, mai entrambi.
#
# Il testo è quello del portale e possono cambiarlo quando vogliono. Se
# cambia, questo selettore smette di agganciare e si torna al comportamento
# vecchio: un minuto di attesa e l'HTML salvato in `data/`. Si degrada, non
# si rompe.
FILE_PENDING_SELECTOR = "p.alert-warning:has-text('in corso di predisposizione')"

# Cartella dove finiscono gli XML: `fatture/` nella radice del progetto, divisa
# in `fatture/emesse` e `fatture/ricevute`. Viene creata al primo download.
#
# È in .gitignore: gli XML sono fatture reali e non devono finire su git.
INVOICES_DIR = "fatture"

# Nomi delle variabili nel file .env. Stanno qui perché sono un contratto fra
# `.env.example` e il codice: se cambia uno deve cambiare l'altro.
ENV_USERNAME = "ADE_USERNAME"
ENV_PASSWORD = "ADE_PASSWORD"
ENV_PIN = "ADE_PIN"

# Codice fiscale/P.IVA del soggetto per cui si lavora. Serve soprattutto via
# cron: permette di scegliere il delegante senza fare domande a nessuno.
ENV_FISCAL_CODE = "ADE_CODICE_FISCALE"

# Permette di spostare altrove la cartella `fatture/` (utile sul server del
# cron). Se non è valorizzata si usa `fatture/` nella radice del progetto.
ENV_INVOICES_DIR = "EXTR_ADE_INVOICES_DIR"
