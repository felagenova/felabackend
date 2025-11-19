from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from dotenv import load_dotenv
from datetime import time, date, timedelta
from io import BytesIO
from reportlab.pdfgen import canvas
from typing import Optional, List
import os
from uuid import UUID

# Carica le variabili d'ambiente dal file .env all'inizio di tutto
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# Definisci la costante mancante
MAX_EXPORT_RECORDS = 1000

from . import models, schemas
from .database import engine, get_db

# Crea le tabelle nel database (se non esistono) all'avvio dell'applicazione
models.Base.metadata.create_all(bind=engine)

app = FastAPI()

# New: Security for admin page
security = HTTPBasic()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") # Get admin password from .env

# Lista degli URL autorizzati a fare richieste al nostro backend.
# È fondamentale per la sicurezza e per risolvere gli errori CORS.
origins = [
    # MODIFICA DI DEBUG: Apriamo temporaneamente a tutte le origini per diagnosticare il problema.
    # Se funziona, il problema è la lista di URL. Se non funziona, il backend sta crashando.
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

@app.get("/api/bookable-events")
def get_bookable_events(db: Session = Depends(get_db)):
    """
    Restituisce una lista di eventi per cui è possibile prenotare.
    Include i brunch delle prossime domeniche e gli eventi speciali.
    """
    events = []
    today = date.today()

    # --- Eventi Speciali (da aggiornare manualmente o spostare su DB in futuro) ---
    # Ora gli eventi speciali vengono recuperati dal database
    db_special_events = db.query(models.SpecialEvent).with_session(db).all()
    for event in db_special_events:
        if event.booking_date >= today:
            events.append({
                "type": "special",
                "id": event.id, # ID dell'evento speciale dal DB
                "display_name": f"{event.display_name} - {event.booking_date.strftime('%d/%m')}",
                "booking_date": event.booking_date.isoformat(),
                "booking_time": event.booking_time.isoformat() if event.booking_time else None,
            })


    # --- Brunch Domenicali (genera per le prossime 8 domeniche) ---
    # Troviamo le prossime 8 domeniche
    sundays_found = 0
    day_offset = 0
    while sundays_found < 8:
        check_date = today + timedelta(days=day_offset)
        if check_date.weekday() == 6: # 6 = Domenica
            events.append({
                "type": "brunch",
                "display_name": f"Brunch - {check_date.strftime('%d/%m')}",
                "booking_date": check_date.isoformat(),
                "available_slots": [time(12, 0).isoformat(), time(13, 30).isoformat()],
                "id": None # I brunch generati non hanno un ID di evento speciale
            })
            sundays_found += 1
        day_offset += 1

    # Ordina gli eventi per data
    def sort_key(event):
        # Per gli eventi speciali, booking_time è una stringa. Per il brunch, usiamo il primo slot disponibile.
        # Se booking_time è None (per eventi senza orario specifico), usiamo un default per l'ordinamento.
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
    credentials: HTTPBasicCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Crea un nuovo evento speciale."""
    if credentials.username != "admin" or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Credenziali non valide", headers={"WWW-Authenticate": "Basic"})
    
    db_event = models.SpecialEvent(**event.model_dump())
    db.add(db_event)
    db.commit()
    db.refresh(db_event)
    return db_event

@app.get("/api/admin/special-events", response_model=List[schemas.SpecialEvent])
async def read_special_events(
    credentials: HTTPBasicCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Restituisce tutti gli eventi speciali."""
    if credentials.username != "admin" or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Credenziali non valide", headers={"WWW-Authenticate": "Basic"})
    
    events = db.query(models.SpecialEvent).order_by(models.SpecialEvent.booking_date, models.SpecialEvent.booking_time).all()
    return events

@app.delete("/api/admin/special-events/{event_id}", response_model=schemas.SpecialEvent)
async def delete_special_event(
    event_id: int,
    credentials: HTTPBasicCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Cancella un evento speciale e tutte le prenotazioni associate."""
    if credentials.username != "admin" or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Credenziali non valide", headers={"WWW-Authenticate": "Basic"})
    
    event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    
    # Prima di cancellare l'evento, cancella tutte le prenotazioni associate
    db.query(models.Booking).filter(models.Booking.event_id == event_id).delete(synchronize_session=False)
    
    db.delete(event)
    db.commit()
    return event

async def send_email_confirmation(email: str, booking_details: dict):
    """
    Prepara e invia l'email di conferma.
    Questa funzione ora è completamente autonoma per evitare problemi di stato su Render.
    """
    # Ricrea la configurazione della mail e la connessione al DB all'interno del task
    # per garantire che sia thread-safe e non causi crash.
    conf_local = ConnectionConfig(
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
        MAIL_FROM=os.getenv("MAIL_FROM"),
        MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
        MAIL_SERVER=os.getenv("MAIL_SERVER"),
        MAIL_STARTTLS=True,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=True
    )
    db = next(get_db())

    # Formattiamo la data e l'ora per una migliore leggibilità
    booking_date_formatted = booking_details['booking_date'].strftime('%d/%m/%Y')
    booking_time_formatted = booking_details['booking_time'].strftime('%H:%M') if booking_details['booking_time'] else "N/D"

    # Determina il nome dell'evento da mostrare nella mail
    event_name = ""
    if booking_details.get("event_id"):
        special_event = db.query(models.SpecialEvent).filter(models.SpecialEvent.id == booking_details["event_id"]).first()
        if special_event:
            event_name = special_event.display_name
    else:
        # Se non è un evento speciale, è un brunch
        event_name = f"Brunch del {booking_date_formatted}"

    # HTML per la riga dell'evento, da inserire solo se l'evento ha un nome
    event_row_html = ""
    if event_name:
        event_row_html = f"""
        <tr style="border-bottom: 1px solid #eee;">
            <td style="padding: 10px 0; font-size: 16px;"><strong>Evento:</strong></td>
            <td style="padding: 10px 0; font-size: 16px; text-align: right;">{event_name}</td>
        </tr>
        """
    
    # Costruiamo il link di cancellazione
    # Il token viene convertito in stringa per essere usato nell'URL
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5502") # Usa la variabile o un default
    cancellation_token_str = str(booking_details['cancellation_token'])
    cancellation_link = f"{frontend_url}/cancellazione.html?token={cancellation_token_str}"

    html_body = f"""
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Conferma Prenotazione - Fela! Music Bar</title>
    </head>
    <body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #f3f0ce; color: #333;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; margin: 20px auto; border-collapse: collapse; background-color: #ffffff; border: 1px solid #ddd;">
            <tr>
                <td align="center" style="padding: 20px 0; background-color: #ff0403;">
                    <h1 style="color: #f3f0ce; margin: 0; font-family: 'Red Hat Display', sans-serif;">Fela! Music Bar</h1>
                </td>
            </tr>
            <tr>
                <td style="padding: 40px 30px;">
                    <h2 style="color: #333333; font-family: 'Red Hat Display', sans-serif; margin-top: 0;">Ciao {booking_details['name']},</h2>
                    <p style="font-size: 16px; line-height: 1.5;">La tua prenotazione da Fela! è confermata. Ecco i dettagli:</p>
                    
                    <table border="0" cellpadding="5" cellspacing="0" width="100%" style="margin-top: 20px; border-collapse: collapse;">
                        {event_row_html}
                        <tr style="border-bottom: 1px solid #eee;">
                            <td style="padding: 10px 0; font-size: 16px;"><strong>Data:</strong></td>
                            <td style="padding: 10px 0; font-size: 16px; text-align: right;">{booking_date_formatted}</td>
                        </tr>
                        <tr style="border-bottom: 1px solid #eee;">
                            <td style="padding: 10px 0; font-size: 16px;"><strong>Ora:</strong></td>
                            <td style="padding: 10px 0; font-size: 16px; text-align: right;">{booking_time_formatted}</td>
                        </tr>
                        <tr>
                            <td style="padding: 10px 0; font-size: 16px;"><strong>Persone:</strong></td>
                            <td style="padding: 10px 0; font-size: 16px; text-align: right;">{booking_details['guests']}</td>
                        </tr>
                    </table>

                    <p style="font-size: 16px; line-height: 1.5; margin-top: 30px;">Grazie per aver scelto Fela! Non vediamo l'ora di accoglierti.</p>
                    <p style="font-size: 14px; color: #888; margin-top: 25px;">Se hai bisogno di cancellare la tua prenotazione, puoi farlo cliccando sul seguente link: <a href="{cancellation_link}" style="color: #5b5bffff;">Cancella prenotazione</a>.</p>
                </td>
            </tr>
            <tr>
                <td align="center" style="padding: 20px; background-color: #f4f4f4; font-size: 12px; color: #777;">
                    <p style="margin: 0;">Fela! Music Bar | Via di S. Cosimo, 6r, 16128 Genova GE</p>
                    <p style="margin: 5px 0 0 0;">Questa è un'email generata automaticamente, per favore non rispondere.</p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    message = MessageSchema(
        subject="Conferma Prenotazione - Fela! Music Bar",
        recipients=[email],
        body=html_body,
        subtype=MessageType.html
    )

    fm = FastMail(conf_local)
    await fm.send_message(message)

@app.post("/api/bookings", response_model=schemas.Booking)
async def create_booking(booking: schemas.BookingCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Endpoint per creare una nuova prenotazione.
    Riceve i dati della prenotazione, li salva nel database e li restituisce.
    """
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

    # Convertiamo l'oggetto SQLAlchemy in un dizionario usando lo schema corretto
    booking_data_for_email = schemas.Booking.from_orm(db_booking).model_dump()

    # Aggiunge l'invio dell'email come task in background
    background_tasks.add_task(send_email_confirmation, booking.email, booking_data_for_email)

    return db_booking

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
    credentials: HTTPBasicCredentials = Depends(security), 
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
    
    # Per semplicità, l'username è fisso a "admin" e controlliamo solo la password.
    if credentials.username != "admin" or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide",
            headers={"WWW-Authenticate": "Basic"},
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
    credentials: HTTPBasicCredentials = Depends(security),
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
    if not ADMIN_PASSWORD or credentials.username != "admin" or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide",
            headers={"WWW-Authenticate": "Basic"},
        )

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
