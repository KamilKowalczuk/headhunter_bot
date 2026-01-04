import asyncio
import logging
import sys
import os
import traceback
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Dict, Set

from sqlalchemy.orm import Session
from sqlalchemy import func
from rich.console import Console
import random 
from app.agents.sender import send_email_via_smtp
from app.backup_manager import backup_manager

# --- KONFIGURACJA LOGOWANIA (ENTERPRISE) ---
# Tworzymy logi w pliku engine.log z rotacją (max 5MB, 3 backupy)
LOG_FILE = "engine.log"

# Konfiguracja głównego loggera
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
file_handler.setLevel(logging.INFO)

# Logger konsolowy (tylko błędy i kluczowe info)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
console_handler.setLevel(logging.WARNING) 

# Setup root logger
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger("nexus_engine")

# Wyciszenie bibliotek (żeby nie śmieciły w konsoli, ale były w pliku)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("apify_client").setLevel(logging.WARNING)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
console = Console()

# Importy z aplikacji
from app.database import engine, Client, Lead, Campaign
from app.agents.scout import run_scout_async
from app.agents.strategy import generate_strategy
from app.agents.researcher import analyze_lead_async
from app.agents.writer import generate_email
from app.scheduler import process_followups, save_draft_via_imap
from app.agents.inbox import check_inbox
from app.warmup import calculate_daily_limit 

# --- KONFIGURACJA SKALOWANIA ---
MAX_CONCURRENT_AGENTS = 20  
DISPATCHER_INTERVAL = 5     

# --- POMOCNICZE FUNKCJE ---

def get_today_progress(session, client):
    """Zwraca liczbę maili wysłanych dzisiaj PRZEZ TEGO KONKRETNEGO KLIENTA."""
    today = datetime.now().date()
    sent_count = session.query(Lead).join(Campaign).filter(
        Campaign.client_id == client.id,
        Lead.status == "SENT",
        func.date(Lead.sent_at) == today
    ).count()
    return sent_count

async def run_client_cycle(client_id: int, semaphore: asyncio.Semaphore):
    """
    JEDEN OBRÓT KOŁA ZAMACHOWEGO (Worker).
    Zabezpieczony semaforem i globalnym try-except.
    """
    async with semaphore:
        session = Session(engine)
        client_name = f"Client_{client_id}"
        
        try:
            # 1. WERYFIKACJA STATUSU
            client = session.query(Client).filter(Client.id == client_id).first()
            if not client or client.status != "ACTIVE":
                return False
            
            client_name = client.name # Update name for logging

            # 2. SPRAWDZENIE LIMITÓW
            limit = calculate_daily_limit(client)
            done_today = get_today_progress(session, client)
            
            if done_today >= limit:
                return False

            # ---------------------------------------------------------
            # FAZA 0: HIGIENA
            # ---------------------------------------------------------
            await asyncio.to_thread(check_inbox, session, client)
            await asyncio.to_thread(process_followups, session, client)

            # ---------------------------------------------------------
            # FAZA 1: EGZEKUCJA (Konsumpcja)
            # ---------------------------------------------------------

            # C. WYSYŁKA / DRAFTOWANIE
            draft = session.query(Lead).join(Campaign).filter(
                Campaign.client_id == client.id, 
                Lead.status == "DRAFTED"
            ).first()
            
            if draft:
                mode = getattr(client, "sending_mode", "DRAFT")
                
                if mode == "AUTO":
                    console.print(f"[bold green]🚀 {client.name}:[/bold green] WYSYŁAM (AUTO) do {draft.company.name}...")
                    await asyncio.sleep(random.randint(3, 10))
                    
                    success = await asyncio.to_thread(send_email_via_smtp, draft, client)
                    
                    if success:
                        draft.status = "SENT"
                        draft.sent_at = datetime.now()
                        session.commit()
                        logger.info(f"[{client.name}] SENT email to {draft.company.name}")
                        
                        wait_time = random.randint(60, 300) 
                        console.print(f"   ☕ {client.name}: Przerwa {wait_time}s")
                        await asyncio.sleep(wait_time) 
                    else:
                        logger.error(f"[{client.name}] SMTP Error for {draft.company.name}")
                else:
                    console.print(f"[green]💾 {client.name}:[/green] Zapisuję draft...")
                    success, info = await asyncio.to_thread(save_draft_via_imap, draft, client)
                    if success:
                        draft.status = "SENT"
                        draft.sent_at = datetime.now()
                        session.commit()
                        logger.info(f"[{client.name}] DRAFT SAVED for {draft.company.name}")
                return True

            # D. PISANIE
            analyzed = session.query(Lead).join(Campaign).filter(
                Campaign.client_id == client.id, 
                Lead.status == "ANALYZED"
            ).first()

            if analyzed:
                console.print(f"[cyan]✍️  {client.name}:[/cyan] Piszę maila do {analyzed.company.name}...")
                await asyncio.to_thread(generate_email, session, analyzed.id)
                return True

            # ---------------------------------------------------------
            # FAZA 2: ZASILANIE (Akwizycja)
            # ---------------------------------------------------------

            # E. RESEARCH (Używamy wersji Async Wrapper)
            new_lead = session.query(Lead).join(Campaign).filter(
                Campaign.client_id == client.id, 
                Lead.status == "NEW"
            ).first()

            if new_lead:
                console.print(f"[blue]🔬 {client.name}:[/blue] Analizuję {new_lead.company.domain}...")
                await analyze_lead_async(session, new_lead.id) # <--- ZMIANA NA ASYNC WRAPPER
                return True

            # F. SCOUTING
            campaign = session.query(Campaign).filter(
                Campaign.client_id == client.id, 
                Campaign.status == "ACTIVE"
            ).order_by(Campaign.id.desc()).first()

            if campaign:
                # Ograniczamy częstotliwość scoutingu (np. raz na 10 cykli jeśli pusto)
                if random.random() < 0.2: 
                     console.print(f"[bold red]🕵️ {client.name}:[/bold red] Sprawdzam strategię...")
                     strategy = await asyncio.to_thread(generate_strategy, client, campaign.strategy_prompt, campaign.id)
                     if strategy and hasattr(strategy, 'search_queries') and strategy.search_queries:
                        strategy.search_queries = strategy.search_queries[:2]
                        await run_scout_async(session, campaign.id, strategy)
                        return True
            
            return False

        except Exception as e:
            logger.error(f"💥 WORKER ERROR [{client_name}]: {str(e)}", exc_info=True)
            return False
        finally:
            session.close()

async def nexus_core_loop():
    """
    RDZEŃ SYSTEMU (Wcześniej main).
    """
    console.clear()
    console.rule("[bold magenta]⚡ NEXUS ENGINE: HARDENED CORE v2[/bold magenta]")
    logger.info("System startup. Max Workers: %s", MAX_CONCURRENT_AGENTS)

    active_tasks: Dict[int, asyncio.Task] = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    while True:
        try:
            # 1. Pobieramy listę aktywnych klientów
            with Session(engine) as session:
                active_clients = session.query(Client.id, Client.name).filter(Client.status == "ACTIVE").all()
                active_client_ids = {c.id for c in active_clients}

            # 2. Sprzątanie
            for cid in list(active_tasks.keys()):
                task = active_tasks[cid]
                if task.done():
                    if task.exception():
                        logger.error(f"Task for Client {cid} crashed: {task.exception()}")
                    del active_tasks[cid]

            # 3. Anulowanie nieaktywnych
            for cid in list(active_tasks.keys()):
                if cid not in active_client_ids:
                    active_tasks[cid].cancel()
                    del active_tasks[cid]

            # 4. Spawn nowych zadań
            spawned_count = 0
            for cid in active_client_ids:
                if cid not in active_tasks:
                    task = asyncio.create_task(run_client_cycle(cid, semaphore))
                    active_tasks[cid] = task
                    spawned_count += 1
            
            if spawned_count > 0:
                logger.info(f"Dispatcher spawned {spawned_count} new tasks. Active: {len(active_tasks)}")

            await asyncio.sleep(DISPATCHER_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.critical(f"🔥 DISPATCHER LOOP ERROR: {e}", exc_info=True)
            await asyncio.sleep(5) # Krótka pauza przed retry pętli


# --- KONFIGURACJA BACKUPU ---
BACKUP_INTERVAL_SECONDS = 6 * 3600  # Co 6 godzin

async def nexus_core_loop():
    """
    RDZEŃ SYSTEMU (Wcześniej main).
    """
    console.clear()
    console.rule("[bold magenta]⚡ NEXUS ENGINE: HARDENED CORE v2.1[/bold magenta]")
    logger.info("System startup. Max Workers: %s", MAX_CONCURRENT_AGENTS)

    active_tasks: Dict[int, asyncio.Task] = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)
    
    # Zmienna do śledzenia ostatniego backupu
    last_backup_time = datetime.now()
    
    # Wykonaj pierwszy backup przy starcie (bezpieczeństwo)
    logger.info("💾 Uruchamiam backup startowy...")
    await asyncio.to_thread(backup_manager.perform_backup)

    while True:
        try:
            # --- SEKCJA BACKUPU ---
            now = datetime.now()
            if (now - last_backup_time).total_seconds() > BACKUP_INTERVAL_SECONDS:
                logger.info("💾 Czas na cykliczny backup...")
                # Uruchamiamy w wątku, żeby nie blokować workerów
                await asyncio.to_thread(backup_manager.perform_backup)
                last_backup_time = now
            # ----------------------

            # 1. Pobieramy listę aktywnych klientów
            with Session(engine) as session:
                active_clients = session.query(Client.id, Client.name).filter(Client.status == "ACTIVE").all()
                active_client_ids = {c.id for c in active_clients}

            # 2. Sprzątanie
            for cid in list(active_tasks.keys()):
                task = active_tasks[cid]
                if task.done():
                    if task.exception():
                        logger.error(f"Task for Client {cid} crashed: {task.exception()}")
                    del active_tasks[cid]

            # 3. Anulowanie nieaktywnych
            for cid in list(active_tasks.keys()):
                if cid not in active_client_ids:
                    active_tasks[cid].cancel()
                    del active_tasks[cid]

            # 4. Spawn nowych zadań
            spawned_count = 0
            for cid in active_client_ids:
                if cid not in active_tasks:
                    task = asyncio.create_task(run_client_cycle(cid, semaphore))
                    active_tasks[cid] = task
                    spawned_count += 1
            
            if spawned_count > 0:
                logger.info(f"Dispatcher spawned {spawned_count} new tasks. Active: {len(active_tasks)}")

            await asyncio.sleep(DISPATCHER_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.critical(f"🔥 DISPATCHER LOOP ERROR: {e}", exc_info=True)
            await asyncio.sleep(5)

async def run_forever():
    """
    WATCHDOG: Nieśmiertelna pętla restartująca system w razie krytycznej awarii.
    """
    while True:
        try:
            await nexus_core_loop()
        except KeyboardInterrupt:
            console.print("\n[bold red]🛑 Zatrzymano silnik NEXUS (Manual Stop).[/bold red]")
            break
        except Exception as e:
            console.print(f"[bold red]💀 CRITICAL SYSTEM CRASH: {e}[/bold red]")
            logger.critical("SYSTEM CRASHED. RESTARTING IN 10s...", exc_info=True)
            await asyncio.sleep(10)
            console.print("[bold green]♻️  SYSTEM REBOOT...[/bold green]")

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        pass