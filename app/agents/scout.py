import os
import asyncio
import logging
from typing import List, Dict, Any, Set
from urllib.parse import urlparse
from apify_client import ApifyClientAsync
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc, text
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Importy aplikacji
from app.database import GlobalCompany, Lead, SearchHistory, Campaign
from app.schemas import StrategyOutput

# --- KONFIGURACJA ENTERPRISE ---
load_dotenv()
logger = logging.getLogger("scout")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")

# === BEZPIECZNIKI (FUSES) ===
BATCH_SIZE = 40             
SAFETY_LIMIT_LEADS = 20     
SAFETY_LIMIT_QUERIES = 2    
DUPLICATE_COOLDOWN_DAYS = 30 # Historia wyszukiwania Scouta
GLOBAL_CONTACT_COOLDOWN = 30 # Okres karencji dla firmy (dni)
# ============================

# DOSTĘPNE ŹRÓDŁA DANYCH (Actors)
ACTOR_MAPS = "compass/crawler-google-places"
ACTOR_SEARCH = "apify/google-search-scraper" 

if not APIFY_TOKEN:
    logger.error("❌ CRITICAL: Brak APIFY_API_TOKEN. Scout jest martwy.")
    client = None
else:
    client = ApifyClientAsync(APIFY_TOKEN)

def _clean_domain(website_url: str) -> str | None:
    """Enterprise-grade domain sanitizer."""
    if not website_url: return None
    try:
        # Usuwamy protokół i www
        parsed = urlparse(website_url)
        domain = parsed.netloc or parsed.path # Fallback jeśli brak http
        domain = domain.replace("www.", "").lower().strip()
        
        # Oczyszczanie ze śmieci po slashu
        if "/" in domain: domain = domain.split("/")[0]

        # Blacklista gigantów
        blacklist = ["facebook.com", "instagram.com", "linkedin.com", "google.com", "youtube.com", "twitter.com", "booksy.com", "znanylekarz.pl", "yelp.com"]
        if domain in blacklist:
            return None
        
        # Musi mieć kropkę (np. firma.pl)
        if "." not in domain:
            return None

        return domain
    except Exception:
        return None

# --- FUNKCJE SYNCHRONICZNE DB (Wrapperowane w to_thread) ---

def _db_get_valid_queries(session: Session, campaign_id: int, raw_queries: List[str]) -> tuple[List[str], int]:
    """Sprawdza historię wyszukiwania i filtruje duplikaty."""
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
    """Tworzy wpis w historii i zwraca jego ID."""
    if not client_id: return None
    entry = SearchHistory(query_text=query, client_id=client_id, results_found=0)
    session.add(entry)
    session.commit()
    return entry.id

def _db_update_history_results(session: Session, entry_id: int, count: int):
    """Aktualizuje liczbę znalezionych wyników."""
    if not entry_id: return
    session.query(SearchHistory).filter(SearchHistory.id == entry_id).update({"results_found": count})
    session.commit()

def _db_process_scraped_items(session: Session, campaign_id: int, items: List[Dict], query: str) -> int:
    """
    LOGIKA TRANSAKCYJNA:
    1. Czyści domeny.
    2. Aktualizuje GlobalCompany (Upsert).
    3. Tworzy Leady (sprawdzając duplikaty i karencję).
    Zwraca liczbę dodanych leadów.
    """
    added_count = 0
    
    # 1. Ekstrakcja domen
    raw_domains = []
    for item in items:
        # Format Google Maps
        if item.get("website"): 
            raw_domains.append(item.get("website"))
        # Format Google Search (organicResults)
        elif item.get("url"):
            raw_domains.append(item.get("url"))

    clean_domains_list = []
    for d in raw_domains:
        cd = _clean_domain(d)
        if cd: clean_domains_list.append(cd)
    
    clean_domains = list(set(clean_domains_list))
    if not clean_domains: return 0

    # 2. Pobranie istniejących firm (Cache Bazy)
    existing_companies = session.query(GlobalCompany).filter(GlobalCompany.domain.in_(clean_domains)).all()
    existing_domains_map = {c.domain: c for c in existing_companies}
    
    new_companies_to_add = []
    
    # Mapowanie itemów na obiekty GlobalCompany
    
    for item in items:
        url = item.get("website") or item.get("url")
        d = _clean_domain(url)
        if not d or d not in clean_domains: continue
        
        if d not in existing_domains_map:
            # Ujednolicamy dane z różnych źródeł
            title = item.get("title") or item.get("title", d)
            category = item.get("categoryName") or "Web Search"
            total_score = item.get("totalScore", 0) # Maps only
            
            # Wyszukiwarka Google nie daje ratingu, więc dajemy domyślny score
            quality_score = int(total_score * 20) if total_score else 60

            new_company = GlobalCompany(
                domain=d,
                name=title,
                pain_points=[f"Source: {category}", f"Query: {query}"],
                is_active=True,
                quality_score=quality_score
            )
            new_companies_to_add.append(new_company)
            existing_domains_map[d] = new_company # Dodajemy do mapy tymczasowej
    
    # Zapis nowych firm
    if new_companies_to_add:
        session.add_all(new_companies_to_add)
        session.commit()
        # Odświeżamy ID po commicie
        for c in new_companies_to_add:
            existing_domains_map[c.domain] = c

    # 3. Przetwarzanie Leadów
    # Pobieramy ID firm, które są JUŻ w tej kampanii
    current_company_ids = [c.id for c in existing_domains_map.values()]
    
    leads_in_campaign = session.query(Lead.global_company_id).filter(
        Lead.campaign_id == campaign_id,
        Lead.global_company_id.in_(current_company_ids)
    ).all()
    ids_in_this_campaign = {l[0] for l in leads_in_campaign}
    
    new_leads_to_add = []
    
    for domain in clean_domains:
        if added_count >= SAFETY_LIMIT_LEADS: break
        
        company_obj = existing_domains_map.get(domain)
        if not company_obj: continue

        # A. Lokalny Duplikat
        if company_obj.id in ids_in_this_campaign:
            continue

        # B. Globalna Karencja (Check Cooldown)
        last_contact = session.query(Lead).filter(
            Lead.global_company_id == company_obj.id,
            Lead.status == "SENT"
        ).order_by(desc(Lead.sent_at)).first()

        if last_contact and last_contact.sent_at:
            days_since = (datetime.now() - last_contact.sent_at).days
            if days_since < GLOBAL_CONTACT_COOLDOWN:
                print(f"      ⏳ {domain}: KARENCJA ({days_since} dni). Skip.")
                continue
            else:
                print(f"      ♻️ {domain}: RECYKLING (Kontakt > {GLOBAL_CONTACT_COOLDOWN} dni).")

        # C. Dodawanie Leada
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
    Silnik Zwiadowczy v5.0 (Non-Blocking & Multi-Source).
    """
    if not client:
        print("❌ Scout Error: Klient Apify nie jest zainicjowany.")
        return 0

    total_added = 0
    
    # 1. FILTRACJA ZAPYTAŃ
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
        
        # --- WYBÓR ŹRÓDŁA ---
        use_google_search = False
        if "remote" in query.lower() or "saas" in query.lower() or "startup" in query.lower() or "software" in query.lower():
            use_google_search = True
            print("      🌐 Tryb: GOOGLE SEARCH (Lepszy dla SaaS/Remote)")
        else:
            print("      🗺️  Tryb: GOOGLE MAPS (Lepszy dla lokalnych firm)")

        # Log startu w DB
        history_id = await asyncio.to_thread(_db_create_history_entry, session, client_id, query)

        items = []
        try:
            if not use_google_search:
                # === GOOGLE MAPS ACTOR ===
                run_input = {
                    "searchStringsArray": [query],
                    "maxCrawledPlacesPerSearch": BATCH_SIZE,
                    "language": "pl",
                    "skipClosedPlaces": True,
                    "onlyWebsites": True,
                }
                run = await client.actor(ACTOR_MAPS).call(run_input=run_input)
            else:
                # === GOOGLE SEARCH ACTOR (POPRAWIONE) ===
                # UWAGA: Ten aktor przyjmuje "queries" jako STRING (wiersze), a nie listę!
                run_input = {
                    "queries": query, # <--- TU BYŁ BŁĄD (było [query])
                    "resultsPerPage": BATCH_SIZE,
                    "countryCode": "pl",
                    "languageCode": "pl",
                }
                run = await client.actor(ACTOR_SEARCH).call(run_input=run_input)

            if run:
                dataset = client.dataset(run["defaultDatasetId"])
                dataset_items_page = await dataset.list_items()
                
                # Normalizacja wyników (Search vs Maps)
                raw_items = dataset_items_page.items
                if use_google_search:
                    # Google Search zwraca listę 'organicResults' wewnątrz itemu
                    for ri in raw_items:
                        items.extend(ri.get("organicResults", []))
                else:
                    items = raw_items

            if not items:
                print("      ⚠️ Brak wyników w Apify.")
                continue

            # Aktualizacja licznika w DB
            await asyncio.to_thread(_db_update_history_results, session, history_id, len(items))

            print(f"      📥 Pobranno {len(items)} surowych wyników. Przetwarzanie...")

            # --- PROCESS BATCH (Non-blocking DB Transaction) ---
            added_in_batch = await asyncio.to_thread(_db_process_scraped_items, session, campaign_id, items, query)
            
            print(f"      💾 Zapisano {added_in_batch} unikalnych leadów.")
            total_added += added_in_batch

        except Exception as e:
            print(f"      ❌ Błąd w Async Scout: {e}")
            await asyncio.sleep(1)

    print(f"🏁 [SCOUT] Koniec tury. Wynik: {total_added}/{SAFETY_LIMIT_LEADS}")
    return total_added