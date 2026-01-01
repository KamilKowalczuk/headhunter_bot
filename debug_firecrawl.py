import os
import requests
import json
from dotenv import load_dotenv

# Ładujemy zmienne
load_dotenv()
api_key = os.getenv("FIRECRAWL_API_KEY")

print("--- DIAGNOSTYKA DIRECT API (TITAN ENGINE) ---")
print(f"1. Klucz API: {'OBECNY' if api_key else 'BRAK (!!!)'}")

if not api_key:
    print("❌ Zatrzmano: Brak klucza w .env")
    exit()

# Konfiguracja requestu
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}
base_url = "https://api.firecrawl.dev/v1"

def test_scrape():
    print("\n2. TEST SCRAPE (Pobieranie treści)...")
    url = "https://example.com"
    endpoint = f"{base_url}/scrape"
    payload = {
        "url": url,
        "formats": ["markdown"]
    }
    
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'markdown' in data['data']:
                md_len = len(data['data']['markdown'])
                print(f"✅ SUKCES! Pobrano {md_len} znaków Markdown.")
                print(f"   Fragment: {data['data']['markdown'][:50]}...")
                return True
            else:
                print(f"⚠️ Dziwna struktura odpowiedzi: {data.keys()}")
        else:
            print(f"❌ Błąd API ({response.status_code}): {response.text}")
            
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
    return False

def test_map():
    print("\n3. TEST MAP (Mapowanie linków)...")
    url = "https://kamilkowalczuk.pl" # Możesz zmienić na dowolną stronę
    endpoint = f"{base_url}/map"
    payload = {"url": url}
    
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            # Obsługa różnych odpowiedzi API
            links = []
            if 'links' in data: links = data['links']
            elif 'data' in data and 'links' in data['data']: links = data['data']['links']
            
            if links:
                print(f"✅ SUKCES! Zmapowano {len(links)} linków.")
                print(f"   Przykłady: {links[:3]}")
                return True
            else:
                print("⚠️ Mapa pusta (ale zapytanie przeszło).")
        else:
            print(f"❌ Błąd API ({response.status_code}): {response.text}")
            
    except Exception as e:
        print(f"❌ Błąd połączenia: {e}")
    return False

if __name__ == "__main__":
    scrape_ok = test_scrape()
    map_ok = test_map()
    
    print("\n" + "="*30)
    if scrape_ok and map_ok:
        print("🚀 WSZYSTKO DZIAŁA! Nowy Researcher (Direct API) jest gotowy.")
    else:
        print("🛑 SĄ PROBLEMY. Sprawdź komunikaty powyżej.")