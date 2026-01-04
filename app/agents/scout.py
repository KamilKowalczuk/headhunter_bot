import os
import asyncio
import logging
from typing import List, Dict, Any, Set, Optional
from urllib.parse import urlparse
from apify_client import ApifyClientAsync
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc, text
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# AI Imports
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

# Importy aplikacji
from app.database import GlobalCompany, Lead, SearchHistory, Campaign, Client
from app.schemas import StrategyOutput

# --- KONFIGURACJA ENTERPRISE ---
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scout")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# === BEZPIECZNIKI (FUSES) ===
BATCH_SIZE = 40             
SAFETY_LIMIT_LEADS = 20     
SAFETY_LIMIT_QUERIES = 2    
DUPLICATE_COOLDOWN_DAYS = 30 
GLOBAL_CONTACT_COOLDOWN = 30 

# DOSTĘPNE ŹRÓDŁA DANYCH (Actors)
ACTOR_MAPS = "compass/crawler-google-places"
ACTOR_SEARCH = "apify/google-search-scraper" 

# Inicjalizacja AI (Gatekeeper)
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash", 
    temperature=0.0, # Zero kreatywności, czysta logika
    google_api_key=GEMINI_KEY
)

if not APIFY_TOKEN:
    logger.error("❌ CRITICAL: Brak APIFY_API_TOKEN. Scout jest martwy.")
    client = None
else:
    client = ApifyClientAsync(APIFY_TOKEN)

# --- MODEL DANYCH DLA AI GATEKEEPERA ---
class ValidatedDomain(BaseModel):
    domain: str = Field(..., description="Czysta domena, np. 'softwarehouse.com'")
    reason: str = Field(..., description="Krótkie uzasadnienie dlaczego pasuje do ICP")

class BatchValidationResult(BaseModel):
    valid_domains: List[ValidatedDomain] = Field(default_factory=list)

# --- LOGIKA BIZNESOWA ---

def _clean_domain(website_url: str) -> str | None:
    """Enterprise-grade domain sanitizer."""
    if not website_url: return None
    try:
        parsed = urlparse(website_url)
        domain = parsed.netloc or parsed.path 
        domain = domain.replace("www.", "").lower().strip()
        
        if "/" in domain: domain = domain.split("/")[0]

        blacklist = [
            "facebook.com", "instagram.com", "linkedin.com", "google.com", "youtube.com", "twitter.com", 
            "booksy.com", "znanylekarz.pl", "yelp.com", "researchgate.net", "wikipedia.org", "medium.com",
            "glassdoor.com", "indeed.com", "pracuj.pl", "nofluffjobs.com", "justjoin.it", "scholar.google.ca", 
            "scholar.google.com", "amazon.com", "allegro.pl", "olx.pl", "otomoto.pl", "booking.com", "tripadvisor.com",
            "f6s.com", "clutch.co", "goodfirms.co"
        ]
        
        if domain.endswith(".gov") or domain.endswith(".edu"): return None
        if domain in blacklist: return None
        if "." not in domain: return None

        return domain
    except Exception:
        return None

def _get_client_icp(session: Session, campaign_id: int) -> dict:
    """Pobiera dane klienta potrzebne do filtracji AI."""
    campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign or not campaign.client:
        return {"icp": "General Business", "industry": "B2B"}
    return {
        "icp": campaign.client.ideal_customer_profile,
        "industry": campaign.client.industry,
        "mode": getattr(campaign.client, "mode", "SALES")
    }

async def _ai_filter_batch(raw_items: List[Dict], client_data: Dict) -> List[str]:
    """
    PROTOCOL: GARBAGE COLLECTOR.
    Gemini 2.0 Flash analizuje listę 40 surowych wyników i odrzuca śmieci (B2C, pomyłki map, konkurencję).
    """
    candidates = []
    for item in raw_items:
        url = item.get("website") or item.get("url")
        clean = _clean_domain(url)
        if clean:
            name = item.get("title") or item.get("title", "Unknown")
            category = item.get("categoryName") or "Web Search"
            candidates.append(f"- URL: {clean} | NAME: {name} | CATEGORY: {category}")
    
    if not candidates: return []

    candidates_str = "\n".join(candidates[:50]) # Limit promptu
    
    system_prompt = """
    Jesteś Gatekeeperem bazy danych B2B. Twoim zadaniem jest filtracja surowych wyników ze scrapingu.
    
    KLIENT (Dla kogo szukamy):
    - Branża: {industry}
    - Kogo szuka (ICP): {icp}
    - Tryb: {mode} (SALES = szuka klientów, JOB_HUNT = szuka pracodawców)
    
    ZASADY FILTRACJI (PROTOCOL 0/1):
    1. ODRZUCAJ pomyłki kategorii (np. szukamy "Software House", a wynik to "Sklep z grami").
    2. ODRZUCAJ gigantów i portale (Facebook, Amazon, Allegro) jeśli jakimś cudem przeszły.
    3. ODRZUCAJ instytucje publiczne (Urząd, Szkoła) chyba że ICP mówi inaczej.
    4. Jeśli Tryb to SALES: ODRZUCAJ konkurencję klienta (chyba że szukamy partnerów).
    
    LISTA KANDYDATÓW:
    {candidates}
    
    Zwróć listę TYLKO pasujących domen w formacie JSON.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Dokonaj selekcji.")
    ])

    # Używamy structured output dla bezpieczeństwa typów
    gatekeeper = prompt | llm.with_structured_output(BatchValidationResult)

    try:
        print(f"      🤖 [AI GATEKEEPER] Analizuję {len(candidates)} kandydatów...")
        result = await gatekeeper.ainvoke({
            "industry": client_data["industry"],
            "icp": client_data["icp"],
            "mode": client_data["mode"],
            "candidates": candidates_str
        })
        
        valid_domains = [v.domain for v in result.valid_domains]
        print(f"      ✅ [AI GATEKEEPER] Przepuszczono: {len(valid_domains)}/{len(candidates)}")
        return valid_domains

    except Exception as e:
        logger.error(f"AI Filter Error: {e}")
        # Fail-open: w razie błędu AI zwracamy wszystkie technicznie poprawne domeny, żeby nie zatrzymać procesu
        return [c.split("|")[0].replace("- URL:", "").strip() for c in candidates]

# --- FUNKCJE BAZODANOWE (Wrapper) ---

def _db_get_valid_queries(session: Session, campaign_id: int, raw_queries: List[str]) -> tuple[List[str], int]:
    campaign_obj = session.query(Campaign).filter(Campaign.id == campaign_id).first()
    client_id = campaign_obj.client_id if campaign_obj else None
    
    valid_queries = []
    print(f"\n🧠 [SCOUT MEMORY] Analizuję {len(raw_queries)} propozycji strategii...")

    for q in raw_queries:
        last_search = session.query(SearchHistory).filter(
            SearchHistory.client_id == client_id,
            SearchHistory.query_text == q,
            SearchHistory.searched_at > datetime.now() - timedelta(days=DUPLICATE_COOLDOWN_DAYS)
        ).first()

        if last_search:
            print(f"   🚫 POMIJAM: '{q}' (Szukano: {last_search.searched_at.strftime('%Y-%m-%d')})")
        else:
            valid_queries.append(q)
            
    return valid_queries[:SAFETY_LIMIT_QUERIES], client_id

def _db_create_history_entry(session: Session, client_id: int, query: str) -> int:
    if not client_id: return None
    entry = SearchHistory(query_text=query, client_id=client_id, results_found=0)
    session.add(entry)
    session.commit()
    return entry.id

def _db_update_history_results(session: Session, entry_id: int, count: int):
    if not entry_id: return
    session.query(SearchHistory).filter(SearchHistory.id == entry_id).update({"results_found": count})
    session.commit()

def _db_process_scraped_items(session: Session, campaign_id: int, items: List[Dict], query: str, approved_domains: List[str]) -> int:
    """
    Wersja v2: Przyjmuje listę approved_domains z AI.
    """
    added_count = 0
    
    # 1. Filtrowanie po liście od AI
    # approved_domains są już po _clean_domain w funkcji AI, ale dla pewności:
    clean_approved = set(d.lower().strip() for d in approved_domains)
    
    if not clean_approved:
        return 0

    # 2. Pobranie istniejących firm (Cache Bazy)
    existing_companies = session.query(GlobalCompany).filter(GlobalCompany.domain.in_(list(clean_approved))).all()
    existing_domains_map = {c.domain: c for c in existing_companies}
    
    new_companies_to_add = []
    
    # Mapowanie itemów na obiekty GlobalCompany
    for item in items:
        url = item.get("website") or item.get("url")
        d = _clean_domain(url)
        
        # KEY CHECK: Czy domena jest na liście zatwierdzonej przez AI?
        if not d or d not in clean_approved: continue
        
        if d not in existing_domains_map:
            title = item.get("title") or item.get("title", d)
            category = item.get("categoryName") or "Web Search"
            total_score = item.get("totalScore", 0) 
            
            quality_score = int(total_score * 20) if total_score else 60

            new_company = GlobalCompany(
                domain=d,
                name=title,
                pain_points=[f"Source: {category}", f"Query: {query}"],
                is_active=True,
                quality_score=quality_score
            )
            new_companies_to_add.append(new_company)
            existing_domains_map[d] = new_company 
    
    # Zapis nowych firm
    if new_companies_to_add:
        session.add_all(new_companies_to_add)
        session.commit()
        for c in new_companies_to_add:
            existing_domains_map[c.domain] = c

    # 3. Przetwarzanie Leadów
    current_company_ids = [c.id for c in existing_domains_map.values()]
    
    leads_in_campaign = session.query(Lead.global_company_id).filter(
        Lead.campaign_id == campaign_id,
        Lead.global_company_id.in_(current_company_ids)
    ).all()
    ids_in_this_campaign = {l[0] for l in leads_in_campaign}
    
    new_leads_to_add = []
    
    for domain in clean_approved:
        if added_count >= SAFETY_LIMIT_LEADS: break
        
        company_obj = existing_domains_map.get(domain)
        if not company_obj: continue

        if company_obj.id in ids_in_this_campaign: continue

        last_contact = session.query(Lead).filter(
            Lead.global_company_id == company_obj.id,
            Lead.status == "SENT"
        ).order_by(desc(Lead.sent_at)).first()

        if last_contact and last_contact.sent_at:
            days_since = (datetime.now() - last_contact.sent_at).days
            if days_since < GLOBAL_CONTACT_COOLDOWN:
                print(f"      ⏳ {domain}: KARENCJA ({days_since} dni). Skip.")
                continue

        new_lead = Lead(
            campaign_id=campaign_id,
            global_company_id=company_obj.id,
            status="NEW",
            ai_confidence_score=company_obj.quality_score or 50
        )
        new_leads_to_add.append(new_lead)
        ids_in_this_campaign.add(company_obj.id)
        added_count += 1

    if new_leads_to_add:
        session.add_all(new_leads_to_add)
        session.commit()
        
    return len(new_leads_to_add)


async def run_scout_async(session: Session, campaign_id: int, strategy: StrategyOutput) -> int:
    """
    Silnik Zwiadowczy v6.0 (AI Gatekeeper Enhanced).
    """
    if not client:
        print("❌ Scout Error: Klient Apify nie jest zainicjowany.")
        return 0

    # Pobieramy kontekst klienta RAZ na początku
    client_data = _get_client_icp(session, campaign_id)
    print(f"🕵️ [SCOUT] Kontekst AI: Szukam dla branży '{client_data['industry']}'")

    total_added = 0
    
    raw_queries = strategy.search_queries
    valid_queries, client_id = await asyncio.to_thread(_db_get_valid_queries, session, campaign_id, raw_queries)
    
    if not valid_queries:
        print("   💤 Scout: Brak nowych zapytań (wszystkie wykorzystane).")
        return 0

    print(f"🚀 [ASYNC SCOUT] Startuję zwiad dla: {valid_queries}")

    for query in valid_queries:
        if total_added >= SAFETY_LIMIT_LEADS:
            print(f"   🧨 LIMIT LEADOW OSIĄGNIĘTY. Stop.")
            break

        print(f"   📍 Wykonuję: '{query}'...")
        
        use_google_search = False
        if "remote" in query.lower() or "saas" in query.lower() or "startup" in query.lower() or "software" in query.lower():
            use_google_search = True
            print("      🌐 Tryb: GOOGLE SEARCH")
        else:
            print("      🗺️  Tryb: GOOGLE MAPS")

        history_id = await asyncio.to_thread(_db_create_history_entry, session, client_id, query)

        items = []
        try:
            if not use_google_search:
                run_input = {
                    "searchStringsArray": [query],
                    "maxCrawledPlacesPerSearch": BATCH_SIZE,
                    "language": "pl",
                    "skipClosedPlaces": True,
                    "onlyWebsites": True,
                }
                run = await client.actor(ACTOR_MAPS).call(run_input=run_input)
            else:
                clean_query = query + " -site:linkedin.com -site:facebook.com -site:youtube.com"
                run_input = {
                    "queries": clean_query, 
                    "resultsPerPage": BATCH_SIZE,
                    "countryCode": "pl",
                    "languageCode": "pl",
                }
                run = await client.actor(ACTOR_SEARCH).call(run_input=run_input)

            if run:
                dataset = client.dataset(run["defaultDatasetId"])
                dataset_items_page = await dataset.list_items()
                raw_items = dataset_items_page.items
                
                if use_google_search:
                    for ri in raw_items:
                        items.extend(ri.get("organicResults", []))
                else:
                    items = raw_items

            if not items:
                print("      ⚠️ Brak wyników w Apify.")
                continue

            await asyncio.to_thread(_db_update_history_results, session, history_id, len(items))
            print(f"      📥 Pobranno {len(items)} surowych wyników.")

            # --- AI GATEKEEPER STEP ---
            # Zamiast wrzucać wszystko, pytamy Gemini co jest wartościowe
            approved_domains = await _ai_filter_batch(items, client_data)
            
            if not approved_domains:
                print("      🗑️ AI odrzuciło wszystkie wyniki jako nieistotne.")
                continue

            # --- PROCESS BATCH ---
            added_in_batch = await asyncio.to_thread(
                _db_process_scraped_items, 
                session, 
                campaign_id, 
                items, 
                query, 
                approved_domains # Przekazujemy przefiltrowaną listę
            )
            
            print(f"      💾 Zapisano {added_in_batch} unikalnych leadów (z {len(approved_domains)} zaakceptowanych).")
            total_added += added_in_batch

        except Exception as e:
            print(f"      ❌ Błąd w Async Scout: {e}")
            # await asyncio.sleep(1) # Opcjonalne

    print(f"🏁 [SCOUT] Koniec tury. Wynik: {total_added}/{SAFETY_LIMIT_LEADS}")
    return total_added