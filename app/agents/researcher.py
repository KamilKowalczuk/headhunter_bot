import os
import re
import requests
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv

# Importy z aplikacji
from app.database import Lead, GlobalCompany
from app.tools import verify_email_domain, get_main_domain_url
from app.schemas import CompanyResearch

# Konfiguracja loggera
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("researcher")

load_dotenv()

# Konfiguracja API
gemini_key = os.getenv("GEMINI_API_KEY")
firecrawl_key = os.getenv("FIRECRAWL_API_KEY")

if not firecrawl_key:
    raise ValueError("❌ CRITICAL: Brak FIRECRAWL_API_KEY w .env.")

# Model AI - Zwiększamy temperaturę minimalnie dla kreatywności w 'icebreaker'
llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash", temperature=0.1, google_api_key=gemini_key)
structured_llm = llm.with_structured_output(CompanyResearch)

# --- NARZĘDZIA POMOCNICZE (SNIPER TOOLS) ---

def extract_emails_via_regex(text: str) -> list:
    """Szybki regex do wyłapywania maili przed AI."""
    if not text: return []
    # Ulepszony regex (odrzuca pliki graficzne w środku maila)
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    found = re.findall(email_pattern, text)
    unique = list(set(email.lower() for email in found))
    
    clean = []
    for email in unique:
        # Filtry antyspamowe/antyassetowe
        if email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg', '.woff')): continue
        if any(x in email for x in ['sentry', 'noreply', 'no-reply', 'example', 'domain', 'email']): continue
        if len(email) < 5 or len(email) > 60: continue
        clean.append(email)
    return clean

class TitanScraper:
    """Klient Firecrawl z obsługą błędów i timeoutów."""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.firecrawl.dev/v1"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def scrape(self, url, check_hiring=False):
        """Pobiera treść. Opcjonalnie szuka słów kluczowych rekrutacji."""
        endpoint = f"{self.base_url}/scrape"
        payload = {
            "url": url, 
            "formats": ["markdown"],
            "onlyMainContent": True, # Oszczędność tokenów - tylko mięso
            "timeout": 15000
        }
        try:
            response = requests.post(endpoint, headers=self.headers, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                content = data.get('data', {}).get('markdown', "")
                return content
            elif response.status_code == 429:
                logger.warning(f"Rate limit Firecrawl na {url}")
                return ""
            return ""
        except Exception as e:
            logger.error(f"Błąd scrapowania {url}: {e}")
            return ""

    def map_site(self, url):
        """Mapuje stronę w poszukiwaniu podstron."""
        endpoint = f"{self.base_url}/map"
        payload = {"url": url, "search": "contact about team career kontakt o-nas zespol kariera"}
        try:
            response = requests.post(endpoint, headers=self.headers, json=payload, timeout=15)
            if response.status_code == 200:
                data = response.json()
                return data.get('links', []) if 'links' in data else data.get('data', {}).get('links', [])
            return []
        except:
            return []

scraper = TitanScraper(firecrawl_key)

def _parallel_scrape(urls: list) -> str:
    """
    Równoległe pobieranie treści z wielu URLi.
    To jest GAME CHANGER dla wydajności.
    """
    full_content = ""
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_url = {executor.submit(scraper.scrape, url): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                data = future.result()
                if data and len(data) > 50:
                    # Dodajemy nagłówek, żeby AI wiedziało skąd jest tekst
                    section_name = "STRONA GŁÓWNA"
                    if "contact" in url or "kontakt" in url: section_name = "KONTAKT"
                    elif "about" in url or "o-nas" in url: section_name = "O NAS"
                    elif "career" in url or "kariera" in url: section_name = "KARIERA/PRACA"
                    
                    full_content += f"\n\n=== {section_name} ({url}) ===\n{data[:15000]}" # Limit na podstronę
            except Exception as e:
                logger.error(f"Błąd w wątku dla {url}: {e}")
    return full_content

def _get_content_titan_strategy(url: str) -> str:
    """Strategia Zwiadu: Mapowanie -> Wybór Celów -> Równoległy Atak."""
    print(f"      🔥 [TITAN] Rozpoczynam skanowanie domeny: {url}")
    
    # 1. Mapowanie (szybkie)
    links = scraper.map_site(url)
    pages_to_scrape = [url] # Zawsze strona główna
    
    # 2. Inteligentny wybór celów
    if links:
        # Priorytety: Kontakt > O nas > Kariera (szukanie sygnałów zakupowych)
        keywords_priority = {
            "kontakt": 1, "contact": 1,
            "o-nas": 2, "about": 2, "team": 2, "zespol": 2,
            "kariera": 3, "career": 3, "jobs": 3, "praca": 3
        }
        
        # Unikalne linki, sortowanie po priorytecie
        scored_links = []
        seen = set([url])
        
        for link in links:
            if link in seen: continue
            if any(ext in link.lower() for ext in ['.jpg', '.png', '.pdf', '.css', 'wp-content']): continue
            
            score = 10 # Domyślnie niski priorytet
            for key, val in keywords_priority.items():
                if key in link.lower():
                    score = val
                    break
            
            if score < 10: # Tylko jeśli znaleźliśmy słowo kluczowe
                scored_links.append((score, link))
                seen.add(link)

        # Sortujemy (1 najniższe = najważniejsze) i bierzemy max 3 dodatkowe podstrony
        scored_links.sort(key=lambda x: x[0])
        top_links = [x[1] for x in scored_links[:3]]
        pages_to_scrape.extend(top_links)
        
        print(f"         🎯 Cele taktyczne: {[u.split('/')[-1] for u in pages_to_scrape[1:]]}")
    else:
        # Fallback manualny
        base = url.rstrip('/')
        pages_to_scrape.extend([f"{base}/kontakt", f"{base}/o-nas"])

    # 3. Równoległe pobieranie (BŁYSKAWICZNE)
    return _parallel_scrape(pages_to_scrape)

def analyze_lead(session: Session, lead_id: int):
    """
    Główna funkcja analityczna.
    """
    lead = session.query(Lead).filter(Lead.id == lead_id).first()
    if not lead: return

    company = lead.company
    print(f"\n   🔎 [RESEARCHER] Analizuję firmę: {company.name}")
    
    # Normalizacja URL
    target_url = get_main_domain_url(company.domain)
    if not target_url.startswith("http"): target_url = "https://" + target_url

    # 1. POBIERANIE DANYCH (ASYNC LOGIC WRAPPED)
    content = _get_content_titan_strategy(target_url)
    
    if not content:
        print(f"      ❌ PUSTY ZWIAD. Oznaczam do ręcznego sprawdzenia.")
        lead.status = "MANUAL_CHECK"
        session.commit()
        return

    # 2. EKSTRAKCJA REGEX (SAFEGUARD)
    regex_emails = extract_emails_via_regex(content)
    if regex_emails:
        print(f"      👀 Regex znalazł: {regex_emails}")

    # 3. ANALIZA SEMANTYCZNA AI (MÓZG)
    print(f"      🧠 Uruchamiam Gemini 2.0 (Business Intelligence)...")
    
    system_prompt = f"""
    Jesteś elitarnym analitykiem sprzedaży B2B (Agency OS).
    Twoim celem jest przygotowanie "amunicji" dla copywritera, aby sprzedać usługi tej firmie.
    
    DANE WEJŚCIOWE:
    Strona WWW klienta (sekcje Home, Kontakt, O nas, Kariera).
    
    ZADANIE:
    1. Zidentyfikuj **Stack Technologiczny** (jakich narzędzi używają? Wordpress? React? HubSpot?).
    2. Znajdź **Sygnały Zakupowe (Hiring Signals)**. Czy rekrutują handlowców? Programistów? To oznacza, że mają budżet i potrzeby.
    3. Znajdź **Decydentów**. Imiona, nazwiska, stanowiska.
    4. Napisz **ICEBREAKER**. Jedno, genialne zdanie, które udowadnia, że zrobiliśmy research. Np. "Gratuluję nagrody X", "Widziałem, że szukacie Head of Sales".
    5. Wybierz najlepszy **E-MAIL**.
    
    Wskazówka od systemu (Regex): {', '.join(regex_emails) if regex_emails else 'Brak'}
    Jeśli Regex znalazł maila, zweryfikuj go kontekstowo i użyj.
    """
    
    chain = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", "{text}")]).pipe(structured_llm)
    
    try:
        # Przekazujemy tekst, ale ucinamy go bezpiecznie do okna kontekstu (ok 60k znaków dla pewności)
        research = chain.invoke({"text": content[:60000]})
    except Exception as e:
        print(f"      ❌ Błąd LLM: {e}")
        lead.status = "MANUAL_CHECK"
        session.commit()
        return

    # 4. LOGIKA WYBORU MAILA (SCORING)
    valid_email = None
    all_candidates = list(set((research.contact_emails or []) + regex_emails))
    
    def score_email(email):
        s = 0
        e = email.lower()
        # Bonusy
        if any(x in e for x in ['prezes', 'ceo', 'owner', 'dyrektor', 'head']): s += 10
        if '.' in e.split('@')[0]: s += 5 # Format imie.nazwisko
        if any(x in e for x in ['hello', 'contact', 'biuro', 'info']): s += 2
        # Kary
        if any(x in e for x in ['kariera', 'jobs', 'rekrutacja', 'no-reply', 'abuse']): s -= 100
        if not verify_email_domain(e): s -= 50 # Sprawdzenie DNS
        return s

    if all_candidates:
        # Sortuj malejąco po wyniku
        scored_emails = sorted([(e, score_email(e)) for e in all_candidates], key=lambda x: x[1], reverse=True)
        print(f"      📧 Scoring maili: {scored_emails}")
        
        best_email, score = scored_emails[0]
        if score > -20: # Próg akceptacji
            valid_email = best_email
        else:
            print("      ⚠️ Wszystkie maile odrzucone (spam/kariera/dns).")

    # 5. AKTUALIZACJA BAZY (COMMIT)
    company.tech_stack = research.tech_stack
    company.decision_makers = research.decision_makers
    company.industry = research.target_audience # Często ICP klienta mówi o jego branży
    company.last_scraped_at = datetime.utcnow()
    
    # Budujemy potężne podsumowanie dla Writera
    hiring_info = f"REKRUTUJĄ: {', '.join(research.hiring_signals)}" if research.hiring_signals else "Brak rekrutacji."
    lead.ai_analysis_summary = (
        f"ICEBREAKER: {research.icebreaker}\n"
        f"SUMMARY: {research.summary}\n"
        f"ICP: {research.target_audience}\n"
        f"{hiring_info}\n"
        f"PAIN POINTS: {research.pain_points_or_opportunities}"
    )
    
    if valid_email:
        lead.target_email = valid_email
        lead.status = "ANALYZED" # Gotowy dla Writera
        lead.ai_confidence_score = 95 # Wysokie zaufanie po głębokim researchu
        print(f"      ✅ SUKCES: Lead gotowy. Target: {valid_email}")
    else:
        lead.status = "MANUAL_CHECK" # Człowiek musi poszukać na LinkedIn
        lead.ai_confidence_score = 20
        print(f"      ⚠️ PARTIAL: Mamy dane, ale brak maila. Do ręcznej weryfikacji.")

    session.commit()