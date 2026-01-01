from sqlalchemy import text
from app.database import engine

def clean_database_columns():
    print("🧹 ROBIĘ PORZĄDEK W TABELI CLIENTS...")
    
    with engine.connect() as conn:
        # 1. USUWANIE NIEPOTRZEBNYCH KOLUMN
        columns_to_drop = ["cv_filename", "cv_file", "attachment_file"]
        
        for col in columns_to_drop:
            try:
                print(f"   🗑️ Usuwam kolumnę: {col}...")
                conn.execute(text(f"ALTER TABLE clients DROP COLUMN IF EXISTS {col};"))
                conn.commit()
            except Exception as e:
                print(f"      (Info: {e})")

        # 2. DODANIE POPRAWNEJ KOLUMNY (attachment_filename)
        try:
            print("   ✨ Dodaję poprawną kolumnę: attachment_filename...")
            # Postgres wyrzuci błąd jeśli kolumna już jest, więc łapiemy go
            conn.execute(text("ALTER TABLE clients ADD COLUMN attachment_filename VARCHAR;"))
            conn.commit()
            print("      ✅ Sukces!")
        except Exception as e:
            print("      ℹ️ Kolumna 'attachment_filename' już istnieje (to dobrze).")

    print("\n🏁 Baza posprzątana. Pamiętaj o 'Sync Metadata' w NocoDB!")

if __name__ == "__main__":
    clean_database_columns()