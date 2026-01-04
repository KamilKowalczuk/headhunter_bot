import os
import asyncio
import logging
from typing import List, Dict, Any, Set
from urllib.parse import urlparse
from apify_client import ApifyClientAsync
from sqlalchemy.orm import Session
from sqlalchemy import select, and_
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Importy aplikacji
from app.database import GlobalCompany, Lead, SearchHistory # <--- NOWY IMPORT
from app.schemas import StrategyOutput

# --- KONFIGURACJA ENTERPRISE ---
load_dotenv()
logger = logging.getLogger("scout")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")

# === BEZPIECZNIKI (FUSES) ===
BATCH_SIZE = 40             # Ile pobieramy z Google Maps na jedno zapytanie
SAFETY_LIMIT_LEADS = 20     # Max nowych leadów na cykl
SAFETY_LIMIT_QUERIES = 2    # Max zapytań na cykl (oszczędzamy budżet)
DUPLICATE_COOLDOWN_DAYS = 30 # Jak często możemy powtórzyć to samo zapytanie?
# ============================

ACTOR_ID = "compass/crawler-google-places"

if not APIFY_TOKEN:
    logger.error("❌ CRITICAL: Brak APIFY_API_TOKEN. Scout jest martwy.")
    client = None
else:
    client = ApifyClientAsync(APIFY_TOKEN)

def _clean_domain(website_url: str) -> str | None:
    """Enterprise-grade domain sanitizer."""
    if not website_url: return None
    try:
        parsed = urlparse(website_url)
        domain = parsed.netloc.replace("www.", "").lower().strip()
        if domain in ["facebook.com", "instagram.com", "linkedin.com", "google.com", "youtube.com", "twitter.com"]:
            return None
        return domain if domain else None
    except Exception:
        return None

async def run_scout_async(session: Session, campaign_id: int, strategy: StrategyOutput) -> int:
    """
    Silnik Zwiadowczy v4.0 (Memory + Precision Mode).
    """
    if not client:
        print("❌ Scout Error: Klient Apify nie jest zainicjowany.")
        return 0

    total_added = 0
    
    # 1. FILTRACJA ZAPYTAŃ (MEMORY CHECK)
    # Sprawdzamy historię, żeby nie palić budżetu na to samo
    raw_queries = strategy.search_queries
    valid_queries = []
    
    # Pobieramy ID klienta z kampanii (potrzebne do historii)
    # Zakładamy, że strategy zostało wywołane z poprawnym client context, 
    # ale tutaj potrzebujemy client_id. Pobierzmy je z kampanii.
    # (W main.py przekazujemy campaign_id, więc możemy pobrać klienta)
    from app.database import Campaign
    campaign_obj = session.query(Campaign).filter(Campaign.id == campaign_id).first()
    client_id = campaign_obj.client_id if campaign_obj else None

    print(f"\n🧠 [SCOUT MEMORY] Analizuję {len(raw_queries)} propozycji strategii...")

    for q in raw_queries:
        # Sprawdź czy szukaliśmy tego w ostatnich 30 dniach dla tego klienta
        last_search = session.query(SearchHistory).filter(
            SearchHistory.client_id == client_id,
            SearchHistory.query_text == q,
            SearchHistory.searched_at > datetime.utcnow() - timedelta(days=DUPLICATE_COOLDOWN_DAYS)
        ).first()

        if last_search:
            print(f"   🚫 POMIJAM: '{q}' (Szukano: {last_search.searched_at.strftime('%Y-%m-%d')})")
        else:
            valid_queries.append(q)

    # Bierzemy tylko TOP X unikalnych zapytań
    final_queries = valid_queries[:SAFETY_LIMIT_QUERIES]
    
    if not final_queries:
        print("   💤 Wszystkie propozycje strategii były już użyte. Scout idzie spać.")
        return 0

    print(f"🚀 [ASYNC SCOUT] Startuję zwiad dla: {final_queries}")
    print(f"   🎯 Cel: Max {SAFETY_LIMIT_LEADS} leadów.")

    for query in final_queries:
        if total_added >= SAFETY_LIMIT_LEADS:
            print(f"   🧨 LIMIT LEADOW OSIĄGNIĘTY. Stop.")
            break

        print(f"   📍 Wykonuję: '{query}'...")
        
        # --- ZMIANA KONFIGURACJI APIFY (PRECISION MODE) ---
        # Usuwamy 'locationQuery' i 'countryCode', żeby nie robić Grid Crawl.
        # Polegamy na tym, że 'query' zawiera nazwę miasta (AI o to dba).
        run_input = {
            "searchStringsArray": [query],
            "maxCrawledPlacesPerSearch": BATCH_SIZE, # Limit wyników na jedno hasło
            "language": "pl",
            "skipClosedPlaces": True,
            "onlyWebsites": True, # Kluczowe dla B2B
            # Wyłączamy zbędne dane = szybciej i taniej
            "scrapeReviewerName": False,
            "scrapeReviewerId": False,
            "scrapeReviewText": False,
            "scrapeReviewImage": False,
            "scrapeReviewRating": False
        }

        try:
            # Rejestrujemy próbę w historii (nawet jak nic nie znajdzie, żeby nie próbować ciągle błędnych haseł)
            if client_id:
                history_entry = SearchHistory(query_text=query, client_id=client_id, results_found=0)
                session.add(history_entry)
                session.commit() # Commit od razu, żeby zapisać "że próbowaliśmy"

            # Call Apify
            run = await client.actor(ACTOR_ID).call(run_input=run_input)
            if not run: continue

            dataset = client.dataset(run["defaultDatasetId"])
            dataset_items_page = await dataset.list_items()
            items = dataset_items_page.items

            if not items:
                print("      ⚠️ Brak wyników.")
                continue

            # Aktualizujemy historię o liczbę wyników
            history_entry.results_found = len(items)
            session.commit()

            print(f"      📥 Pobranno {len(items)} firm. Przetwarzanie...")

            # --- OPTYMALIZACJA BULK (Bez zmian, bo działała dobrze) ---
            raw_domains = [item.get("website") for item in items if item.get("website")]
            clean_domains = list(set([d for d in [_clean_domain(url) for url in raw_domains] if d]))
            
            if not clean_domains: continue

            existing_companies = session.query(GlobalCompany).filter(GlobalCompany.domain.in_(clean_domains)).all()
            existing_domains_map = {c.domain: c for c in existing_companies}
            
            new_companies_to_add = []
            
            # 1. Dodawanie firm
            for item in items:
                if total_added >= SAFETY_LIMIT_LEADS: break
                domain = _clean_domain(item.get("website"))
                if not domain or domain not in clean_domains: continue

                if domain not in existing_domains_map:
                    total_score = item.get("totalScore", 0)
                    category = item.get("categoryName", "Nieznana")
                    
                    new_company = GlobalCompany(
                        domain=domain,
                        name=item.get("title") or domain,
                        pain_points=[f"Category: {category}", f"Rating: {total_score}/5", f"From: {query}"],
                        is_active=True,
                        quality_score=int(total_score * 20) if total_score else 50
                    )
                    new_companies_to_add.append(new_company)
                    existing_domains_map[domain] = new_company
            
            if new_companies_to_add:
                session.add_all(new_companies_to_add)
                session.commit()

            # 2. Dodawanie leadów
            # Pobieramy ID firm
            current_companies = session.query(GlobalCompany).filter(GlobalCompany.domain.in_(clean_domains)).all()
            company_id_map = {c.domain: c.id for c in current_companies}

            # Sprawdzamy duplikaty w kampanii
            existing_leads = session.query(Lead).filter(
                Lead.campaign_id == campaign_id,
                Lead.global_company_id.in_(company_id_map.values())
            ).all()
            existing_lead_ids = {l.global_company_id for l in existing_leads}
            
            new_leads_to_add = []
            for domain in clean_domains:
                if total_added >= SAFETY_LIMIT_LEADS: break
                comp_id = company_id_map.get(domain)
                if not comp_id: continue

                if comp_id not in existing_lead_ids:
                    company_obj = existing_domains_map.get(domain)
                    score = company_obj.quality_score if company_obj else 50

                    new_lead = Lead(
                        campaign_id=campaign_id,
                        global_company_id=comp_id,
                        status="NEW",
                        ai_confidence_score=score
                    )
                    new_leads_to_add.append(new_lead)
                    existing_lead_ids.add(comp_id)
                    total_added += 1

            if new_leads_to_add:
                session.add_all(new_leads_to_add)
                session.commit()
                print(f"      💾 Dodano {len(new_leads_to_add)} nowych leadów.")

        except Exception as e:
            session.rollback()
            print(f"      ❌ Błąd w Async Scout: {e}")
            await asyncio.sleep(1)

    print(f"🏁 [SCOUT] Koniec tury. Wynik: {total_added}/{SAFETY_LIMIT_LEADS}")
    return total_added