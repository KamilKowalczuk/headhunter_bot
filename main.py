import asyncio
import logging
import sys
import os
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from rich.console import Console

# Konfiguracja ścieżek i loggera
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO)
console = Console()

# Importy z aplikacji
from app.database import engine, Client, Lead, Campaign
from app.agents.scout import run_scout_async
from app.agents.strategy import generate_strategy
from app.agents.researcher import analyze_lead
from app.agents.writer import generate_email
from app.scheduler import process_followups, save_draft_via_imap
from app.agents.inbox import check_inbox

# --- POMOCNICZE FUNKCJE ---

def get_today_progress(session, client):
    """Zwraca liczbę maili wysłanych dzisiaj PRZEZ TEGO KONKRETNEGO KLIENTA."""
    # POPRAWKA DATY: Używamy daty serwera (lokalnej), żeby pasowało do bazy
    today = datetime.now().date()
    
    sent_count = session.query(Lead).join(Campaign).filter(
        Campaign.client_id == client.id, # <--- KLUCZOWE: Filtr po ID klienta
        Lead.status == "SENT",
        func.date(Lead.sent_at) == today
    ).count()
    return sent_count

async def run_client_cycle(client_id: int):
    """
    JEDEN OBRÓT KOŁA ZAMACHOWEGO.
    """
    session = Session(engine)
    
    try:
        # 1. WERYFIKACJA STATUSU
        client = session.query(Client).filter(Client.id == client_id).first()
        if not client or client.status != "ACTIVE":
            return False

        # 2. SPRAWDZENIE LIMITÓW
        limit = client.daily_limit or 50
        done_today = get_today_progress(session, client)
        
        # LOGOWANIE STANU (Teraz będziesz widział to w logach)
        console.print(f"[dim]📊 {client.name}: Postęp wysyłki {done_today}/{limit}[/dim]")

        if done_today >= limit:
            console.print(f"[dim]🛑 {client.name}: Limit wyczerpany na dziś ({done_today}/{limit}).[/dim]")
            return False

        # ---------------------------------------------------------
        # FAZA 0: HIGIENA
        # ---------------------------------------------------------
        await asyncio.to_thread(check_inbox, session, client)
        await asyncio.to_thread(process_followups, session, client)

        # ---------------------------------------------------------
        # FAZA 1: EGZEKUCJA (Konsumpcja)
        # ---------------------------------------------------------

        # C. WYSYŁKA
        draft = session.query(Lead).join(Campaign).filter(
            Campaign.client_id == client.id, 
            Lead.status == "DRAFTED"
        ).first()
        
        if draft:
            console.print(f"[green]🚀 {client.name}:[/green] Wysyłam draft do {draft.company.name}...")
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

        # F. SCOUTING (Scout - Ostateczność)
        campaign = session.query(Campaign).filter(
            Campaign.client_id == client.id, 
            Campaign.status == "ACTIVE"
        ).order_by(Campaign.id.desc()).first()

        if campaign:
            console.print(f"[bold red]🕵️ {client.name}:[/bold red] PUSTY LEJEK! Generuję strategię...")
            
            # Generowanie strategii
            strategy = await asyncio.to_thread(generate_strategy, client, campaign.strategy_prompt, campaign.id)
            
            # --- FIX: ZABEZPIECZENIE PRZED NoneType ---
            # Sprawdzamy czy strategy istnieje ORAZ czy search_queries to lista (i nie jest None)
            if strategy and hasattr(strategy, 'search_queries') and strategy.search_queries:
                # Bierzemy max 2 zapytania
                strategy.search_queries = strategy.search_queries[:2]
                
                console.print(f"[yellow]   🔍 Cele: {strategy.search_queries}[/yellow]")
                await run_scout_async(session, campaign.id, strategy)
                return True
            else:
                console.print(f"[red]⚠️ {client.name}:[/red] AI zwróciło pustą strategię. Próbuję ponownie za chwilę.")
                # Nie zwracamy błędu, tylko False, żeby system spróbował w następnym cyklu
                return False
        else:
            console.print(f"[red]❌ {client.name}:[/red] Brak aktywnej kampanii (celu).")
            return False

    except Exception as e:
        # Dodajemy pełny zrzut błędu, żeby łatwiej debugować
        import traceback
        console.print(f"[bold red]💥 BŁĄD KRYTYCZNY KLIENTA {client_id}: {e}[/bold red]")
        # console.print(traceback.format_exc()) # Odkomentuj jeśli chcesz widzieć pełny stos błędów
        return False
    finally:
        session.close()

async def main():
    """Główna pętla zarządcza."""
    console.clear()
    console.rule("[bold magenta]⚡ NEXUS ENGINE: AUTONOMOUS CORE[/bold magenta]")
    console.print("[dim]System działa. Wymuszam flush logów.[/dim]\n")

    while True:
        session = Session(engine)
        active_clients = session.query(Client).filter(Client.status == "ACTIVE").all()
        active_client_ids = [c.id for c in active_clients]
        session.close()

        if not active_client_ids:
            console.print("[yellow]💤 Wszyscy agenci uśpieni (PAUSED). Czekam 10s...[/yellow]")
            await asyncio.sleep(10)
            continue

        any_action_global = False

        for client_id in active_client_ids:
            result = await run_client_cycle(client_id)
            if result:
                any_action_global = True

        if not any_action_global:
            console.print("[dim]💤 Brak zadań / Limity wyczerpane. Pauza 30s...[/dim]")
            await asyncio.sleep(30)
        else:
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        # Wymuszenie flushowania stdout dla Windows/Linux (KLUCZOWE DLA LOGÓW LIVE)
        sys.stdout.reconfigure(line_buffering=True)
        
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold red]🛑 Zatrzymano silnik NEXUS.[/bold red]")