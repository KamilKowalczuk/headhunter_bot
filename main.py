import asyncio
import logging
import sys
import os
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from rich.console import Console
import random 
from app.agents.sender import send_email_via_smtp 

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
from app.warmup import calculate_daily_limit # <--- NOWY IMPORT

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

        # 2. SPRAWDZENIE LIMITÓW (WARM-UP LOGIC)
        # Obliczamy limit dynamicznie na podstawie stażu w warm-upie
        limit = calculate_daily_limit(client)
        done_today = get_today_progress(session, client)
        
        # Logowanie stanu z informacją o Warm-upie
        limit_str = f"{limit}"
        if client.warmup_enabled and limit < (client.daily_limit or 50):
            limit_str += " (Warm-up 🔥)"
            
        console.print(f"[dim]📊 {client.name}: Postęp wysyłki {done_today}/{limit_str}[/dim]")

        if done_today >= limit:
            console.print(f"[dim]🛑 {client.name}: Limit dzienny osiągnięty ({done_today}/{limit}).[/dim]")
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
            # SPRAWDZAMY TRYB WYSYŁKI
            mode = getattr(client, "sending_mode", "DRAFT")
            
            if mode == "AUTO":
                console.print(f"[bold green]🚀 {client.name}:[/bold green] WYSYŁAM (AUTO) do {draft.company.name}...")
                
                # Symulacja człowieka przed kliknięciem "Wyślij" (3-10 sekund "wahania")
                await asyncio.sleep(random.randint(3, 10))
                
                success = await asyncio.to_thread(send_email_via_smtp, draft, client)
                
                if success:
                    draft.status = "SENT"
                    draft.sent_at = datetime.now()
                    session.commit()
                    console.print(f"   ✅ Wysłano! Następny mail za chwilę...")
                    
                    # === HUMAN JITTER ===
                    # Po wysłaniu maila człowiek nie wysyła następnego natychmiast.
                    # Czeka od 2 do 8 minut.
                    wait_time = random.randint(120, 480) 
                    console.print(f"   ☕ Przerwa na kawę: {wait_time}s (Symulacja człowieka)")
                    await asyncio.sleep(wait_time)
                    
                else:
                    console.print(f"   ❌ Błąd wysyłki SMTP.")
            
            else:
                # TRYB DRAFT (Bezpieczny)
                console.print(f"[green]💾 {client.name}:[/green] Zapisuję draft (IMAP) dla {draft.company.name}...")
                success, info = await asyncio.to_thread(save_draft_via_imap, draft, client)
                if success:
                    draft.status = "SENT" # W trybie draft traktujemy zapisanie jako "obsłużenie"
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
            
            # Zabezpieczenie przed pustą strategią (NoneType fix)
            if strategy and hasattr(strategy, 'search_queries') and strategy.search_queries:
                strategy.search_queries = strategy.search_queries[:2]
                console.print(f"[yellow]   🔍 Cele: {strategy.search_queries}[/yellow]")
                await run_scout_async(session, campaign.id, strategy)
                return True
            else:
                console.print(f"[red]⚠️ {client.name}:[/red] AI nie wygenerowało fraz. Ponawiam w następnym cyklu.")
                return False
        else:
            console.print(f"[red]❌ {client.name}:[/red] Brak aktywnej kampanii (celu).")
            return False

    except Exception as e:
        console.print(f"[bold red]💥 BŁĄD KRYTYCZNY KLIENTA {client_id}: {e}[/bold red]")
        # import traceback
        # console.print(traceback.format_exc()) 
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
        sys.stdout.reconfigure(line_buffering=True)
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold red]🛑 Zatrzymano silnik NEXUS.[/bold red]")