import sys
import os

# Magia ścieżek
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import imaplib
import time
import mimetypes
import random
from datetime import datetime, timedelta
from email.message import EmailMessage
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import engine, Lead, Client
from rich.console import Console

console = Console()

def save_draft_via_imap(lead: Lead, client: Client):
    """
    Zapisuje draft z załącznikiem (PDF/DOCX) jeśli jest skonfigurowany.
    """
    msg = EmailMessage()
    msg["Subject"] = lead.generated_email_subject
    msg["From"] = f"{client.sender_name} <{client.smtp_user}>"
    
    if lead.target_email:
        msg["To"] = lead.target_email
    else:
        return False, "Brak adresu email (target_email)."
    
    msg.set_content("Wersja tekstowa maila.")
    msg.add_alternative(lead.generated_email_body, subtype="html")

    # --- OBSŁUGA ZAŁĄCZNIKA (NAPRAWIONA NAZWA ZMIENNEJ) ---
    if client.attachment_filename:
        # Zakładamy, że pliki są w folderze 'files' w głównym katalogu
        file_path = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'files', client.attachment_filename)
        
        if os.path.exists(file_path):
            # Odgadnij typ pliku
            ctype, encoding = mimetypes.guess_type(file_path)
            if ctype is None or encoding is not None:
                ctype = 'application/octet-stream'
            
            maintype, subtype = ctype.split('/', 1)

            with open(file_path, 'rb') as f:
                file_data = f.read()
                msg.add_attachment(
                    file_data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=client.attachment_filename
                )
        else:
            console.print(f"[bold red]⚠️ BŁĄD ZAŁĄCZNIKA:[/bold red] Nie znaleziono pliku '{client.attachment_filename}' w folderze files/!")
    # -------------------------------

    try:
        if not client.imap_server: return False, "Brak IMAP"
        mail = imaplib.IMAP4_SSL(client.imap_server, client.imap_port or 993)
        mail.login(client.smtp_user, client.smtp_password)
        
        selected_folder = "Drafts"
        folders_to_try = ["[Gmail]/Drafts", "Drafts", "Draft", "Wersje robocze"]
        try:
            status, folder_list_raw = mail.list()
            f_list = str(folder_list_raw)
            for f in folders_to_try:
                if f in f_list or f.replace("/", "&") in f_list: 
                    selected_folder = f
                    break
        except: pass

        mail.append(selected_folder, '(\\Draft \\Seen)', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        mail.logout()
        # Informacja zwrotna o załączniku
        att_info = f"(+ {client.attachment_filename})" if client.attachment_filename else ""
        return True, f"Zapisano w: {selected_folder} {att_info}"
    except Exception as e:
        return False, str(e)


def process_followups(session: Session, client: Client):
    """
    Logika Drip: Znajdź leady, które milczą i cofnij je do Pisarza.
    """
    # KONFIGURACJA CZASU (W produkcji: days=3. W teście: minutes=5)
    DELAY_TIME = timedelta(minutes=5) 
    
    now = datetime.utcnow()
    
    pending_followups = session.query(Lead).join(Lead.campaign).filter(
        Lead.campaign.has(client_id=client.id),
        Lead.status == "SENT",
        Lead.step_number < 3,
        (Lead.replied_at == None)
    ).all()
    
    if not pending_followups:
        return

    # console.print(f"⏳ Sprawdzam follow-upy dla {len(pending_followups)} wysłanych leadów...")

    for lead in pending_followups:
        last_action = lead.sent_at or lead.last_action_at
        if not last_action: continue

        time_passed = now - last_action
        
        if time_passed > DELAY_TIME:
            next_step = lead.step_number + 1
            console.print(f"   ⏰ CZAS NA FOLLOW-UP! {lead.company.name}. Wchodzimy na Krok {next_step}.")
            
            lead.status = "ANALYZED"
            lead.step_number = next_step
            lead.last_action_at = now
            lead.ai_analysis_summary = str(lead.ai_analysis_summary) + f" [UPDATE: Klient milczy po mailu nr {lead.step_number-1}. Czas na przypomnienie.]"
            session.commit()

def run_scheduler_cycle():
    """Główny cykl pracy"""
    session = Session(engine)
    clients = session.query(Client).filter(Client.status == "ACTIVE").all() 
    
    console.rule("[bold cyan]DRIP PROTOCOL: CYKL PRACY[/bold cyan]")
    
    for client in clients:
        process_followups(session, client)
        
        lead_to_process = session.query(Lead).join(Lead.campaign).filter(
            Lead.campaign.has(client_id=client.id),
            Lead.status == "DRAFTED"
        ).first()

        if lead_to_process:
            console.print(f"📥 Zapisuję draft (Krok {lead_to_process.step_number}): {lead_to_process.company.name}...")
            
            success, info = save_draft_via_imap(lead_to_process, client)

            if success:
                lead_to_process.status = "SENT"
                lead_to_process.sent_at = datetime.utcnow()
                lead_to_process.last_action_at = datetime.utcnow()
                console.print(f"   ✅ ZAPISANO DRAFT: {info}")
            else:
                console.print(f"   ❌ BŁĄD IMAP: {info}")
            
            session.commit()
            time.sleep(2)

    session.close()

if __name__ == "__main__":
    # Jeśli uruchamiamy plik bezpośrednio, rób pętlę
    while True:
        run_scheduler_cycle()
        time.sleep(30)