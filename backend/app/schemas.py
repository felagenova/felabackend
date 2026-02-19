from pydantic import BaseModel, EmailStr
from datetime import date, time
import uuid
from typing import Optional, List, Dict, Any

# Schema per la creazione di una prenotazione (dati in input dall'API)
class BookingCreate(BaseModel):
    event_id: Optional[int] = None # Nuovo campo
    name: str
    email: EmailStr
    phone: str
    booking_date: date
    booking_time: Optional[time] = None
    guests: int
    notes: Optional[str] = None
    push_subscription: Optional[Dict[str, Any]] = None # NUOVO: Dati per la notifica push

# Schema per la lettura di una prenotazione (dati in output dall'API)
class Booking(BookingCreate):
    id: int
    cancellation_token: uuid.UUID
    event_id: Optional[int] = None # Nuovo campo

    class Config:
        from_attributes = True

# Schemi per gli eventi speciali
class SpecialEventBase(BaseModel):
    display_name: str
    description: Optional[str] = None # NUOVO: Descrizione opzionale
    booking_date: date
    booking_time: Optional[time] = None
    available_slots: Optional[List[str]] = None # NUOVO: per i turni
    max_guests: Optional[int] = None # NUOVO: numero massimo di posti
    slot_capacities: Optional[Dict[str, int]] = None # NUOVO: capacità specifica per turno
    is_closed: bool = False # NUOVO

class SpecialEventCreate(SpecialEventBase):
    pass

class SpecialEvent(SpecialEventBase):
    id: int

    class Config:
        from_attributes = True

# Schema per l'iscrizione alle notifiche generali
class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: Dict[str, str]
