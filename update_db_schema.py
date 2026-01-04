from sqlalchemy import text
from app.database import engine, Base

def update_database_columns():
    print("🛠️ NEXUS MIGRATION: Wdrażanie Auto-Sender...")
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE clients ADD COLUMN sending_mode VARCHAR DEFAULT 'DRAFT';"))
            print("   ✅ Dodano: sending_mode")
        except: print("   ℹ️ Kolumna 'sending_mode' już istnieje.")
        conn.commit()

if __name__ == "__main__":
    update_database_columns()