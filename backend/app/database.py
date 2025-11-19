from sqlalchemy import create_engine, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

# Carica le variabili d'ambiente da un file .env (per lo sviluppo locale)
# Costruisce il percorso al file .env che si trova nella cartella 'backend' (un livello sopra 'app')
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=dotenv_path)

# Render.com imposta automaticamente questa variabile d'ambiente.
# Per lo sviluppo locale, dovrai creare un file .env con DATABASE_URL="postgresql://user:password@host:port/dbname"
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("ERRORE: La variabile d'ambiente DATABASE_URL non è stata trovata. Assicurati che il file .env esista e sia corretto.")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Funzione per ottenere una sessione del database
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
