import time
import sys
import os
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from rich.console import Console
from rich.table import Table

# Importy
from app.database import engine, Client, Lead, Campaign
from app.agents.scout import run_scout
from app.agents.strategy import generate_strategy
from app.agents.researcher import analyze_lead
from app.agents.writer import generate_email
from app.scheduler import process_followups, save_draft_via_imap

console = Console()

def get_today_progress(session, client):
    """Licznik: Ile maili wysłano dzisiaj."""
    today = datetime.utcnow().date()
    sent_count = session.query(Lead).join(Campaign).filter(
        Campaign.client_id == client.id,
        Lead.status == "SENT",
        func.date(Lead.sent_at) == today
    ).count()
    return sent_count

def run_autobot_logic():
    console.clear()
    console.rule("[bold magenta]🤖 TITAN AUTOBOT: ENTERPRISE ENGINE[/bold magenta]")
    console.print("[dim]System działa w trybie ciągłym. Użyj Ctrl+C aby zatrzymać.[/dim]\n")
    
    while True:
        session = Session(engine)
        clients = session.query(Client).filter(Client.status == "ACTIVE").all()
        
        if not clients:
            console.print("[yellow]💤 Brak aktywnych agentów. Czekam 60s...[/yellow]")
            session.close()
            time.sleep(60)
            continue

        any_action_taken = False # Flaga: czy w tym cyklu cokolwiek zrobiliśmy?

        for client in clients:
            limit = client.daily_limit or 50
            done_today = get_today_progress(session, client)
            remaining_quota = limit - done_today
            
            # --- ZASADA 1: LIMIT ---
            if remaining_quota <= 0:
                continue # Ten klient ma wolne na dziś

            # --- ZASADA 2: PRIORYTETY (Pull System) ---
            # Sprawdzamy, co jest do zrobienia, od końca lejka
            
            # A. WYSYŁKA (Priorytet najwyższy - domykamy sprzedaż)
            draft = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "DRAFTED").first()
            if draft:
                console.print(f"[green]🚀 {client.name}:[/green] Wysyłam draft do {draft.company.name}...")
                save_draft_via_imap(draft, client)
                draft.status = "SENT"
                draft.sent_at = datetime.utcnow()
                session.commit()
                any_action_taken = True
                continue # Zrobiliśmy akcję dla tego klienta, idziemy do następnego (Round Robin)

            # B. PISANIE (Priorytet średni)
            analyzed = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "ANALYZED").first()
            if analyzed:
                console.print(f"[cyan]✍️  {client.name}:[/cyan] Piszę maila do {analyzed.company.name}...")
                generate_email(session, analyzed.id)
                any_action_taken = True
                continue

            # C. ANALIZA (Priorytet niski)
            new_lead = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "NEW").first()
            if new_lead:
                console.print(f"[blue]🔬 {client.name}:[/blue] Analizuję {new_lead.company.domain}...")
                analyze_lead(session, new_lead.id)
                any_action_taken = True
                continue

            # D. ZASILANIE (Alarm! Pusty lejek)
            # Jeśli doszliśmy tutaj, to znaczy, że lejek jest PUSTY.
            console.print(f"[bold red]🕵️ {client.name}:[/bold red] PUSTO! Uruchamiam Scouta (Apify)...")
            
            # Pobierz aktywną kampanię
            campaign = session.query(Campaign).filter(Campaign.client_id == client.id, Campaign.status == "ACTIVE").order_by(Campaign.id.desc()).first()
            
            if campaign:
                # --- ZMIANA: Przekazujemy campaign.id do strategii ---
                strategy = generate_strategy(client, campaign.strategy_prompt, campaign.id)
                
                # Ograniczamy do 2 zapytań na cykl (żeby nie spalić wszystkich dzielnic na raz)
                strategy.search_queries = strategy.search_queries[:2] 
                
                console.print(f"[yellow]   🔍 Nowe cele: {strategy.search_queries}[/yellow]")
                run_scout(session, campaign.id, strategy)
                any_action_taken = True
            else:
                console.print(f"[red]❌ {client.name}:[/red] Brak aktywnej kampanii. Nie mogę szukać.")

        session.close()

        # Jeśli w całym cyklu (dla wszystkich klientów) nic się nie działo, robimy pauzę
        if not any_action_taken:
            console.print("[dim]💤 Wszyscy obsłużeni lub limity wyczerpane. Czekam 60s...[/dim]")
            time.sleep(60)
        else:
            # Krótka pauza techniczna między cyklami round-robin
            time.sleep(2)

if __name__ == "__main__":
    try:
        run_autobot_logic()
    except KeyboardInterrupt:
        console.print("Zatrzymano.")