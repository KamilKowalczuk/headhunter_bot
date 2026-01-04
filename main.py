import asyncio
import logging
import sys
import os
from datetime import datetime
from typing import Dict, Set

from sqlalchemy.orm import Session
from sqlalchemy import func
from rich.console import Console
import random 
from app.agents.sender import send_email_via_smtp 

# Konfiguracja ścieżek i loggera
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# Zmieniamy poziom logowania na WARNING dla bibliotek, żeby nie śmiecić przy 1000 wątkach
logging.basicConfig(level=logging.WARNING) 
logger = logging.getLogger("nexus_engine")
logger.setLevel(logging.INFO)

console = Console()

# Importy z aplikacji
from app.database import engine, Client, Lead, Campaign
from app.agents.scout import run_scout_async
from app.agents.strategy import generate_strategy
from app.agents.researcher import analyze_lead
from app.agents.writer import generate_email
from app.scheduler import process_followups, save_draft_via_imap
from app.agents.inbox import check_inbox
from app.warmup import calculate_daily_limit 

# --- KONFIGURACJA SKALOWANIA ---
MAX_CONCURRENT_AGENTS = 20  # <--- LIMIT RÓWNOLEGŁYCH WORKERÓW (Chroni DB przed "Too many clients")
DISPATCHER_INTERVAL = 5     # Co ile sekund sprawdzać nowe zadania

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
    Zabezpieczony semaforem, aby nie przeciążyć bazy danych.
    """
    async with semaphore:
        # Tworzymy sesję tylko na czas wykonania zadania
        # Używamy to_thread dla operacji DB, jeśli to możliwe, lub krótkich sesji
        session = Session(engine)
        
        try:
            # 1. WERYFIKACJA STATUSU
            # Pobieramy klienta wewnątrz wątku/sesji
            client = session.query(Client).filter(Client.id == client_id).first()
            if not client or client.status != "ACTIVE":
                return False

            # 2. SPRAWDZENIE LIMITÓW (WARM-UP LOGIC)
            limit = calculate_daily_limit(client)
            done_today = get_today_progress(session, client)
            
            # Logowanie stanu (tylko co jakiś czas lub przy zmianie, żeby nie spamować konsoli przy 1000 klientach)
            # Przy dużej skali logujemy tylko istotne zdarzenia
            limit_str = f"{limit}"
            if client.warmup_enabled and limit < (client.daily_limit or 50):
                limit_str += " (Warm-up 🔥)"
                
            # Zmniejszamy noise w logach - logujemy tylko jeśli coś robimy
            # console.print(f"[dim]📊 {client.name}: Postęp wysyłki {done_today}/{limit_str}[/dim]")

            if done_today >= limit:
                # console.print(f"[dim]🛑 {client.name}: Limit dzienny osiągnięty ({done_today}/{limit}).[/dim]")
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
                    
                    # Symulacja człowieka - to teraz nie blokuje innych klientów!
                    await asyncio.sleep(random.randint(3, 10))
                    
                    success = await asyncio.to_thread(send_email_via_smtp, draft, client)
                    
                    if success:
                        draft.status = "SENT"
                        draft.sent_at = datetime.now()
                        session.commit()
                        console.print(f"   ✅ {client.name}: Wysłano!")
                        
                        # === HUMAN JITTER ===
                        # Klient idzie na kawę, ale Worker zwalnia semafor? 
                        # NIE. Jeśli chcemy, żeby agent 'odpoczął', kończymy cykl i pozwalamy Dispatcherowi
                        # go nie podnosić przez chwilę, albo używamy sleep tutaj.
                        # Przy 1000 klientach lepiej zakończyć zadanie i pozwolić innym wejść.
                        # Ale dla zachowania "ciągłości" sesji człowieka:
                        wait_time = random.randint(60, 300) 
                        console.print(f"   ☕ {client.name}: Przerwa {wait_time}s")
                        await asyncio.sleep(wait_time) 
                        
                    else:
                        console.print(f"   ❌ {client.name}: Błąd SMTP.")
                
                else:
                    # TRYB DRAFT
                    console.print(f"[green]💾 {client.name}:[/green] Zapisuję draft...")
                    success, info = await asyncio.to_thread(save_draft_via_imap, draft, client)
                    if success:
                        draft.status = "SENT"
                        draft.sent_at = datetime.now()
                        session.commit()
                
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

            # E. RESEARCH
            new_lead = session.query(Lead).join(Campaign).filter(
                Campaign.client_id == client.id, 
                Lead.status == "NEW"
            ).first()

            if new_lead:
                console.print(f"[blue]🔬 {client.name}:[/blue] Analizuję {new_lead.company.domain}...")
                await asyncio.to_thread(analyze_lead, session, new_lead.id)
                return True

            # F. SCOUTING
            campaign = session.query(Campaign).filter(
                Campaign.client_id == client.id, 
                Campaign.status == "ACTIVE"
            ).order_by(Campaign.id.desc()).first()

            if campaign:
                # Sprawdzamy czy warto odpalać scouta (czy są inne leady)
                # Ograniczamy zapytania do scouta, to kosztuje
                if random.random() < 0.1: # 10% szansy na sprawdzenie scouta w pustym przebiegu
                     console.print(f"[bold red]🕵️ {client.name}:[/bold red] Sprawdzam strategię...")
                     strategy = await asyncio.to_thread(generate_strategy, client, campaign.strategy_prompt, campaign.id)
                     if strategy and hasattr(strategy, 'search_queries') and strategy.search_queries:
                        strategy.search_queries = strategy.search_queries[:2]
                        await run_scout_async(session, campaign.id, strategy)
                        return True
            
            return False

        except Exception as e:
            console.print(f"[bold red]💥 BŁĄD KLIENTA {client_id}: {e}[/bold red]")
            return False
        finally:
            session.close()

async def main():
    """
    GŁÓWNA PĘTLA ZARZĄDCZA (DISPATCHER).
    """
    console.clear()
    console.rule("[bold magenta]⚡ NEXUS ENGINE: HIGH-CONCURRENCY CORE[/bold magenta]")
    console.print(f"[dim]Start systemu. Max Workers: {MAX_CONCURRENT_AGENTS}[/dim]\n")

    # Śledzenie aktywnych zadań: {client_id: Task}
    active_tasks: Dict[int, asyncio.Task] = {}
    
    # Semafor ograniczający równoległe obciążenie
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    while True:
        try:
            # 1. Pobieramy listę aktywnych klientów (SZYBKI ODCZYT)
            # Używamy osobnej sesji tylko do pobrania ID
            with Session(engine) as session:
                active_clients = session.query(Client.id, Client.name).filter(Client.status == "ACTIVE").all()
                active_client_ids = {c.id for c in active_clients} # Set dla szybkiego wyszukiwania
                client_names = {c.id: c.name for c in active_clients}

            # 2. Sprzątanie zakończonych zadań
            # Tworzymy kopię kluczy, bo będziemy modyfikować słownik
            for cid in list(active_tasks.keys()):
                task = active_tasks[cid]
                if task.done():
                    # Jeśli zadanie rzuciło wyjątkiem, logujemy go
                    if task.exception():
                        console.print(f"[red]⚠️ Worker {cid} padł: {task.exception()}[/red]")
                    del active_tasks[cid]

            # 3. Anulowanie zadań klientów, którzy przestali być aktywni
            for cid in list(active_tasks.keys()):
                if cid not in active_client_ids:
                    console.print(f"[yellow]🛑 Zatrzymuję workera dla klienta ID: {cid}[/yellow]")
                    active_tasks[cid].cancel()
                    del active_tasks[cid]

            # 4. Uruchamianie nowych workerów dla bezczynnych klientów
            spawned_count = 0
            for cid in active_client_ids:
                if cid not in active_tasks:
                    # Tworzymy zadanie i wrzucamy do słownika
                    # Przekazujemy semafor do środka
                    task = asyncio.create_task(run_client_cycle(cid, semaphore))
                    active_tasks[cid] = task
                    spawned_count += 1
            
            # Raport stanu Dispatchera (tylko jeśli coś się dzieje)
            if spawned_count > 0 or len(active_tasks) < 5:
                console.print(f"[dim]🔄 Dispatcher: Aktywne zadania: {len(active_tasks)} | Oczekujące w semaforze: {max(0, len(active_tasks) - MAX_CONCURRENT_AGENTS)}[/dim]")

            # Dyspozytor śpi krótko, żeby szybko reagować
            await asyncio.sleep(DISPATCHER_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            console.print(f"[bold red]🔥 CRITICAL DISPATCHER ERROR: {e}[/bold red]")
            await asyncio.sleep(5)

    console.print("\n[bold red]🛑 Zatrzymano silnik NEXUS.[/bold red]")

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        pass