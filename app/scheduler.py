import sys
import os
import imaplib
import time
import mimetypes
from datetime import datetime, timedelta
from email.message import EmailMessage
from sqlalchemy.orm import Session
from app.database import Lead, Client
from rich.console import Console

console = Console()

def save_draft_via_imap(lead: Lead, client: Client):
    """
    Zapisuje draft z załącznikiem (PDF/DOCX) na serwerze IMAP.
    Używane przez main.py.
    """
    msg = EmailMessage()
    msg["Subject"] = lead.generated_email_subject
    msg["From"] = f"{client.sender_name} <{client.smtp_user}>"
    
    if lead.target_email:
        msg["To"] = lead.target_email
    else:
        return False, "Brak adresu email."
    
    # --- SKLEJANIE TREŚCI ZE STOPKĄ (NOWOŚĆ) ---
    final_html_body = lead.generated_email_body
    
    if client.html_footer:
        # Doklejamy stopkę HTML (dodajemy <br> dla odstępu)
        final_html_body += f"<br><br>{client.html_footer}"
    
    # Wersja tekstowa (AI generuje HTML, więc usuwamy tagi dla wersji plain text lub zostawiamy prosto)
    # Tu uproszczenie: wersja tekstowa to po prostu info
    msg.set_content("Twój klient poczty nie obsługuje HTML. Zobacz wersję sformatowaną.")
    
    # Wersja HTML (główna)
    msg.add_alternative(final_html_body, subtype="html")

    # --- OBSŁUGA ZAŁĄCZNIKA ---
    if client.attachment_filename:
        # Szukamy w folderze files/ o poziom wyżej od app/
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, 'files', client.attachment_filename)
        
        if os.path.exists(file_path):
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
            console.print(f"[bold red]⚠️ BŁĄD ZAŁĄCZNIKA:[/bold red] Nie znaleziono pliku '{client.attachment_filename}'")
    # -------------------------------

    try:
        if not client.imap_server: return False, "Brak konfiguracji IMAP"
        
        # Łączenie z IMAP
        mail = imaplib.IMAP4_SSL(client.imap_server, client.imap_port or 993)
        mail.login(client.smtp_user, client.smtp_password)
        
        # Wybór folderu Drafts
        selected_folder = "Drafts"
        folders_to_try = ["[Gmail]/Drafts", "Drafts", "Draft", "Wersje robocze", "INBOX.Drafts"]
        try:
            status, folder_list_raw = mail.list()
            f_list = str(folder_list_raw)
            for f in folders_to_try:
                if f in f_list or f.replace("/", "&") in f_list: 
                    selected_folder = f
                    break
        except: pass

        # Zapis draftu
        mail.append(selected_folder, '(\\Draft \\Seen)', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        mail.logout()
        
        att_info = f"(+ {client.attachment_filename})" if client.attachment_filename else ""
        return True, f"Zapisano w folderze: {selected_folder} {att_info}"
    except Exception as e:
        return False, str(e)


def process_followups(session: Session, client: Client):
    """
    Logika Drip: Przesuwa leady do kolejnego kroku, jeśli minął czas.
    Używane przez main.py.
    """
    # KONFIGURACJA CZASU (Dla testów: minuty. Dla produkcji: dni)
    # Zmień na timedelta(days=3) w produkcji!
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

    for lead in pending_followups:
        last_action = lead.sent_at or lead.last_action_at
        if not last_action: continue

        time_passed = now - last_action
        
        if time_passed > DELAY_TIME:
            next_step = lead.step_number + 1
            console.print(f"   ⏰ [DRIP] {lead.company.name}: Czas na krok {next_step}.")
            
            lead.status = "ANALYZED" # Status ANALYZED wyzwala Writera w main.py
            lead.step_number = next_step
            lead.last_action_at = now
            
            # Dodajemy notatkę dla AI, żeby wiedziało, że to przypomnienie
            summary = lead.ai_analysis_summary or ""
            lead.ai_analysis_summary = summary + f"\n[SYSTEM UPDATE]: Klient nie odpisał na maila nr {next_step-1}. Napisz krótkie przypomnienie."
            
            session.commit()