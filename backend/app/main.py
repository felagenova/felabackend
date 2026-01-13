from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from dotenv import load_dotenv
import sib_api_v3_sdk # Per le email
from sib_api_v3_sdk.rest import ApiException # Importa la classe di eccezione corretta
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

# Import per Notifiche Push e Scheduler
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler
import json

# Carica le variabili d'ambiente dal file .env all'inizio di tutto
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# Definisci la costante mancante
MAX_EXPORT_RECORDS = 1000
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "https://felabackend.onrender.com") # URL base del backend

# Configure Sendinblue (Brevo) API client
SENDINBLUE_API_KEY = os.getenv("SENDINBLUE_API_KEY")

if not SENDINBLUE_API_KEY:
    print("WARNING: SENDINBLUE_API_KEY not found. Email sending will be disabled.")
    sendinblue_api_client = None
    transactional_emails_api = None
else:
    # Aggiungi un log per verificare che la chiave sia stata caricata correttamente
    print(f"Sendinblue API Key loaded successfully (ends with: ...{SENDINBLUE_API_KEY[-4:]}).")
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = SENDINBLUE_API_KEY
    sendinblue_api_client = sib_api_v3_sdk.ApiClient(configuration)
    transactional_emails_api = sib_api_v3_sdk.TransactionalEmailsApi(sendinblue_api_client)

# Configurazione VAPID per Web Push
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_MAILTO = os.getenv("VAPID_MAILTO", "mailto:admin@example.com")

if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
    print("WARNING: VAPID keys not found. Push notifications will be disabled.")

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
        
        # Avvia lo scheduler per le notifiche programmate
        scheduler.start()
        print("Scheduler started.")
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

    # 1. Prova a caricare le credenziali dalla variabile d'ambiente (per Render)
    google_creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
    
    if google_creds_json:
        print("Caricamento credenziali Google Sheets da variabile d'ambiente...")
        creds_dict = json.loads(google_creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPE)
    else:
        # 2. Fallback al file locale (per sviluppo)
        creds_path = os.path.join(os.path.dirname(__file__), '..', 'credentials.json')
        if not os.path.exists(creds_path):
            raise FileNotFoundError("File credentials.json non trovato e variabile GOOGLE_SHEETS_CREDENTIALS non impostata.")
        
        print("Caricamento credenziali Google Sheets da file locale...")
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPE)

    client = gspread.authorize(creds)
    SHEET_ID = "1GnbUbfP666Gpzvbh9fzStaiakMm4qRoSrClfF4LW6Ck"
    spreadsheet = client.open_by_key(SHEET_ID)
    mailing_list_sheet = spreadsheet.worksheet("Foglio1") # Assicurati che il nome del foglio sia "Foglio1"
    print("Google Sheets client initialized successfully.")
except FileNotFoundError as e:
    print(f"ERRORE CONFIGURAZIONE: {e}. La funzionalità di mailing list non sarà attiva.")
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
    "https://felagenova.github.io", # Il tuo sito di produzione
    "http://127.0.0.1:5502",  # L'indirizzo del tuo Live Server per i test locali
    "http://localhost:5502",   # Aggiunto per maggiore compatibilità
    # Durante lo sviluppo, può essere utile consentire tutte le origini.
    # Rimuovi o commenta questa riga in produzione se vuoi una sicurezza più stretta.
    "*"
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

# --- SISTEMA DI NOTIFICHE PUSH ---

scheduler = BackgroundScheduler()

def send_web_push(subscription_info, message_body):
    """Funzione helper per inviare una notifica push."""
    if not VAPID_PRIVATE_KEY:
        return
    
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(message_body),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_MAILTO}
        )
    except WebPushException as ex:
        print(f"Web Push Failed: {ex}")
        # Se l'endpoint non è più valido (410), bisognerebbe rimuoverlo dal DB, 
        # ma per semplicità qui logghiamo solo l'errore.
    except Exception as e:
        print(f"Generic Push Error: {e}")

def broadcast_notification(message_body, db: Session):
    """Invia una notifica a tutti gli iscritti generali."""
    subscriptions = db.query(models.PushSubscription).all()
    print(f"Broadcasting notification to {len(subscriptions)} subscribers...")
    for sub in subscriptions:
        # Ricostruisce l'oggetto subscription nel formato richiesto da pywebpush
        sub_info = {
            "endpoint": sub.endpoint,
            "keys": sub.keys
        }
        send_web_push(sub_info, message_body)

# --- JOB SCHEDULATI ---

def scheduled_monday_notification():
    """Invia notifica ogni lunedì alle 18:00."""
    print("Running scheduled job: Monday Program Notification")
    # Dobbiamo creare una nuova sessione DB perché siamo in un thread diverso
    db = database.SessionLocal()
    try:
        message = {
            "title": "Nuovo Programma Settimanale!",
            "body": "Il programma della settimana è uscito su Instagram. Corri a vederlo!",
            "url": "https://www.instagram.com/felamusicbar/" # Link alla pagina IG
        }
        broadcast_notification(message, db)
    finally:
        db.close()

def scheduled_booking_reminder():
    """Invia promemoria alle 10:30 per gli eventi di oggi."""
    print("Running scheduled job: Daily Booking Reminder")
    db = database.SessionLocal()
    try:
        today = date.today()
        # Trova tutte le prenotazioni per oggi che hanno una sottoscrizione push salvata
        bookings_today = db.query(models.Booking).filter(
            models.Booking.booking_date == today,
            models.Booking.push_subscription.isnot(None)
        ).all()

        for booking in bookings_today:
            # Determina il nome dell'evento
            event_name = "il tuo evento"
            if booking.event:
                event_name = booking.event.display_name
            elif booking.booking_time in [time(12, 0), time(13, 30)]:
                event_name = "il Brunch"
            
            message = {
                "title": "Promemoria Prenotazione Fela!",
                "body": f"Ciao {booking.name}, ti ricordiamo la tua prenotazione per {event_name} oggi alle {booking.booking_time.strftime('%H:%M')}.",
                "url": "https://felagenova.github.io"
            }
            
            # Invia la notifica specifica a questo utente
            send_web_push(booking.push_subscription, message)
            
    finally:
        db.close()

# Aggiungi i job allo scheduler
# Lunedì alle 18:00
scheduler.add_job(scheduled_monday_notification, 'cron', day_of_week='mon', hour=18, minute=0)
# Tutti i giorni alle 10:30
scheduler.add_job(scheduled_booking_reminder, 'cron', hour=10, minute=30)


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()
    print("Scheduler shut down.")

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

# --- NUOVO: Endpoint per l'iscrizione alle Notifiche Push Generali ---
@app.post("/api/push-subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe_to_push(subscription: schemas.PushSubscriptionCreate, db: Session = Depends(get_db)):
    """
    Salva una sottoscrizione push per le notifiche generali (broadcast).
    """
    # Controlla se esiste già
    existing_sub = db.query(models.PushSubscription).filter(models.PushSubscription.endpoint == subscription.endpoint).first()
    if existing_sub:
        # Aggiorna le chiavi se necessario
        existing_sub.keys = subscription.keys
        db.commit()
        return {"message": "Sottoscrizione aggiornata."}
    
    new_sub = models.PushSubscription(endpoint=subscription.endpoint, keys=subscription.keys)
    db.add(new_sub)
    db.commit()
    return {"message": "Iscrizione alle notifiche avvenuta con successo!"}


def send_booking_confirmation_email(
    recipient_email: str,
    booking_summary: dict,
    cancellation_link: str
):
    """
    Invia un'email di conferma prenotazione con i dettagli e un link di cancellazione.
    """
    if not transactional_emails_api:
        print(f"Email sending disabled. Not sending confirmation to {recipient_email}.")
        return

    sender_email = os.getenv("SENDER_EMAIL", "fela.booker@gmail.com") # Get sender email from env or use default
    sender_name = os.getenv("SENDER_NAME", "Fela! Music Bar")

    subject = "Conferma Prenotazione Fela! Music Bar"

    # Costruisci il contenuto HTML per l'email
    html_content = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                background-color: #f3f0ce;
                color: #1a1a1a;
                font-family: 'Red Hat Display', sans-serif;
                line-height: 1.6;
                margin: 0;
                padding: 0;
            }}
            .container {{
                max-width: 600px;
                margin: 20px auto;
                padding: 20px;
                border-radius: 8px;
                background-color: #f3f0ce;
            }}
            h2 {{
                color: #ff0403;
                font-weight: 700;
            }}
            .button {{ display: inline-block; background-color: #D9534F; color: white !important; padding: 10px 20px; text-decoration: none; border-radius: 5px; margin-top: 20px; }}
            .footer {{ margin-top: 30px; font-size: 0.9em; color: #777; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Grazie per la tua prenotazione da Fela!</h2>
            <p>Ciao {booking_summary['name']},</p>
            <p>La tua prenotazione è stata confermata con successo. Ecco un riepilogo dei dettagli:</p>
            <div class="summary-item"><strong>Evento:</strong> {booking_summary['event_name']}</div>
            <div class="summary-item"><strong>Data:</strong> {booking_summary['booking_date']}</div>
            <div class="summary-item"><strong>Ora:</strong> {booking_summary['booking_time']}</div>
            <div class="summary-item"><strong>Ospiti:</strong> {booking_summary['guests']}</div>
            <div class="summary-item"><strong>Note:</strong> {booking_summary['notes'] if booking_summary['notes'] else 'Nessuna'}</div>
            
            <p>Se hai bisogno di cancellare la tua prenotazione, puoi farlo cliccando sul link qui sotto:</p>
            <a href="{cancellation_link}" class="button">Cancella la mia prenotazione</a>
            
            <div class="footer">
                <p>Ti aspettiamo al Fela! Music Bar.</p>
                <p>Via di S. Cosimo, 6r, 16128 Genova GE</p>
            </div>
        </div>
    </body>
    </html>
    """

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        sender={"name": sender_name, "email": sender_email},
        to=[{"email": recipient_email}],
        subject=subject,
        html_content=html_content
    )

    try:
        api_response = transactional_emails_api.send_transac_email(send_smtp_email)
        print(f"Email di conferma inviata a {recipient_email}: {api_response}")
    except ApiException as e:
        print(f"Errore durante l'invio dell'email a {recipient_email}: {e}")
    except Exception as e:
        print(f"Errore generico durante l'invio dell'email a {recipient_email}: {e}")

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
        # Usa l'orario dell'evento se presente, altrimenti un orario di default per l'ordinamento
        event_time_for_sort = event.get('booking_time') or '00:00:00'
        
        # Se ci sono turni, usa il primo per l'ordinamento per raggruppare correttamente
        if event.get('available_slots'):
            event_time_for_sort = event['available_slots'][0]
            
        return (event['booking_date'], event_time_for_sort)
    events.sort(key=sort_key)
    return events

# --- CRUD per gli eventi speciali (protetti da autenticazione admin) ---
@app.post("/api/admin/special-events", response_model=schemas.SpecialEvent)
async def create_special_event(
    event: schemas.SpecialEventCreate,
    admin: HTTPBasicCredentials = Depends(get_current_admin),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks() # Aggiunto per le notifiche
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

    # --- NOTIFICA PUSH: NUOVO EVENTO ---
    # Invia una notifica a tutti gli iscritti
    message = {
        "title": "Nuovo Evento da Fela!",
        "body": f"È stato annunciato un nuovo evento: {db_event.display_name} il {db_event.booking_date.strftime('%d/%m')}. Prenota ora!",
        "url": "https://felagenova.github.io"
    }
    background_tasks.add_task(broadcast_notification, message, db)

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
    
    current_max_guests = MAX_GUESTS

    # 1. Se è un EVENTO SPECIALE, usiamo la sua logica specifica
    if booking.event_id:
        event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == booking.event_id).first()
        # Se l'evento ha un limite specifico impostato, usiamo quello
        if event and event.max_guests is not None:
            current_max_guests = event.max_guests
            
        # Contiamo solo le prenotazioni per questo specifico evento
        booked_guests = db.query(func.sum(models.Booking.guests)).filter(
            models.Booking.event_id == booking.event_id
        ).scalar() or 0
        
        error_context = "per questo evento"
        
        # --- NUOVO: Controllo Capienza Specifica del Turno ---
        # Se l'evento ha capacità specifiche per i turni e la prenotazione ha un orario
        if event.slot_capacities and booking.booking_time:
            time_str = booking.booking_time.strftime('%H:%M')
            # Controlla se esiste un limite per questo specifico orario
            if time_str in event.slot_capacities:
                slot_max = event.slot_capacities[time_str]
                
                # Conta quanti ospiti ci sono GIÀ per questo specifico turno
                slot_booked = db.query(func.sum(models.Booking.guests)).filter(
                    models.Booking.event_id == booking.event_id,
                    models.Booking.booking_time == booking.booking_time
                ).scalar() or 0
                
                if slot_booked + booking.guests > slot_max:
                    raise HTTPException(status_code=400, detail=f"Spiacenti, non c'è abbastanza posto per il turno delle {time_str}. Posti rimasti nel turno: {slot_max - slot_booked}.")

    # 2. Se è un BRUNCH (Standard), controlla la capienza del turno
    elif booking.booking_time in BRUNCH_SLOTS:
        booked_guests = db.query(func.sum(models.Booking.guests)).filter(
            models.Booking.booking_date == booking.booking_date,
            models.Booking.booking_time == booking.booking_time
        ).scalar() or 0
        
        error_context = f"per il turno delle {booking.booking_time.strftime('%H:%M')}"
        
    # 3. Altrimenti, SERATA STANDARD (esclude il brunch)
    else:
        booked_guests = db.query(func.sum(models.Booking.guests)).filter(
            models.Booking.booking_date == booking.booking_date,
            ~models.Booking.booking_time.in_(BRUNCH_SLOTS) # Esclude i turni del brunch dal conteggio
        ).scalar() or 0
        
        error_context = "per la serata"

    # Calcola i posti totali se questa prenotazione venisse accettata
    total_guests_if_booked = booked_guests + booking.guests

    # Se si supera la capienza, restituisci un errore specifico.
    if total_guests_if_booked > current_max_guests:
        available_slots = current_max_guests - booked_guests
        error_message = f"Spiacenti, non c'è abbastanza posto {error_context}. Posti rimasti: {available_slots}."
        if available_slots <= 0:
            error_message = f"Spiacenti, siamo al completo {error_context}."
        raise HTTPException(status_code=400, detail=error_message)

    # Creiamo la prenotazione includendo l'event_id se presente
    db_booking = models.Booking(**booking.model_dump()) # booking.model_dump() include già event_id


    db.add(db_booking)
    db.commit()
    db.refresh(db_booking)

    # --- Prepara e invia l'email di conferma in background ---
    # Costruisci il link di cancellazione usando l'URL base del backend
    cancellation_url = f"{BACKEND_BASE_URL}/api/bookings/cancel/{db_booking.cancellation_token}"

    # Prepara il riepilogo della prenotazione
    event_name = "Brunch" # Default per brunch
    if db_booking.event_id:
        event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == db_booking.event_id).first()
        if event:
            event_name = event.display_name

    booking_summary = {
        "name": db_booking.name,
        "event_name": event_name,
        "booking_date": db_booking.booking_date.strftime('%d/%m/%Y'),
        "booking_time": db_booking.booking_time.strftime('%H:%M') if db_booking.booking_time else "N/D",
        "guests": db_booking.guests,
        "notes": db_booking.notes
    }

    background_tasks.add_task(send_booking_confirmation_email, recipient_email=db_booking.email, booking_summary=booking_summary, cancellation_link=cancellation_url)

    # --- NOTIFICA PUSH: CONFERMA PRENOTAZIONE ---
    # Se la prenotazione include i dati di sottoscrizione push, invia una notifica immediata
    if booking.push_subscription:
        push_message = {
            "title": "Prenotazione Confermata!",
            "body": f"Grazie {booking.name}, la tua prenotazione per il {booking.booking_date.strftime('%d/%m')} è confermata.",
            "url": cancellation_url # Cliccando si va ai dettagli/cancellazione
        }
        background_tasks.add_task(send_web_push, booking.push_subscription, push_message)

    # Restituisce un messaggio di successo che include l'informazione sull'email
    return {"message": "Prenotazione effettuata. Riceverai una email di conferma."}

# New: Endpoint to handle booking cancellation
@app.get("/api/bookings/cancel/{token}", status_code=status.HTTP_303_SEE_OTHER)
def cancel_booking(token: str, db: Session = Depends(get_db)) -> RedirectResponse:
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
    
    # Reindirizza l'utente a una pagina di conferma sul frontend
    cancellation_confirmation_url = "https://felagenova.github.io/Fela-/cancellazione.html"
    return RedirectResponse(url=cancellation_confirmation_url)

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
