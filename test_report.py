# test_report.py
from sqlalchemy.orm import Session
from app.database import engine
from app.agents.reporter import create_pdf_report

session = Session(engine)
# Podaj ID klienta, dla którego chcesz raport (np. 1)
create_pdf_report(session, client_id=1)