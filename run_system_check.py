import os
import sys
from rich.console import Console
from rich.panel import Panel
from dotenv import load_dotenv

# Importy silników
from sqlalchemy import text
from app.database import engine
from apify_client import ApifyClient
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()
console = Console()

def test_database():
    console.print("1. [bold]Baza Danych (PostgreSQL)[/bold]...", end=" ")
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        console.print("[green]✅ OK[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ BŁĄD: {e}[/red]")
        return False

def test_gemini():
    console.print("2. [bold]Google Gemini (AI Brain)[/bold]...", end=" ")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        console.print("[red]❌ BŁĄD: Brak klucza w .env[/red]")
        return False
        
    try:
        llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=api_key)
        response = llm.invoke("Odpowiedz tylko słowem: OK")
        if "OK" in response.content:
            console.print("[green]✅ OK[/green]")
            return True
        else:
            console.print(f"[yellow]⚠️ Dziwna odpowiedź: {response.content}[/yellow]")
            return True
    except Exception as e:
        console.print(f"[red]❌ BŁĄD: {e}[/red]")
        return False

def test_apify():
    console.print("3. [bold]Apify (Google Maps Scout)[/bold]...", end=" ")
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        console.print("[red]❌ BŁĄD: Brak APIFY_API_TOKEN w .env[/red]")
        return False

    try:
        client = ApifyClient(token)
        # Próba pobrania informacji o użytkowniku (lekki test połączenia)
        user = client.user().get()
        if user:
            console.print(f"[green]✅ OK (Zalogowano jako: {user.get('username', 'Unknown')})[/green]")
            return True
        else:
            console.print("[red]❌ BŁĄD: Niepoprawny token?[/red]")
            return False
    except Exception as e:
        console.print(f"[red]❌ BŁĄD: {e}[/red]")
        return False

def test_directories():
    console.print("4. [bold]Struktura Katalogów[/bold]...", end=" ")
    files_dir = os.path.join(os.path.dirname(__file__), 'files')
    if os.path.exists(files_dir):
        console.print("[green]✅ OK (Folder 'files/' istnieje)[/green]")
        return True
    else:
        try:
            os.makedirs(files_dir)
            console.print("[yellow]⚠️ Utworzono brakujący folder 'files/'[/yellow]")
            return True
        except:
            console.print("[red]❌ Nie można utworzyć folderu[/red]")
            return False

def main():
    console.clear()
    console.print(Panel.fit("[bold magenta]🔍 AGENCY OS: DIAGNOSTYKA SYSTEMU[/bold magenta]"))
    
    checks = [
        test_database(),
        test_gemini(),
        test_apify(),
        test_directories()
    ]
    
    console.print("\n" + "-"*30 + "\n")
    
    if all(checks):
        console.print("[bold green]🚀 WSZYSTKIE SYSTEMY SPRAWNE. MOŻESZ STARTOWAĆ![/bold green]")
    else:
        console.print("[bold red]🛑 WYKRYTO BŁĘDY. POPRAW KONFIGURACJĘ PRZED STARTEM.[/bold red]")

if __name__ == "__main__":
    main()