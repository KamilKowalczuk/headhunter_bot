from urllib.parse import urlparse
import re
import dns.resolver

def normalize_domain(url: str) -> str:
    """Czyści URL do samej domeny."""
    if not url: return ""
    if not url.startswith(("http://", "https://")): url = "http://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain.startswith("www."): domain = domain[4:]
        return domain.lower()
    except: return ""

def clean_text(text: str) -> str:
    """Usuwa nadmiarowe spacje."""
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()

def get_main_domain_url(url: str) -> str:
    """Zwraca czysty URL strony głównej (np. z https://x.com/careers -> https://x.com)"""
    if not url: return ""
    if not url.startswith(("http://", "https://")): url = "https://" + url
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except:
        return url

def verify_email_domain(email: str) -> bool:
    """Sprawdza czy domena maila ma rekordy MX (czy mail może istnieć)."""
    try:
        domain = email.split('@')[1]
        records = dns.resolver.resolve(domain, 'MX')
        return len(records) > 0
    except:
        return False