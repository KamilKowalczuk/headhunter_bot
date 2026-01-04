import os
import asyncio
import logging
from typing import List, Dict, Any, Set
from urllib.parse import urlparse
from apify_client import ApifyClientAsync
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, desc # <--- ZMIANA: Dodano 'desc' do sortowania dat
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Importy aplikacji
from app.database import GlobalCompany, Lead, SearchHistory
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
GLOBAL_CONTACT_COOLDOWN = 30 # <--- ZMIANA: Okres karencji dla firmy (dni)
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
    Silnik Zwiadowczy v5.0 (Recycling Mode).
    """
    if not client:
        print("❌ Scout Error: Klient Apify nie jest zainicjowany.")
        return 0

    total_added = 0
    
    # 1. FILTRACJA ZAPYTAŃ (MEMORY CHECK)
    raw_queries = strategy.search_queries
    valid_queries = []
    
    from app.database import Campaign
    campaign_obj = session.query(Campaign).filter(Campaign.id == campaign_id).first()
    client_id = campaign_obj.client_id if campaign_obj else None

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

    final_queries = valid_queries[:SAFETY_LIMIT_QUERIES]
    
    if not final_queries:
        print("   💤 Wszystkie propozycje strategii były już użyte. Scout idzie spać.")
        return 0

    print(f"🚀 [ASYNC SCOUT] Startuję zwiad dla: {final_queries}")

    for query in final_queries:
        if total_added >= SAFETY_LIMIT_LEADS:
            print(f"   🧨 LIMIT LEADOW OSIĄGNIĘTY. Stop.")
            break

        print(f"   📍 Wykonuję: '{query}'...")
        
        run_input = {
            "searchStringsArray": [query],
            "maxCrawledPlacesPerSearch": BATCH_SIZE,
            "language": "pl",
            "skipClosedPlaces": True,
            "onlyWebsites": True,
            "scrapeReviewerName": False,
            "scrapeReviewerId": False,
            "scrapeReviewText": False,
            "scrapeReviewImage": False,
            "scrapeReviewRating": False
        }

        try:
            if client_id:
                history_entry = SearchHistory(query_text=query, client_id=client_id, results_found=0)
                session.add(history_entry)
                session.commit()

            run = await client.actor(ACTOR_ID).call(run_input=run_input)
            if not run: continue

            dataset = client.dataset(run["defaultDatasetId"])
            dataset_items_page = await dataset.list_items()
            items = dataset_items_page.items

            if not items:
                print("      ⚠️ Brak wyników.")
                continue

            history_entry.results_found = len(items)
            session.commit()

            print(f"      📥 Pobranno {len(items)} firm. Analiza duplikatów i karencji...")

            # --- KROK 1: Aktualizacja GlobalCompany ---
            raw_domains = [item.get("website") for item in items if item.get("website")]
            clean_domains = list(set([d for d in [_clean_domain(url) for url in raw_domains] if d]))
            
            if not clean_domains: continue

            existing_companies = session.query(GlobalCompany).filter(GlobalCompany.domain.in_(clean_domains)).all()
            existing_domains_map = {c.domain: c for c in existing_companies}
            
            new_companies_to_add = []
            
            for item in items:
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
                    existing_domains_map[domain] = new_company # Dodajemy do mapy tymczasowo
            
            if new_companies_to_add:
                session.add_all(new_companies_to_add)
                session.commit()

            # --- KROK 2: Tworzenie Leadów (z Logiką Recyklingu) ---
            
            # Pobieramy ID firm po commicie
            current_companies = session.query(GlobalCompany).filter(GlobalCompany.domain.in_(clean_domains)).all()
            company_id_map = {c.domain: c for c in current_companies}

            # Sprawdzamy, czy firma jest JUŻ W TEJ KAMPANII (Lokalny Duplikat)
            leads_in_this_campaign = session.query(Lead.global_company_id).filter(
                Lead.campaign_id == campaign_id,
                Lead.global_company_id.in_([c.id for c in current_companies])
            ).all()
            ids_in_this_campaign = {l[0] for l in leads_in_this_campaign}
            
            new_leads_to_add = []
            
            for domain in clean_domains:
                if total_added >= SAFETY_LIMIT_LEADS: break
                
                company_obj = company_id_map.get(domain)
                if not company_obj: continue

                # A. Sprawdzenie Lokalnego Duplikatu (Czy już mielimy tę firmę TERAZ?)
                if company_obj.id in ids_in_this_campaign:
                    # print(f"      🔹 {domain}: Już jest w bieżącej kampanii.")
                    continue

                # B. GLOBALNE SPRAWDZENIE KARENCJI (Czy wysłano maila niedawno?)
                # Szukamy ostatniego wysłanego maila do tej firmy (z dowolnej kampanii/klienta)
                last_contact = session.query(Lead).filter(
                    Lead.global_company_id == company_obj.id,
                    Lead.status == "SENT"
                ).order_by(desc(Lead.sent_at)).first()

                if last_contact and last_contact.sent_at:
                    # Obliczamy ile dni minęło
                    days_since = (datetime.now() - last_contact.sent_at).days
                    
                    if days_since < GLOBAL_CONTACT_COOLDOWN:
                        print(f"      ⏳ {domain}: KARENCJA (Kontakt {days_since} dni temu). Pomijam.")
                        continue
                    else:
                        print(f"      ♻️ {domain}: RECYKLING (Kontakt > {GLOBAL_CONTACT_COOLDOWN} dni). Dodaję ponownie!")
                
                # Jeśli przeszliśmy tutaj -> Dodajemy Leada (Jako NEW)
                score = company_obj.quality_score if company_obj.quality_score else 50
                
                new_lead = Lead(
                    campaign_id=campaign_id,
                    global_company_id=company_obj.id,
                    status="NEW",
                    ai_confidence_score=score
                )
                new_leads_to_add.append(new_lead)
                
                # Blokujemy dodanie tego samego w tej samej pętli
                ids_in_this_campaign.add(company_obj.id) 
                total_added += 1

            if new_leads_to_add:
                session.add_all(new_leads_to_add)
                session.commit()
                print(f"      💾 Dodano {len(new_leads_to_add)} nowych leadów (w tym z recyklingu).")
            else:
                print("      💨 Brak nowych szans (tylko duplikaty lub karencja).")

        except Exception as e:
            session.rollback()
            print(f"      ❌ Błąd w Async Scout: {e}")
            await asyncio.sleep(1)

    print(f"🏁 [SCOUT] Koniec tury. Wynik: {total_added}/{SAFETY_LIMIT_LEADS}")
    return total_added