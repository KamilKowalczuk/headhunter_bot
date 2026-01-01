import json
import os
from typing import List

FILES_DIR = "files"

def get_history_file(campaign_id: int) -> str:
    """Zwraca ścieżkę do pliku historii dla danej kampanii."""
    if not os.path.exists(FILES_DIR):
        os.makedirs(FILES_DIR)
    return os.path.join(FILES_DIR, f"campaign_{campaign_id}_history.json")

def load_used_queries(campaign_id: int) -> List[str]:
    """Ładuje listę zapytań, które już wykorzystaliśmy."""
    filepath = get_history_file(campaign_id)
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_used_queries(campaign_id: int, new_queries: List[str]):
    """Dopisuje nowe zapytania do historii."""
    current = load_used_queries(campaign_id)
    # Dodajemy tylko unikalne, lowercase, żeby uniknąć duplikatów
    updated = list(set(current + [q.lower().strip() for q in new_queries]))
    
    filepath = get_history_file(campaign_id)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)