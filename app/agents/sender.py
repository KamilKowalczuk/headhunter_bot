import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.database import Lead, Client

def send_email_via_smtp(lead: Lead, client: Client) -> bool:
    """
    Wysyła fizycznego maila przez SMTP (Pełen Automat).
    """
    try:
        sender_email = client.smtp_user
        password = client.smtp_password
        receiver_email = lead.target_email
        
        # Tworzenie wiadomości
        message = MIMEMultipart("alternative")
        message["Subject"] = lead.generated_email_subject
        message["From"] = f"{client.sender_name} <{sender_email}>"
        message["To"] = receiver_email

        # Treść HTML (Writer generuje HTML)
        html_content = lead.generated_email_body
        
        # Dodajemy stopkę HTML, jeśli istnieje i nie jest pusta
        if client.html_footer:
            html_content += f"<br><br>{client.html_footer}"

        # Wersja Plain Text (Dla bezpieczeństwa anty-spamowego - uproszczona)
        text_content = "Proszę włączyć widok HTML, aby zobaczyć tę wiadomość."

        part1 = MIMEText(text_content, "plain")
        part2 = MIMEText(html_content, "html")

        message.attach(part1)
        message.attach(part2)

        # Logowanie do serwera
        context = ssl.create_default_context()
        
        # Obsługa różnych portów (465 SSL, 587 TLS)
        if client.smtp_port == 465:
            with smtplib.SMTP_SSL(client.smtp_server, client.smtp_port, context=context) as server:
                server.login(sender_email, password)
                server.sendmail(sender_email, receiver_email, message.as_string())
        else:
            with smtplib.SMTP(client.smtp_server, client.smtp_port) as server:
                server.starttls(context=context)
                server.login(sender_email, password)
                server.sendmail(sender_email, receiver_email, message.as_string())

        return True

    except Exception as e:
        print(f"❌ Błąd SMTP: {e}")
        return False