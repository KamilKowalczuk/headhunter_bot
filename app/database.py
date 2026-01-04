import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Float
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from dotenv import load_dotenv

# Ładowanie konfiguracji z .env
load_dotenv()

# Pobranie adresu bazy danych
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("Błąd: Brak zmiennej DATABASE_URL w pliku .env")

# Inicjalizacja silnika bazy danych
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- 1. CLIENT DNA (Mózg Strategiczny) ---
class Client(Base):
    __tablename__ = "clients"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # STATUS AGENCJI (Nowość dla Dashboardu: ACTIVE / PAUSED)
    status = Column(String, default="ACTIVE") 
    mode = Column(String, default="SALES") # Opcje: "SALES", "JOB_HUNT"
    
    name = Column(String, nullable=False, unique=True) # np. "SoftwareHut"
    
    # STRATEGICZNE DNA (Wypełniasz w NocoDB)
    industry = Column(String)              # "Software House"
    value_proposition = Column(Text)       # "Dozimy MVP w 3 miesiące"
    ideal_customer_profile = Column(Text)  # "Fintechy, Seed/Series A"
    tone_of_voice = Column(String)         # "Profesjonalny, Direct"
    
    # HARD CONSTRAINTS (Czego unikać)
    negative_constraints = Column(Text)    # "Nie wspominaj o WordPress"
    case_studies = Column(Text)            # "Zrobiliśmy projekt X dla firmy Y..."
    
    # KONFIGURACJA TECHNICZNA
    sender_name = Column(String)           # "Kamil Kowalczuk"
    smtp_user = Column(String)             # "kamil@agencja.pl"
    smtp_password = Column(String)         # Hasło aplikacji
    smtp_server = Column(String)           # "smtp.googlemail.com"
    smtp_port = Column(Integer, default=465)
    imap_server = Column(String)           # np. imap.gmail.com
    imap_port = Column(Integer, default=993)
    daily_limit = Column(Integer, default=50) # Bezpiecznik wysyłki
    html_footer = Column(String, nullable=True) # Kod HTML stopki

    # --- WARM-UP CONFIG ---
    warmup_enabled = Column(Boolean, default=False)       # Czy rozgrzewka włączona?
    warmup_start_limit = Column(Integer, default=2)       # Od ilu zaczynamy?
    warmup_increment = Column(Integer, default=2)         # O ile zwiększamy dziennie?
    warmup_started_at = Column(DateTime, nullable=True)   # Kiedy zaczęliśmy?

    sending_mode = Column(String, default="DRAFT")

    attachment_filename = Column(String, nullable=True)

    campaigns = relationship("Campaign", back_populates="client")

# --- 2. GLOBAL KNOWLEDGE GRAPH (Pamięć Świata) ---
class GlobalCompany(Base):
    __tablename__ = "global_companies"
    
    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String, unique=True, index=True) # Klucz unikalny: "google.com"
    name = Column(String)
    
    # 360 INTELLIGENCE (Z Firecrawl)
    tech_stack = Column(JSONB, default=[])       # ["React", "AWS"]
    decision_makers = Column(JSONB, default=[])  # [{"name": "Jan", "role": "CTO"}]
    pain_points = Column(JSONB, default=[])      # ["Wolna strona", "Brak mobile"]
    hiring_status = Column(String)               # "Hiring" / "Layoffs"
    
    # VALIDATION LAYER (Sędzia)
    is_active = Column(Boolean, default=True)
    has_mx_records = Column(Boolean, default=False) # Czy maile działają?
    last_scraped_at = Column(DateTime, default=datetime.utcnow)
    quality_score = Column(Integer, default=0) # 0-100 (Pulse Check)

    leads = relationship("Lead", back_populates="company")

# --- 3. KAMPANIE (Zlecenia) ---
class Campaign(Base):
    __tablename__ = "campaigns"
    
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    name = Column(String)            # "Szukanie Fintechów UK"
    status = Column(String, default="ACTIVE") # ACTIVE, PAUSED, COMPLETED
    
    # STRATEGIA
    strategy_prompt = Column(Text)   # "Znajdź firmy, które niedawno dostały dofinansowanie"
    target_region = Column(String)   # "UK, London"

    client = relationship("Client", back_populates="campaigns")
    leads = relationship("Lead", back_populates="campaign")

# --- 4. LEADS (Konkretne Szanse Sprzedażowe) ---
class Lead(Base):
    __tablename__ = "leads"
    
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    global_company_id = Column(Integer, ForeignKey("global_companies.id"))
    
    # WYNIKI AGENTÓW
    ai_analysis_summary = Column(Text) # "Pasuje do ICP bo..."
    generated_email_subject = Column(String)
    generated_email_body = Column(Text)
    
    # KONTAKT (Email Hunter) - TO BYŁO BRAKUJĄCE
    target_email = Column(String, nullable=True) 

    # HALLUCINATION KILLER & DRIP
    ai_confidence_score = Column(Integer) # 0-100
    status = Column(String, default="NEW") # NEW -> SCRAPED -> DRAFTED -> SENT
    
    # FOLLOW-UP MECHANISM (Przeniesione z Campaign tutaj, bo dotyczy Leada)
    step_number = Column(Integer, default=1) # To naprawia AttributeError
    last_action_at = Column(DateTime, default=datetime.utcnow)

    scheduled_for = Column(DateTime) # Kiedy wysłać?
    sent_at = Column(DateTime)       # Kiedy wysłano?
    
    campaign = relationship("Campaign", back_populates="leads")
    company = relationship("GlobalCompany", back_populates="leads")

    # SEKCJA ODPOWIEDZI (INBOX)
    replied_at = Column(DateTime, nullable=True)
    reply_content = Column(String, nullable=True) # Treść maila od klienta
    reply_sentiment = Column(String, nullable=True) # POSITIVE, NEGATIVE, NEUTRAL
    reply_analysis = Column(String, nullable=True) # Krótka notatka AI

class SearchHistory(Base):
    __tablename__ = "search_history"

    id = Column(Integer, primary_key=True, index=True)
    query_text = Column(String, index=True) # np. "Software House Kraków"
    client_id = Column(Integer, ForeignKey("clients.id"))
    searched_at = Column(DateTime, default=datetime.utcnow)
    results_found = Column(Integer, default=0)

# Funkcja pomocnicza do pobierania sesji
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()