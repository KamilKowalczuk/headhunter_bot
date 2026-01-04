from datetime import datetime
from app.database import Client

def calculate_daily_limit(client: Client) -> int:
    """
    Oblicza efektywny limit na DZIŚ, uwzględniając rozgrzewkę.
    """
    target_limit = client.daily_limit or 50
    
    # Jeśli warm-up wyłączony lub brak daty startu -> pełny limit
    if not client.warmup_enabled or not client.warmup_started_at:
        return target_limit

    # Obliczamy ile dni minęło od startu rozgrzewki
    # (Używamy .date(), żeby liczyć pełne dni kalendarzowe)
    days_passed = (datetime.now().date() - client.warmup_started_at.date()).days
    
    if days_passed < 0: days_passed = 0 # Zabezpieczenie

    # Wzór: Start + (Dni * Przyrost)
    start = client.warmup_start_limit or 2
    increment = client.warmup_increment or 2
    
    current_warmup_limit = start + (days_passed * increment)
    
    # Limit nie może przekroczyć docelowego 'daily_limit'
    effective_limit = min(current_warmup_limit, target_limit)
    
    return effective_limit