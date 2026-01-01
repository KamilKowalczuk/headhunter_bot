import sys
import os
import time
from sqlalchemy.orm import Session
from rich.console import Console

# Importy z naszej aplikacji
from app.database import engine, Client, Campaign, Lead
from app.agents.strategy import generate_strategy
from app.agents.scout import run_scout
from app.agents.researcher import analyze_lead
from app.agents.writer import generate_email
from app.scheduler import save_draft_via_imap # Importujemy funkcję zapisu

console = Console()

def main():
    session = Session(engine)
    
    try:
        # 1. KONFIGURACJA POCZĄTKOWA
        # Upewnij się, że nazwa klienta pasuje do tej w NocoDB!
        CLIENT_NAME = "Agencja"  
        
        # Cel testowy - zmień jeśli chcesz
        INTENT = "Znajdź agencje marketingowe w Warszawie, które mogą potrzebować automatyzacji raportowania."

        console.rule("[bold red]🚀 AGENCY OS: URUCHAMIAM PEŁNĄ SEKWENCJĘ[/bold red]")

        # --- A. POBIERANIE KLIENTA ---
        client = session.query(Client).filter(Client.name == CLIENT_NAME).first()
        if not client:
            console.print(f"[bold red]❌ BŁĄD: Nie znaleziono klienta '{CLIENT_NAME}' w bazie![/bold red]")
            return

        # --- B. TWORZENIE KAMPANII ---
        CAMPAIGN_NAME = f"Test Full Flow {int(time.time())}" # Unikalna nazwa
        console.print(f"[yellow]Tworzę nową kampanię: {CAMPAIGN_NAME}[/yellow]")
        
        campaign = Campaign(
            client_id=client.id,
            name=CAMPAIGN_NAME,
            status="ACTIVE",
            strategy_prompt=INTENT
        )
        session.add(campaign)
        session.commit()
        session.refresh(campaign)

        # --- ETAP 1: STRATEGIA ---
        console.rule("[magenta]ETAP 1: STRATEGIA (MÓZG)[/magenta]")
        console.print(f"[dim]Cel: {INTENT}[/dim]")
        
        strategy = generate_strategy(client, INTENT)
        
        # Zapis do bazy
        keywords_text = ", ".join(strategy.search_queries)
        campaign.strategy_prompt = f"CEL: {INTENT}\n\nKEYWORDS: {keywords_text}"
        session.commit()
        
        console.print(f"🧠 Strateg wymyślił {len(strategy.search_queries)} zapytań.")
        console.print(f"👉 Przykłady: {strategy.search_queries[:3]}")

        # --- ETAP 2: ZWIAD (SCOUT) ---
        console.rule("[cyan]ETAP 2: ZWIAD (OCZY)[/cyan]")
        
        # OGRANICZENIE DLA TESTU: Bierzemy tylko 2 pierwsze frazy i limit 3 firm na frazę
        # Żeby nie czekać wieki i nie spalić limitów
        strategy.search_queries = strategy.search_queries[:2]
        
        # Nadpisujemy funkcję scouta żeby brała mały limit (jeśli obsługuje)
        # W run_scout mamy hardcoded limit=5, to jest OK na test.
        
        new_leads_count = run_scout(session, campaign.id, strategy)
        console.print(f"[bold green]✅ Znaleziono {new_leads_count} surowych leadów.[/bold green]")

        if new_leads_count == 0:
            console.print("[red]Brak leadów, przerywam proces.[/red]")
            return

        # --- ETAP 3: BADACZ (RESEARCHER) ---
        console.rule("[blue]ETAP 3: BADACZ (ANALIZA & EMAILE)[/blue]")
        
        # Pobieramy leady z tej konkretnej kampanii
        leads_to_analyze = session.query(Lead).filter(
            Lead.campaign_id == campaign.id, 
            Lead.status == "NEW"
        ).all()

        for i, lead in enumerate(leads_to_analyze):
            console.print(f"\n[dim]({i+1}/{len(leads_to_analyze)}) Analizuję: {lead.company.domain}...[/dim]")
            analyze_lead(session, lead.id)

        # --- ETAP 4: PISARZ (WRITER) ---
        console.rule("[yellow]ETAP 4: PISARZ (DRAFTY)[/yellow]")
        
        # Pobieramy tylko te, które przeszły analizę (ANALYZED)
        leads_to_write = session.query(Lead).filter(
            Lead.campaign_id == campaign.id, 
            Lead.status == "ANALYZED"
        ).all()

        if not leads_to_write:
            console.print("[red]Żaden lead nie przeszedł analizy (brak maili lub nie są firmami).[/red]")
        else:
            for lead in leads_to_write:
                console.print(f"\n[dim]Piszę dla: {lead.company.name}...[/dim]")
                generate_email(session, lead.id)

        # --- ETAP 5: SCHEDULER (ZAPIS DO IMAP) ---
        console.rule("[green]ETAP 5: WYSYŁKA (ZAPIS DO DRAFTÓW)[/green]")
        
        leads_to_save = session.query(Lead).filter(
            Lead.campaign_id == campaign.id, 
            Lead.status == "DRAFTED"
        ).all()

        if not leads_to_save:
            console.print("[yellow]Brak gotowych draftów do zapisu.[/yellow]")
        else:
            for lead in leads_to_save:
                console.print(f"📥 Zapisuję draft dla: {lead.company.name} ({lead.target_email})...")
                success, info = save_draft_via_imap(lead, client)
                
                if success:
                    lead.status = "SAVED_AS_DRAFT"
                    console.print(f"   ✅ [bold green]SUKCES:[/bold green] {info}")
                else:
                    console.print(f"   ❌ [bold red]BŁĄD IMAP:[/bold red] {info}")
                
                session.commit()

        # PODSUMOWANIE
        console.rule("[bold white]PODSUMOWANIE[/bold white]")
        console.print(f"Kampania: {CAMPAIGN_NAME}")
        console.print("Sprawdź folder 'Wersje robocze' na swojej skrzynce pocztowej!")

    except Exception as e:
        console.print(f"[bold red]KRYTYCZNY BŁĄD PROCESU:[/bold red] {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        session.close()

if __name__ == "__main__":
    main()