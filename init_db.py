from app.database import engine, Base

def init_db():
    print("🚀 Inicjalizacja Agency OS Database...")
    try:
        # To polecenie tworzy wszystkie tabele zdefiniowane w app/database.py
        Base.metadata.create_all(bind=engine)
        print("✅ Tabele utworzone pomyślnie:")
        print("   - clients (Client DNA)")
        print("   - global_companies (Knowledge Graph)")
        print("   - campaigns")
        print("   - leads")
    except Exception as e:
        print(f"❌ Błąd inicjalizacji: {e}")

if __name__ == "__main__":
    init_db()