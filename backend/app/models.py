from sqlalchemy import Column, Integer, String, Date, Time, ForeignKey, Boolean
from .database import Base
import uuid
from sqlalchemy.orm import relationship

class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("special_events.id"), nullable=True) # Nuovo campo
    name = Column(String, index=True)
    email = Column(String, unique=True, index=True)
    phone = Column(String)
    booking_date = Column(Date)
    booking_time = Column(Time)
    guests = Column(Integer)
    cancellation_token = Column(String, unique=True, index=True, default=lambda: str(uuid.uuid4()))
    notes = Column(String, nullable=True)

    event = relationship("SpecialEvent", back_populates="bookings") # Relazione con SpecialEvent

class SpecialEvent(Base):
    __tablename__ = "special_events"
    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String, index=True)
    booking_date = Column(Date)
    booking_time = Column(Time, nullable=True) # L'ora può essere opzionale per alcuni eventi
    is_closed = Column(Boolean, default=False, nullable=False) # NUOVO: per chiudere le prenotazioni

    bookings = relationship("Booking", back_populates="event") # Relazione inversa con Booking
