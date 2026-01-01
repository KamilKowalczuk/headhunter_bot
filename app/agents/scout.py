import os
import time
import logging
from typing import List, Dict, Any
from urllib.parse import urlparse
from apify_client import ApifyClient
from sqlalchemy.orm import Session
from sqlalchemy import select
from dotenv import load_dotenv

# Importy aplikacji
from app.database import GlobalCompany, Lead
from app.schemas import StrategyOutput

# --- KONFIGURACJA ENTERPRISE & SAFETY ---
load_dotenv()
logger = logging.getLogger("scout")
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")

# === BEZPIECZNIKI (FUSES) ===
BATCH_SIZE = 40             # Ile pobieramy z Google Maps na jedno zapytanie
SAFETY_LIMIT_LEADS = 20     # 🧨 MAX NOWYCH LEADÓW NA JEDNO URUCHOMIENIE (STOP po osiągnięciu)
SAFETY_LIMIT_QUERIES = 3    # 🧨 MAX FRAZ DO PRZETWORZENIA (Ignorujemy resztę strategii)
# ============================

COUNTRY_CODE = "pl"
LANGUAGE = "pl"
ACTOR_ID = "compass/crawler-google-places"

if not APIFY_TOKEN:
    logger.error("❌ CRITICAL: Brak APIFY_API_TOKEN. Scout jest martwy.")
    client = None
else:
    client = ApifyClient(APIFY_TOKEN)

def _clean_domain(website_url: str) -> str | None:
    """Enterprise-grade domain sanitizer."""
    if not website_url:
        return None
    try:
        parsed = urlparse(website_url)
        domain = parsed.netloc.replace("www.", "").lower().strip()
        return domain if domain else None
    except Exception:
        return None

def run_scout(session: Session, campaign_id: int, strategy: StrategyOutput) -> int:
    """
    Silnik Zwiadowczy v2.1 (Safety Fuse Edition).
    
    Zabezpieczenia:
    - Przerywa działanie po znalezieniu SAFETY_LIMIT_LEADS (np. 20 sztuk).
    - Analizuje max SAFETY_LIMIT_QUERIES (np. 3 frazy).
    - Oszczędza budżet Apify i bazę danych.
    """
    if not client:
        print("❌ Scout Error: Klient Apify nie jest zainicjowany.")
        return 0

    total_added = 0
    
    # 1. Ograniczamy liczbę zapytań (Query Cap)
    safe_queries = strategy.search_queries[:SAFETY_LIMIT_QUERIES]
    
    print(f"\n🚀 [SCOUT] Startuję zwiad (SAFE MODE).")
    print(f"   🎯 Limit: Max {SAFETY_LIMIT_LEADS} nowych leadów.")
    print(f"   🔍 Przetwarzam {len(safe_queries)} z {len(strategy.search_queries)} fraz strategii.")

    for query in safe_queries:
        # 2. Sprawdzenie głównego bezpiecznika PRZED wysłaniem zapytania do Apify (Oszczędność $$$)
        if total_added >= SAFETY_LIMIT_LEADS:
            print(f"   🧨 BEZPIECZNIK: Osiągnięto limit {SAFETY_LIMIT_LEADS} leadów. Przerywam pętlę.")
            break

        print(f"   📍 Skanuję sektor: '{query}'...")
        
        run_input = {
            "searchStringsArray": [query],
            "maxCrawledPlacesPerSearch": BATCH_SIZE,
            "language": LANGUAGE,
            "countryCode": COUNTRY_CODE,
            "locationQuery": "Poland",
            "skipClosedPlaces": True,
            "onlyWebsites": True,
            # Optymalizacja kosztów - wyłączamy zbędne dane
            "scrapeReviewerName": False,
            "scrapeReviewerId": False,
            "scrapeReviewerUrl": False,
            "scrapeReviewText": False,
            "scrapeReviewImage": False,
            "scrapeReviewRating": False
        }

        try:
            run = client.actor(ACTOR_ID).call(run_input=run_input)
            if not run: continue

            dataset_items = client.dataset(run["defaultDatasetId"]).list_items().items
            if not dataset_items:
                print("      ⚠️ Brak wyników.")
                continue

            print(f"      📥 Pobranno {len(dataset_items)} wyników. Filtruję...")

            domains_processed_in_batch = set()

            for item in dataset_items:
                # 3. Sprawdzenie bezpiecznika WEWNĄTRZ pętli przetwarzania
                if total_added >= SAFETY_LIMIT_LEADS:
                    break

                website = item.get("website")
                title = item.get("title")
                domain = _clean_domain(website)
                
                if not domain or domain in domains_processed_in_batch:
                    continue
                domains_processed_in_batch.add(domain)

                # Logika biznesowa (Baza danych)
                company = session.query(GlobalCompany).filter(GlobalCompany.domain == domain).first()

                if not company:
                    total_score = item.get("totalScore", 0)
                    reviews_count = item.get("reviewsCount", 0)
                    category = item.get("categoryName", "Nieznana")
                    address = item.get("address", "Brak adresu")

                    company = GlobalCompany(
                        domain=domain,
                        name=title or domain,
                        pain_points=[f"Category: {category}", f"Rating: {total_score}/5"],
                        is_active=True,
                        quality_score=int(total_score * 20) if total_score else 50
                    )
                    session.add(company)
                    session.flush()
                
                existing_lead = session.query(Lead).filter(
                    Lead.campaign_id == campaign_id,
                    Lead.global_company_id == company.id
                ).first()

                if not existing_lead:
                    new_lead = Lead(
                        campaign_id=campaign_id,
                        global_company_id=company.id,
                        status="NEW",
                        ai_confidence_score=company.quality_score
                    )
                    session.add(new_lead)
                    total_added += 1
            
            session.commit()
            print(f"      💾 Zapisano partię. Stan licznika: {total_added}/{SAFETY_LIMIT_LEADS}")

        except Exception as e:
            session.rollback()
            print(f"      ❌ Błąd w partii: {e}")
            time.sleep(1)

    print(f"🏁 [SCOUT] Misja zakończona. Pozyskano: {total_added} leadów.")
    return total_added