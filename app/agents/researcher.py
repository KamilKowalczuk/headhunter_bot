import os
import re
import httpx  # <--- ZMIANA: httpx zamiast requests
import json
import logging
import html
import asyncio
# from concurrent.futures import ThreadPoolExecutor, as_completed # <--- USUNIĘTE (Zastąpione przez asyncio.gather)
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
    # Nie rzucamy błędu krytycznego przy imporcie, tylko logujemy, żeby apka nie padła
    logger.error("❌ CRITICAL: Brak FIRECRAWL_API_KEY w .env. Researcher nie zadziała.")

# Model AI
llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash", temperature=0.1, google_api_key=gemini_key)
structured_llm = llm.with_structured_output(CompanyResearch)

# --- NARZĘDZIA POMOCNICZE (SNIPER TOOLS) ---

def extract_emails_from_html(raw_html: str) -> list:
    """Ekstrakcja z BRUDNEGO HTMLa (X-RAY)."""
    if not raw_html: return []
    
    text = html.unescape(raw_html)
    emails = []
    
    # 1. Linki mailto (Priorytet)
    mailto_pattern = r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
    emails.extend(re.findall(mailto_pattern, text))
    
    # 2. Tekst
    text_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails.extend(re.findall(text_pattern, text))
    
    unique = list(set(e.lower() for e in emails))
    clean = []
    for email in unique:
        if email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg', '.woff', '.webp', '.mp4')): continue
        if any(x in email for x in ['sentry', 'noreply', 'no-reply', 'example', 'domain', 'email.com', 'bootstrap', 'react']): continue
        if len(email) < 5 or len(email) > 60: continue
        clean.append(email)
        
    return clean

class TitanScraper:
    """Klient Firecrawl - Tryb Async (HTTPX)."""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.firecrawl.dev/v1"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def scrape(self, url):  # <--- ZMIANA: async def
        """Pobiera HTML (dla Regexa) i Markdown (dla AI)."""
        if not self.api_key: return None
        
        endpoint = f"{self.base_url}/scrape"
        payload = {
            "url": url, 
            "formats": ["markdown", "html"], 
            "onlyMainContent": False, # WAŻNE: Pobieramy stopki!
            "timeout": 20000,
            "excludeTags": ["script", "style", "video", "canvas"] 
        }
        
        # Używamy httpx.AsyncClient dla nieblokujących zapytań
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(endpoint, headers=self.headers, json=payload)
                if response.status_code == 200:
                    data = response.json().get('data', {})
                    if not data.get('markdown') and not data.get('html'):
                        return None
                    return {
                        "markdown": data.get('markdown', ""),
                        "html": data.get('html', "")
                    }
                return None
            except Exception as e:
                logger.error(f"Błąd scrapowania {url}: {e}")
                return None

    async def map_site(self, url): # <--- ZMIANA: async def
        """Mapuje stronę."""
        if not self.api_key: return []
        
        endpoint = f"{self.base_url}/map"
        payload = {"url": url, "search": "contact about team career kontakt o-nas zespol kariera"}
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(endpoint, headers=self.headers, json=payload)
                if response.status_code == 200:
                    data = response.json()
                    return data.get('links', []) or data.get('data', {}).get('links', [])
                return []
            except:
                return []

scraper = TitanScraper(firecrawl_key)

async def _parallel_scrape(urls: list) -> dict: # <--- ZMIANA: async def
    """Wielowątkowe pobieranie (Async Gather)."""
    combined_markdown = ""
    all_html_emails = []
    
    urls = list(set(urls))
    
    print(f"         🚀 Uruchamiam {len(urls)} zadań async scrapingowych...")
    
    # Zastępujemy ThreadPoolExecutor przez asyncio.gather (prawdziwa równoległość IO)
    tasks = [scraper.scrape(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for i, result in enumerate(results):
        url = urls[i]
        
        if isinstance(result, Exception):
            logger.error(f"Błąd zadania {url}: {result}")
            continue
            
        if result:
            # 1. Regex z HTML
            if result.get("html"):
                found = extract_emails_from_html(result["html"])
                if found:
                    print(f"            👀 Znaleziono w HTML ({url}): {found}")
                    all_html_emails.extend(found)
            
            # 2. Markdown dla AI
            md = result.get("markdown", "")
            if len(md) > 50:
                section_name = "STRONA"
                if "contact" in url or "kontakt" in url: section_name = "KONTAKT"
                elif "about" in url or "o-nas" in url: section_name = "O NAS"
                
                combined_markdown += f"\n\n=== {section_name} ({url}) ===\n{md[:15000]}"
                
    return {
        "markdown": combined_markdown,
        "regex_emails": list(set(all_html_emails))
    }

async def _get_content_titan_strategy(url: str) -> dict: # <--- ZMIANA: async def
    """Strategia BULLDOZER: Mapowanie + Wymuszone Ścieżki (Async)."""
    print(f"      🔥 [TITAN] Cel: {url}")
    
    base_url = url.rstrip('/')
    forced_pages = [
        base_url,
        f"{base_url}/kontakt",
        f"{base_url}/contact",
        f"{base_url}/o-nas",
        f"{base_url}/about"
    ]
    
    # Async Mapowanie
    mapped_links = await scraper.map_site(url)
    final_list = forced_pages.copy()
    
    if mapped_links:
        keywords = ["team", "zespol", "kariera", "career", "praca"]
        interesting = [l for l in mapped_links if any(k in l.lower() for k in keywords)]
        final_list.extend(interesting[:2])

    clean_urls = []
    seen = set()
    for u in final_list:
        if u in seen: continue
        if any(ext in u.lower() for ext in ['.pdf', '.jpg', '.png', '#']): continue
        clean_urls.append(u)
        seen.add(u)

    clean_urls.sort(key=lambda x: 0 if 'kontakt' in x or 'contact' in x else 1)
    target_urls = clean_urls[:5]

    print(f"         🎯 Lista celów: {[u.split('/')[-1] for u in target_urls]}")
    # Async Scraping
    return await _parallel_scrape(target_urls)

def analyze_lead(session: Session, lead_id: int):
    """
    RESEARCHER V4: BULLDOZER EDITION (Wrapper Synchroniczny).
    Uruchamia asynchroniczny scraping wewnątrz synchronicznej funkcji.
    """
    lead = session.query(Lead).filter(Lead.id == lead_id).first()
    if not lead: return

    company = lead.company
    client = lead.campaign.client # Pobieramy klienta żeby sprawdzić tryb
    mode = getattr(client, "mode", "SALES") # SALES lub JOB_HUNT

    print(f"\n   🔎 [RESEARCHER {mode}] Analiza: {company.name}")
    
    target_url = get_main_domain_url(company.domain)
    if not target_url.startswith("http"): target_url = "https://" + target_url

    # 1. POBIERANIE (Bulldozer Strategy - RUN ASYNC IN SYNC CONTEXT)
    # Używamy asyncio.run, aby odpalić szybki event loop dla httpx wewnątrz wątku roboczego
    try:
        scan_result = asyncio.run(_get_content_titan_strategy(target_url))
    except Exception as e:
        logger.error(f"      ❌ Błąd Async Loop w Research: {e}")
        scan_result = {"markdown": "", "regex_emails": []}
    
    content_md = scan_result["markdown"]
    regex_emails = scan_result["regex_emails"]

    if not content_md and not regex_emails:
        print(f"      ❌ PUSTY ZWIAD. Próba 404 na wszystkich podstronach.")
        lead.status = "MANUAL_CHECK"
        session.commit()
        return

    # 2. ANALIZA AI (Zależna od TRYBU)
    print(f"      🧠 Gemini analizuje dane...")
    
    regex_hint = ""
    if regex_emails:
        regex_hint = (
            f"ZNALAZŁEM NASTĘPUJĄCE MAILE W KODZIE HTML (TO SĄ FAKTY): {', '.join(regex_emails)}. "
            f"DODAJ JE DO LISTY contact_emails."
        )

    if mode == "JOB_HUNT":
        # --- PROMPT REKRUTACYJNY ---
        system_prompt = f"""
        Jesteś Analitykiem Rynku Pracy IT. Twoim zadaniem jest ocenić firmę jako potencjalnego PRACODAWCĘ.
        Analizujesz surową treść ze strony WWW.
        
        ZADANIA PRIORYTETOWE:
        1. **E-MAIL:** {regex_hint} Szukaj maili do HR, Rekrutacji (kariera@, jobs@, rekrutacja@) LUB do CTO/Team Leaderów.
        2. **TECH STACK:** Jakie technologie widać w ogłoszeniach o pracę lub opisach projektów? (np. Python, AWS, React).
        3. **HIRING:** Czy mają zakładkę "Kariera"? Czy szukają ludzi? (Nawet jeśli nie ma Twojego stanowiska).
        4. **DECYDENT:** Szukaj imion: CTO, Head of Engineering, HR Manager, Founder.
        
        CELE:
        - Znajdź punkty zaczepienia do listu motywacyjnego ("Widzę, że używacie X").
        - Wyłap kulturę firmy (Remote/Hybrid?).
        """
    else:
        # --- PROMPT SPRZEDAŻOWY (STANDARD) ---
        system_prompt = f"""
        Jesteś analitykiem B2B. Analizujesz surową treść HTML/Markdown z kilku podstron firmy.
        
        ZADANIE:
        1. **E-MAIL:** {regex_hint} Szukaj w sekcjach "Kontakt", "Stopka".
        2. Stack Tech & Hiring (Jako sygnał rozwoju).
        3. Icebreaker (Punkt zaczepienia do sprzedaży).
        
        Priorytety maili: Imienne > Biuro/Kontakt/Hello > Sprzedaż.
        Ignoruj: przykładowe domeny, webmasterów, grafikę.
        """
    
    try:
        chain = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", "{text}")]).pipe(structured_llm)
        research = chain.invoke({"text": content_md[:70000]})
    except Exception as e:
        print(f"      ❌ Błąd LLM: {e}")
        if regex_emails:
            print("      ⚠️ LLM Error. Ratuję lead mailami z HTML.")
            lead.target_email = regex_emails[0]
            lead.status = "ANALYZED"
            lead.ai_confidence_score = 50
            lead.ai_analysis_summary = "HTML RESCUE MODE. LLM FAILED."
            session.commit()
            return
        lead.status = "MANUAL_CHECK"
        session.commit()
        return

    # 3. MERGE & SCORE (Zależne od trybu)
    combined_emails = list(set((research.contact_emails or []) + regex_emails))
    
    def score_email(email):
        s = 0
        e = email.lower()
        if mode == "JOB_HUNT":
            # W trybie pracy: HR i Kariera są OK, ale CTO/Founder lepsi
            if any(x in e for x in ['kariera', 'jobs', 'rekrutacja', 'hr', 'people']): s += 20
            if any(x in e for x in ['cto', 'tech', 'engineering']): s += 25
            if any(x in e for x in ['ceo', 'founder']): s += 15
        else:
            # W trybie sprzedaży: Kariera to śmietnik
            if any(x in e for x in ['ceo', 'owner', 'founder', 'prezes']): s += 20
            if any(x in e for x in ['kariera', 'jobs', 'rekrutacja']): s -= 20 # Kara za HR w sprzedaży
            
        if any(x in e for x in ['biuro', 'info', 'hello', 'kontakt', 'office']): s += 15
        if '.' in e.split('@')[0]: s += 5
        if not verify_email_domain(e): s -= 100
        return s

    valid_email = None
    if combined_emails:
        scored = sorted([(e, score_email(e)) for e in combined_emails], key=lambda x: x[1], reverse=True)
        print(f"      📧 Scoring [{mode}]: {scored}")
        
        best_email, score = scored[0]
        if score > -20:
            valid_email = best_email

    # 4. ZAPIS
    company.tech_stack = research.tech_stack
    company.decision_makers = research.decision_makers
    company.industry = research.target_audience
    company.last_scraped_at = datetime.now()
    
    lead.ai_analysis_summary = (
        f"MODE: {mode}\n"
        f"ICEBREAKER: {research.icebreaker}\n"
        f"SUMMARY: {research.summary}\n"
        f"MAILS: {combined_emails}\n"
        f"HIRING: {research.hiring_signals}\n"
        f"PAIN: {research.pain_points_or_opportunities}"
    )
    
    if valid_email:
        lead.target_email = valid_email
        lead.status = "ANALYZED"
        lead.ai_confidence_score = 95
        print(f"      ✅ SUKCES: {valid_email}")
    else:
        lead.status = "MANUAL_CHECK"
        lead.ai_confidence_score = 15
        print(f"      ⚠️ MANUAL CHECK")

    session.commit()

# --- ASYNC WRAPPER DLA PĘTLI GŁÓWNEJ ---
async def analyze_lead_async(session: Session, lead_id: int):
    """
    Asynchroniczny wrapper dla researchera.
    Uruchamia ciężki proces scrapowania w osobnym wątku (przez analyze_lead),
    a wewnątrz wątku odpala się mini-loop AsyncIO dla httpx.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, analyze_lead, session, lead_id)