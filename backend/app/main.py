from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from dotenv import load_dotenv
import sib_api_v3_sdk # Per le email
from datetime import time, date, timedelta, datetime # Aggiunto datetime
from io import BytesIO
from reportlab.pdfgen import canvas
from typing import Optional, List
import os
from uuid import UUID

# Import per Google Sheets
import gspread
from google.oauth2.service_account import Credentials
from pydantic import EmailStr

# Carica le variabili d'ambiente dal file .env all'inizio di tutto
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# Definisci la costante mancante
MAX_EXPORT_RECORDS = 1000

from . import models, schemas, database
from .database import engine, get_db

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    print("FastAPI application starting up...")
    try:
        # Crea le tabelle nel database (se non esistono) all'avvio dell'applicazione
        models.Base.metadata.create_all(bind=engine)
        print("Database tables checked/created successfully.")
    except Exception as e:
        print(f"ERROR during database startup: {e}")
        # Rilancia l'eccezione per impedire l'avvio dell'app con un DB non funzionante
        raise

# --- NUOVO: Configurazione per Google Sheets ---
try:
    SCOPE = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file"
    ]
    CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), '..', 'credentials.json')
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPE)
    client = gspread.authorize(creds)
    SHEET_ID = "1GnbUbfP666Gpzvbh9fzStaiakMm4qRoSrClfF4LW6Ck"
    spreadsheet = client.open_by_key(SHEET_ID)
    mailing_list_sheet = spreadsheet.worksheet("Foglio1") # Assicurati che il nome del foglio sia "Foglio1"
    print("Google Sheets client initialized successfully.")
except FileNotFoundError:
    print("ERRORE: File 'credentials.json' non trovato. La funzionalità di mailing list non sarà attiva.")
    mailing_list_sheet = None
except Exception as e:
    print(f"ERRORE durante l'inizializzazione di Google Sheets: {e}")
    mailing_list_sheet = None

# --- NUOVO: Modello Pydantic per la richiesta di iscrizione ---
class MailingListSignup(schemas.BaseModel):
    email: EmailStr


# New: Security for admin page
security = HTTPBasic()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") # Get admin password from .env

# Funzione di dipendenza per la sicurezza dell'admin
def get_current_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Dipendenza per verificare le credenziali dell'amministratore.
    Centralizza la logica di autenticazione.
    """
    correct_username = credentials.username == "admin"
    # Usa una funzione di confronto sicura per prevenire attacchi di timing
    correct_password = ADMIN_PASSWORD is not None and credentials.password == ADMIN_PASSWORD
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Credenziali non valide", headers={"WWW-Authenticate": "Basic"})
    return credentials

# Lista degli URL autorizzati a fare richieste al nostro backend.
# È fondamentale per la sicurezza e per risolvere gli errori CORS.
origins = [
    "http://127.0.0.1:5502",  # L'indirizzo del tuo Live Server per i test locali
    "http://localhost:5502",   # Aggiunto per maggiore compatibilità
    # Lista completa per GitHub Pages per massima compatibilità
    "https://felagenova.github.io",
    "https://felagenova.github.io/",
]

# Aggiungiamo il middleware CORS all'applicazione FastAPI.
# Questo "insegna" al backend ad accettare le richieste dal frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,       # Consente le richieste solo dagli URL nella lista `origins`
    allow_credentials=True,      # Permette l'invio di cookie/credenziali
    allow_methods=["*"],         # Permette tutti i metodi HTTP (GET, POST, PUT, etc.)
    allow_headers=["*"],         # Permette tutte le intestazioni HTTP
    expose_headers=["Content-Disposition"], # Permette al JS di leggere l'header per il nome del file
)

# --- NUOVO: Endpoint per l'iscrizione alla Mailing List ---
@app.post("/api/mailing-list-signup", status_code=status.HTTP_201_CREATED)
async def signup_to_mailing_list(signup_data: MailingListSignup):
    """
    Aggiunge un'email alla mailing list su Google Sheets.
    """
    if not mailing_list_sheet:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Il servizio di mailing list non è al momento disponibile a causa di un errore di configurazione."
        )

    try:
        # Controlla se l'email è già presente
        # Usiamo .get_all_values() per essere sicuri di leggere tutto e gestiamo il caso di foglio vuoto
        all_records = mailing_list_sheet.get_all_values()
        existing_emails = [row[0] for row in all_records if row] # Estrae solo la prima colonna (email)

        if signup_data.email in existing_emails:
            # Restituisce un codice 200 OK con un messaggio specifico per l'utente già iscritto
            return {"message": "Email già iscritta!"}

        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        new_row = [signup_data.email, timestamp]
        mailing_list_sheet.append_row(new_row)
        return {"message": "Iscrizione alla mailing list avvenuta con successo!"}
    except Exception as e:
        print(f"Errore durante la scrittura su Google Sheets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Impossibile completare l'iscrizione alla mailing list in questo momento."
        )

@app.get("/api/bookable-events")
def get_bookable_events(db: Session = Depends(get_db)):
    """
    Restituisce una lista di eventi per cui è possibile prenotare.
    Include i brunch delle prossime domeniche e gli eventi speciali.
    """
    events = []
    today = date.today()

    # --- Eventi Speciali (recuperati dal DB) ---
    # Ora gli eventi speciali vengono recuperati dal database
    db_special_events = db.query(models.SpecialEvent).filter(models.SpecialEvent.is_closed == False).with_session(db).all()
    for event in db_special_events:
        if event.booking_date >= today:
            # Logica unificata per tutti gli eventi
            events.append({
                "type": "special",
                "id": event.id,
                "display_name": f"{event.display_name} - {event.booking_date.strftime('%d/%m')}",
                "booking_date": event.booking_date.isoformat(),
                "booking_time": event.booking_time.isoformat() if event.booking_time else None,
                # Standardizza l'output per i turni, assicurando che sia sempre una lista
                "available_slots": event.available_slots if event.available_slots else []
            })


    # Ordina gli eventi per data
    def sort_key(event):
        event_time_str = event.get('booking_time')
        if event_time_str is None and event.get('type') == 'brunch':
            event_time_str = event['available_slots'][0] if event['available_slots'] else '00:00:00'
        elif event_time_str is None:
            event_time_str = '00:00:00' # Default per eventi senza orario

        return (event['booking_date'], event_time_str)
    events.sort(key=sort_key)
    return events

# --- CRUD per gli eventi speciali (protetti da autenticazione admin) ---
@app.post("/api/admin/special-events", response_model=schemas.SpecialEvent)
async def create_special_event(
    event: schemas.SpecialEventCreate,
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Crea un nuovo evento speciale."""
    
    # Converte il modello Pydantic in un dizionario
    event_data = event.model_dump()
    
    # Assicura che i turni (available_slots) siano gestiti correttamente come JSON.
    # SQLAlchemy con il driver giusto (es. psycopg2) gestirà la serializzazione.
    db_event = models.SpecialEvent(**event_data)

    db.add(db_event)
    db.commit()
    db.refresh(db_event)
    return db_event

@app.get("/api/admin/special-events", response_model=List[schemas.SpecialEvent])
async def read_special_events(
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Restituisce tutti gli eventi speciali."""
    
    # Modificato per restituire tutti gli eventi, inclusi quelli chiusi,
    # così l'admin può vederli e gestirli tutti.
    events = db.query(models.SpecialEvent).order_by(
        models.SpecialEvent.booking_date.desc(), models.SpecialEvent.booking_time.desc()
    ).all()
    # È necessario assicurarsi che i dati vengano serializzati correttamente,
    # inclusi i campi JSON come available_slots.
    # Forzare la conversione tramite il modello Pydantic garantisce che tutti i campi siano presenti.
    response_events = [schemas.SpecialEvent.from_orm(event) for event in events]
    return response_events

@app.patch("/api/admin/special-events/{event_id}/toggle-status", response_model=schemas.SpecialEvent)
async def toggle_event_status(
    event_id: int,
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """
    Cambia lo stato di un evento (aperto/chiuso alle prenotazioni).
    """

    event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Evento non trovato")

    event.is_closed = not event.is_closed # Inverte lo stato attuale
    db.commit()
    db.refresh(event)
    return event


@app.delete("/api/admin/special-events/{event_id}", response_model=schemas.SpecialEvent)
async def delete_special_event(
    event_id: int,
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Cancella un evento speciale e tutte le prenotazioni associate."""
    
    event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    
    # Prima di cancellare l'evento, cancella tutte le prenotazioni associate
    db.query(models.Booking).filter(models.Booking.event_id == event_id).delete(synchronize_session=False)
    
    db.delete(event)
    db.commit()
    return event

@app.post("/api/bookings")
async def create_booking(booking: schemas.BookingCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Endpoint per creare una nuova prenotazione.
    Riceve i dati della prenotazione, li salva nel database e restituisce un messaggio di conferma.
    """
    # --- CONTROLLO SICUREZZA: L'EVENTO È APERTO? ---
    if booking.event_id:
        event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == booking.event_id).first()
        if not event:
            raise HTTPException(status_code=404, detail="L'evento specificato non esiste.")
        if event.is_closed:
            raise HTTPException(status_code=400, detail="Spiacenti, le prenotazioni per questo evento sono chiuse.")

    # --- CONTROLLO DUPLICATI MIGLIORATO ---
    # Controlla se esiste già una prenotazione con la stessa email PER LO STESSO EVENTO.
    query = db.query(models.Booking).filter(models.Booking.email == booking.email)

    if booking.event_id:
        # Se è un evento speciale, controlla per event_id
        query = query.filter(models.Booking.event_id == booking.event_id)
    else:
        # Se è un brunch o altro evento, controlla per data e ora
        query = query.filter(models.Booking.booking_date == booking.booking_date, models.Booking.booking_time == booking.booking_time)

    existing_booking = query.first()

    if existing_booking:
        raise HTTPException(status_code=400, detail="Hai già una prenotazione per questo specifico evento con la stessa email.")

    # --- LOGICA DI CONTROLLO POSTI FLESSIBILE ---
    MAX_GUESTS = 25
    BRUNCH_SLOTS = [time(12, 0), time(13, 30)]

    # Se la prenotazione è per un turno del brunch, controlla la capienza di quello specifico turno.
    if booking.booking_time in BRUNCH_SLOTS:
        booked_guests = db.query(func.sum(models.Booking.guests)).filter(
            models.Booking.booking_date == booking.booking_date,
            models.Booking.booking_time == booking.booking_time
        ).scalar() or 0
        
        error_context = f"per il turno delle {booking.booking_time.strftime('%H:%M')}"
        
    # Altrimenti, se è per un evento serale, controlla la capienza totale della giornata (escludendo il brunch).
    else:
        booked_guests = db.query(func.sum(models.Booking.guests)).filter(
            models.Booking.booking_date == booking.booking_date,
            ~models.Booking.booking_time.in_(BRUNCH_SLOTS) # Esclude i turni del brunch dal conteggio
        ).scalar() or 0
        
        error_context = "per la serata"

    # Calcola i posti totali se questa prenotazione venisse accettata
    total_guests_if_booked = booked_guests + booking.guests

    # Se si supera la capienza, restituisci un errore specifico.
    if total_guests_if_booked > MAX_GUESTS:
        available_slots = MAX_GUESTS - booked_guests
        error_message = f"Spiacenti, non c'è abbastanza posto {error_context}. Posti rimasti: {available_slots}."
        if available_slots <= 0:
            error_message = f"Spiacenti, siamo al completo {error_context}."
        raise HTTPException(status_code=400, detail=error_message)

    # Creiamo la prenotazione includendo l'event_id se presente
    db_booking = models.Booking(**booking.model_dump()) # booking.model_dump() include già event_id


    db.add(db_booking)
    db.commit()
    db.refresh(db_booking)

    # Restituisce un semplice messaggio di successo invece dell'oggetto prenotazione
    return {"message": "Prenotazione effettuata"}

# New: Endpoint to handle booking cancellation
@app.get("/api/bookings/cancel/{token}", status_code=status.HTTP_200_OK)
def cancel_booking(token: str, db: Session = Depends(get_db)):
    """
    Endpoint per cancellare una prenotazione tramite un token univoco.
    """
    booking_to_cancel = db.query(models.Booking).filter(models.Booking.cancellation_token == token).first()

    if not booking_to_cancel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token di cancellazione non valido o prenotazione già cancellata."
        )
    
    db.delete(booking_to_cancel)
    db.commit()
    return {"message": "La tua prenotazione è stata cancellata con successo."}

@app.get("/")
def read_root():
    """
    Endpoint di base per verificare che il server sia operativo.
    """
    return {"message": "Benvenuto nel backend di Fela! Il sistema è operativo."}

# New: Endpoint for admin page to view bookings
@app.get("/api/admin/bookings")
async def get_all_bookings(
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db), 
    skip: int = 0, 
    limit: int = 10,
    event_date: Optional[date] = None,
    event_time: Optional[time] = None,
    event_id: Optional[int] = None # Nuovo parametro per filtrare per ID evento speciale
):
    """
    Endpoint protetto da password per visualizzare tutte le prenotazioni.
    Richiede autenticazione Basic.

    - **skip**: Numero di risultati da saltare (per la paginazione). Default: 0.
    - **limit**: Numero massimo di risultati da restituire per pagina. Default: 10.
    - **event_date**: Filtra le prenotazioni per una data specifica.
    - **event_time**: Filtra le prenotazioni per un orario specifico.
    - **event_id**: Filtra le prenotazioni per un evento speciale specifico.

    Esempio di utilizzo:
    /api/admin/bookings?skip=20&limit=10 (mostra i risultati da 20 a 30)
    """
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_PASSWORD non configurata nel backend. Contatta l'amministratore."
        )
    
    query = db.query(models.Booking)

    # Applica i filtri se forniti
    if event_id:
        # Il filtro per event_id è prioritario e più preciso
        query = query.filter(models.Booking.event_id == event_id)
    elif event_date and event_time:
        # Il filtro per data e ora viene usato per i brunch o altri eventi non speciali
        query = query.filter(models.Booking.booking_date == event_date, models.Booking.booking_time == event_time)

    # Calcola il numero totale di prenotazioni (filtrate o meno)
    total_bookings = query.count()

    # Applica ordinamento e paginazione
    bookings = query.order_by(
        models.Booking.booking_date.desc(), models.Booking.booking_time.desc()
    ).offset(skip).limit(limit).all()

    # Restituisce un oggetto contenente il totale e la lista delle prenotazioni
    return {
        "total": total_bookings,
        "bookings": bookings
    }

@app.get("/api/bookings/pdf")
async def export_bookings_to_pdf(
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db),
    event_date: Optional[date] = None,
    event_time: Optional[time] = None,
    event_id: Optional[int] = None,
    limit: int = 1000 # Aggiungi il parametro limit
):
    """
    Endpoint protetto per esportare le prenotazioni in un file PDF.
    Accetta gli stessi parametri di filtro di /api/admin/bookings.
    """

    query = db.query(models.Booking)
    # Determina il titolo del PDF in base ai filtri
    pdf_title = "Lista di tutte le Prenotazioni"
    # Determina il nome del file PDF
    file_name_base = "prenotazioni_fela"

    # Applica gli stessi filtri dell'endpoint get_all_bookings
    if event_id:
        query = query.filter(models.Booking.event_id == event_id)
        # Recupera il nome dell'evento per il titolo
        event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == event_id).first()
        if event:
            # Rimuovi caratteri speciali e spazi per un nome file valido
            sanitized_event_name = "".join(c for c in event.display_name if c.isalnum() or c.isspace()).replace(" ", "_")
            file_name_base = f"prenotazioni_{sanitized_event_name}_{event.booking_date.strftime('%Y-%m-%d')}"
            pdf_title = f"Prenotazioni per: {event.display_name}"

    elif event_date and event_time:
        query = query.filter(models.Booking.booking_date == event_date, models.Booking.booking_time == event_time)
        # Crea un titolo per i brunch
        pdf_title = f"Prenotazioni Brunch del {event_date.strftime('%d/%m/%Y')} ore {event_time.strftime('%H:%M')}"
        file_name_base = f"prenotazioni_brunch_{event_date.strftime('%Y-%m-%d')}_{event_time.strftime('%H-%M')}"

    # Applica ordinamento e poi limita il numero di risultati
    bookings = query.order_by(
        models.Booking.booking_date.asc(),
        models.Booking.booking_time.asc()
    ).limit(limit).all()

    # Crea il PDF in memoria
    buffer = BytesIO()
    p = canvas.Canvas(buffer)

    # Impostazioni del documento
    p.setTitle("Lista Prenotazioni Fela!")
    p.drawString(70, 800, pdf_title) # Usa il titolo dinamico
    p.line(70, 795, 525, 795)

    # Intestazioni della tabella
    headers = ["ID", "Nome", "Email", "Data", "Ora", "Ospiti"]
    x_positions = [50, 80, 200, 350, 420, 480]
    y_position = 770

    for i, header in enumerate(headers):
        p.drawString(x_positions[i], y_position, header)

    y_position -= 20

    # Scrivi i dati delle prenotazioni
    for booking in bookings:
        if y_position < 50: # Se siamo alla fine della pagina, creane una nuova
            p.showPage()
            y_position = 800

        data = [
            str(booking.id),
            booking.name[:20], # Tronca nomi lunghi
            booking.email[:25], # Tronca email lunghe
            booking.booking_date.strftime('%d/%m/%y'),
            booking.booking_time.strftime('%H:%M') if booking.booking_time else "N/D",
            str(booking.guests)
        ]
        for i, item in enumerate(data):
            p.drawString(x_positions[i], y_position, item)
        y_position -= 15

    p.showPage()
    p.save()
    print("PDF generated successfully")

    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={file_name_base}.pdf"})

# Aggiungi questa parte alla fine del file per l'esecuzione locale
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
