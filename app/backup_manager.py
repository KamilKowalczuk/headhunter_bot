import os
import shutil
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("nexus_backup")

class BackupManager:
    def __init__(self, backup_dir="files/backups", max_backups=20):
        self.backup_dir = Path(os.getcwd()) / backup_dir
        self.max_backups = max_backups
        self.db_url = os.getenv("DATABASE_URL")
        
        # Upewnij się, że katalog istnieje
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _get_timestamp(self):
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _rotate_backups(self):
        """Usuwa najstarsze backupy, jeśli przekroczymy limit."""
        try:
            files = sorted(self.backup_dir.glob("backup_*"), key=os.path.getmtime)
            
            if len(files) > self.max_backups:
                to_delete = len(files) - self.max_backups
                for i in range(to_delete):
                    files[i].unlink()
                    logger.info(f"♻️ [ROTATION] Usunięto stary backup: {files[i].name}")
        except Exception as e:
            logger.error(f"❌ Błąd rotacji backupów: {e}")

    def perform_backup(self) -> bool:
        """Wykrywa typ bazy i wykonuje kopię."""
        if not self.db_url:
            logger.error("❌ Brak DATABASE_URL w .env")
            return False

        timestamp = self._get_timestamp()
        
        try:
            # 1. Obsługa SQLite (lokalny plik)
            if self.db_url.startswith("sqlite"):
                db_path_str = self.db_url.replace("sqlite:///", "")
                source_path = Path(db_path_str)
                
                if not source_path.exists():
                    # Próba fixu ścieżki (jeśli jest relatywna)
                    source_path = Path(os.getcwd()) / db_path_str
                
                if source_path.exists():
                    dest_name = f"backup_sqlite_{timestamp}.db"
                    dest_path = self.backup_dir / dest_name
                    shutil.copy2(source_path, dest_path)
                    logger.info(f"✅ [BACKUP] SQLite zapisany: {dest_name}")
                    self._rotate_backups()
                    return True
                else:
                    logger.error(f"❌ Nie znaleziono pliku bazy SQLite: {source_path}")
                    return False

            # 2. Obsługa PostgreSQL (Enterprise)
            elif self.db_url.startswith("postgresql"):
                parsed = urlparse(self.db_url)
                db_name = parsed.path.lstrip('/')
                user = parsed.username
                password = parsed.password
                host = parsed.hostname
                port = parsed.port or 5432
                
                dest_name = f"backup_pg_{db_name}_{timestamp}.sql"
                dest_path = self.backup_dir / dest_name

                # Komenda pg_dump (musi być zainstalowana w systemie/kontenerze)
                env = os.environ.copy()
                if password:
                    env["PGPASSWORD"] = password

                cmd = [
                    "pg_dump",
                    "-h", host,
                    "-p", str(port),
                    "-U", user,
                    "-F", "c", # Format custom (kompresowany)
                    "-b",      # Blobs
                    "-v",      # Verbose
                    "-f", str(dest_path),
                    db_name
                ]

                process = subprocess.run(cmd, env=env, capture_output=True, text=True)
                
                if process.returncode == 0:
                    logger.info(f"✅ [BACKUP] Postgres zapisany: {dest_name}")
                    self._rotate_backups()
                    return True
                else:
                    logger.error(f"❌ Błąd pg_dump: {process.stderr}")
                    return False

            else:
                logger.warning(f"⚠️ Nieobsługiwany typ bazy do backupu: {self.db_url}")
                return False

        except Exception as e:
            logger.error(f"❌ Błąd krytyczny backupu: {e}")
            return False

# Singleton instance
backup_manager = BackupManager()