import os
import re
import dns.resolver
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

# API CONFIG
DEBOUNCE_API_KEY = os.getenv("DEBOUNCE_API_KEY")

def normalize_domain(url: str) -> str:
    """Czyści URL do samej domeny."""
    if not url: return ""
    if not url.startswith(("http://", "https://")): url = "http://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        if domain.startswith("www."): domain = domain[4:]
        return domain.lower()
    except: return ""

def clean_text(text: str) -> str:
    """Usuwa nadmiarowe spacje."""
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()

def get_main_domain_url(url: str) -> str:
    """Zwraca czysty URL strony głównej."""
    if not url: return ""
    if not url.startswith(("http://", "https://")): url = "https://" + url
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except:
        return url

def verify_email_mx(email: str) -> bool:
    """
    Szybka, darmowa weryfikacja DNS/MX.
    """
    try:
        domain = email.split('@')[1]
        records = dns.resolver.resolve(domain, 'MX')
        return len(records) > 0
    except:
        return False

def verify_email_deep(email: str) -> str:
    """
    ENTERPRISE VERIFICATION (DeBounce API - SMART LOGIC).
    Dokumentacja vs Rzeczywistość: Ignorujemy 'code' jeśli 'result' jest obiecujący.
    """
    # 1. Fallback: Jeśli brak klucza API, robimy tylko MX check
    if not DEBOUNCE_API_KEY:
        mx_ok = verify_email_mx(email)
        return "OK" if mx_ok else "INVALID"

    # 2. API Call
    try:
        url = "https://api.debounce.io/v1/"
        params = {
            "api": DEBOUNCE_API_KEY,
            "email": email
        }
        
        # Timeout 10s na request
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # Debugowanie w konsoli (widzisz co się dzieje)
            print(f"🐛 [DEBUG] DeBounce JSON: {data}")

            # Pobieramy dane z zagnieżdżonego obiektu lub głównego (Hybryda)
            debounce_data = data.get("debounce", data) # Fallback na root jeśli brak 'debounce'
            
            result_text = str(debounce_data.get("result", "")).lower() # np. "safe to send", "risky"
            code = str(debounce_data.get("code", "0"))
            
            # --- NOWA LOGIKA BIZNESOWA (AGRESYWNA SPRZEDAŻ) ---
            
            # 1. PEWNIAKI
            if "safe" in result_text or code == "1":
                return "OK"
            
            # 2. RYZYKOWNE (Catch-all, Role, Spamtrap ale oznaczony jako Risky)
            # W Cold Emailu "Risky" to wciąż szansa na deal. Odrzucenie tego to strata pieniędzy.
            if "risky" in result_text:
                return "RISKY"
                
            # 3. SPECJALNE PRZYPADKI (Gdy tekst jest niejasny, patrzymy na kody)
            if code == "5": return "RISKY" # Accept All
            if code == "6": return "OK"    # Role-Based (Sales/Info) - to są nasi klienci!
            
            # 4. TWARDE ODRZUCENIE
            if "invalid" in result_text or code in ["2", "3", "8"]:
                return "INVALID"

            # 5. Jeśli dotarliśmy tutaj i kod to 4 (Spamtrap), ale nie był Risky...
            # To znaczy że to groźny Spamtrap.
            if code == "4":
                return "INVALID"
            
            # Domyślnie Invalid, żeby nie palić domeny
            return "UNKNOWN"
            
        else:
            print(f"⚠️ API Http Error: {response.status_code}")
            return "UNKNOWN"

    except Exception as e:
        print(f"⚠️ Błąd API DeBounce dla {email}: {e}")
        return "OK" if verify_email_mx(email) else "INVALID"