from sqlalchemy import text
from app.database import engine, Base

def update_database_columns():
    print("🛠️ NEXUS MIGRATION: Aktualizacja struktury...")
    
    with engine.connect() as conn:
        # ... (poprzednie migracje HTML footer itd.) ...
        
        # SEARCH HISTORY (Nowość)
        try:
            print("   ✨ Tworzenie tabeli: search_history...")
            # Najprościej: Używamy create_all dla nowych tabel, sqlalchemy samo ogarnie jeśli nie istnieją
            Base.metadata.create_all(bind=engine)
            print("      ✅ Tabela sprawdzona/utworzona.")
        except Exception as e:
            print(f"      ❌ Błąd przy tabeli search_history: {e}")

    print("\n🏁 Migracja zakończona.")

if __name__ == "__main__":
    update_database_columns()