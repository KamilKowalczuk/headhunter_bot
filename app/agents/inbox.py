import imaplib
import email
import os
from email.header import decode_header
from datetime import datetime
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

from app.database import engine, Lead, Client
from app.schemas import ReplyAnalysis

load_dotenv()

# Model AI do analizy sentymentu
analyst_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0,
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(ReplyAnalysis)

def decode_mime_words(s):
    """Pomocnik do dekodowania tematów maili"""
    return u''.join(
        word.decode(encoding or 'utf8') if isinstance(word, bytes) else word
        for word, encoding in decode_header(s)
    )

def get_email_body(msg):
    """Wyciąga czysty tekst z maila"""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get('Content-Disposition'))
            if ctype == 'text/plain' and 'attachment' not in cdispo:
                return part.get_payload(decode=True).decode('utf-8', errors='ignore')
    else:
        return msg.get_payload(decode=True).decode('utf-8', errors='ignore')
    return ""

def check_inbox(session: Session, client: Client):
    """Sprawdza skrzynkę odbiorczą w poszukiwaniu odpowiedzi od leadów."""
    print(f"📬 INBOX: Sprawdzam pocztę dla {client.name} ({client.smtp_user})...")
    
    if not client.imap_server:
        print("   ❌ Brak konfiguracji IMAP.")
        return

    try:
        mail = imaplib.IMAP4_SSL(client.imap_server, client.imap_port or 993)
        mail.login(client.smtp_user, client.smtp_password)
        mail.select("INBOX")

        # Szukamy tylko NIEPRZECZYTANYCH (UNSEEN)
        # Możesz zmienić na 'ALL', ale wtedy będzie mieliło całą skrzynkę
        status, messages = mail.search(None, 'UNSEEN')
        
        email_ids = messages[0].split()
        if not email_ids:
            print("   📭 Brak nowych wiadomości.")
            return

        print(f"   📨 Znaleziono {len(email_ids)} nowych maili. Analizuję...")

        for e_id in email_ids:
            _, msg_data = mail.fetch(e_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # 1. KTO PISZE?
                    sender_header = decode_mime_words(msg.get("From"))
                    sender_email = email.utils.parseaddr(sender_header)[1]
                    
                    # 2. CZY TO NASZ LEAD?
                    # Szukamy w bazie leada, który ma taki target_email LUB domena się zgadza
                    lead = session.query(Lead).filter(
                        (Lead.target_email == sender_email) | 
                        (Lead.company.has(domain=sender_email.split('@')[-1]))
                    ).first()

                    if not lead:
                        print(f"   👤 Ignoruję: {sender_email} (Nie ma w bazie leadów)")
                        continue

                    print(f"   🎯 O! Odpisał LEAD ID {lead.id}: {sender_email}")
                    
                    # 3. POBIERZ TREŚĆ
                    body = get_email_body(msg)
                    if not body:
                        continue

                    # 4. ANALIZA AI
                    analysis = analyst_llm.invoke(f"Przeanalizuj odpowiedź od klienta:\n\n{body[:2000]}")
                    
                    # 5. AKTUALIZACJA BAZY
                    lead.replied_at = datetime.utcnow()
                    lead.reply_content = body[:5000] # Ucinamy dla bezpieczeństwa bazy
                    lead.reply_sentiment = analysis.sentiment
                    lead.reply_analysis = f"{analysis.summary} | SUGGESTION: {analysis.suggested_action}"
                    
                    if analysis.is_interested:
                        lead.status = "HOT_LEAD"
                        print(f"   🔥 HOT LEAD! {lead.company.name} jest zainteresowany!")
                    elif analysis.sentiment == "NEGATIVE":
                        lead.status = "NOT_INTERESTED"
                        print(f"   ❄️ Klient nie jest zainteresowany.")
                    else:
                        lead.status = "REPLIED" # Neutralna odpowiedź
                    
                    session.commit()

        mail.close()
        mail.logout()

    except Exception as e:
        print(f"   ❌ Błąd IMAP: {e}")