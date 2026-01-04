import os
import logging
import re # <--- DODAŁEM
from datetime import datetime
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv

from app.database import Lead, Client, GlobalCompany
from app.schemas import EmailDraft, AuditResult

# Konfiguracja loggera
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("writer")

load_dotenv()

# Dwa modele: Writer (Kreatywny) i Auditor (Analityczny)
writer_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.75,
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(EmailDraft)

auditor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.0,
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(AuditResult)

# --- SAFETY NET: HTML VALIDATOR (DODANO) ---
def _sanitize_and_validate_html(html_content: str) -> str:
    """
    Naprawia i czyści HTML wygenerowany przez AI.
    Chroni przed rozsypaniem się maila w Outlooku.
    """
    if not html_content: return ""

    # 1. Usuwanie niebezpiecznych tagów (XSS / Spam Filters)
    forbidden_tags = [r'<script.*?>.*?</script>', r'<iframe.*?>.*?</iframe>', r'<object.*?>.*?</object>', r'<style.*?>.*?</style>']
    clean_html = html_content
    for tag in forbidden_tags:
        clean_html = re.sub(tag, '', clean_html, flags=re.DOTALL | re.IGNORECASE)

    # 2. Sprawdzenie balansu tagów (Prosta Heurystyka)
    # Zliczamy otwarcia i zamknięcia dla kluczowych bloków
    tags_to_check = ['div', 'p', 'b', 'strong', 'i', 'em', 'ul', 'li']
    
    for tag in tags_to_check:
        open_count = len(re.findall(f"<{tag}[^>]*>", clean_html, re.IGNORECASE))
        close_count = len(re.findall(f"</{tag}>", clean_html, re.IGNORECASE))
        
        if open_count > close_count:
            # Jeśli brakuje zamknięcia, dodajemy je na końcu
            missing = open_count - close_count
            logger.warning(f"⚠️ [HTML FIX] Brakuje {missing} zamknieć dla <{tag}>. Doklejam.")
            clean_html += f"</{tag}>" * missing

    # 3. Usuwanie wielokrotnych <br> (Estetyka)
    clean_html = re.sub(r'(<br\s*/?>){3,}', '<br><br>', clean_html)
    
    return clean_html.strip()
# -------------------------------------------

def generate_email(session: Session, lead_id: int):
    """
    Wrapper synchroniczny (dla kompatybilności z wątkami).
    """
    _generate_email_sync(session, lead_id)

def _generate_email_sync(session: Session, lead_id: int):
    """
    MASTER PROCESS: Generowanie maila.
    """
    lead = session.query(Lead).filter(Lead.id == lead_id).first()
    if not lead or not lead.campaign or not lead.campaign.client:
        logger.error(f"❌ Błąd danych leada ID {lead_id}.")
        return

    client = lead.campaign.client
    company = lead.company
    
    # Pobieramy tryb działania (SALES lub JOB_HUNT)
    mode = getattr(client, "mode", "SALES")
    
    logger.info(f"✍️  [WRITER {mode}] Piszę dla {company.name} (Step {lead.step_number})...")

    # --- 1. PRZYGOTOWANIE PERSONY (DECYDENTA) ---
    decision_maker_name = "Zespole"
    dm_data = company.decision_makers
    
    if dm_data:
        try:
            first_dm = dm_data[0] if isinstance(dm_data, list) and len(dm_data) > 0 else str(dm_data)
            if "(" in first_dm:
                parts = first_dm.split("(")
                full_name = parts[0].strip()
                decision_maker_name = full_name.split(" ")[0]
            else:
                decision_maker_name = first_dm.split(" ")[0]
        except Exception as e:
            logger.warning(f"Błąd parsowania decydenta: {e}")
            decision_maker_name = "Zespole"

    # --- 2. GENEROWANIE TREŚCI (ITERACJA 1) ---
    try:
        draft = _call_writer(
            client=client, 
            company=company, 
            decision_maker=decision_maker_name, 
            lead_summary=lead.ai_analysis_summary or "Brak danych z researchu.", 
            step=lead.step_number,
            mode=mode
        )
    except Exception as e:
        logger.error(f"❌ Błąd AI Writera: {e}")
        return
    
    # --- 3. SAFETY NET: WALIDACJA HTML (ZMIANA) ---
    safe_body = _sanitize_and_validate_html(draft.body)

    final_status = "DRAFTED"
    score = 85
    
    # --- 4. ZAPIS WYNIKU ---
    lead.generated_email_subject = draft.subject
    lead.generated_email_body = safe_body # Zapisujemy bezpieczny HTML
    lead.ai_confidence_score = score
    
    if lead.status != "MANUAL_CHECK":
        lead.status = final_status
    
    lead.last_action_at = datetime.now()
    session.commit()
    logger.info(f"   💾 Zapisano draft: '{draft.subject}' (HTML Validated)")


def _call_writer(client, company, decision_maker, lead_summary, step=1, feedback=None, mode="SALES"):
    """
    ENGINE: Silnik generujący treść (Polimorficzny: SALES / JOB_HUNT).
    """
    sender = client.sender_name or "Zespół"
    uvp = client.value_proposition or "Wsparcie B2B"
    cases = client.case_studies or ""
    tone = client.tone_of_voice or "Profesjonalny"
    constraints = client.negative_constraints or "Brak"
    
    # Logika stopki
    signature_instruction = ""
    if getattr(client, "html_footer", None): 
        signature_instruction = (
            "⛔ BARDZO WAŻNE: NIE dodawaj na końcu maila żadnego podpisu ani pożegnania "
            "(typu 'Pozdrawiam, Jan'). Mail ma się kończyć kropką po ostatnim zdaniu lub CTA. "
            "Podpis HTML (Stopka) zostanie doklejony automatycznie przez system wysyłkowy."
        )
    else:
        signature_instruction = f"Zakończ maila profesjonalnym podpisem tekstowym: {sender}."

    if mode == "JOB_HUNT":
        # --- TRYB REKRUTACJI ---
        system_role = f"""
        Jesteś Specjalistą ds. Aplikacji o Pracę (Job Hunter).
        Piszesz w imieniu Kandydata: {sender}.
        
        TWOJE BIO (UVP): {uvp}
        TWOJE DOŚWIADCZENIE (Case Studies): {cases[:300]}
        TONE: {tone} (np. "Pewny siebie, ale pokorny").
        """
        
        if step == 1:
            strategy_prompt = f"""
            RODZAJ: DIRECT APPLICATION (Cold Email do Firmy)
            STRUKTURA: "Value Match" (Widzę, że robicie X -> Ja umiem X -> Pogadamy?)
            CEL: Umówić rozmowę rekrutacyjną (lub kawę online).
            
            INSTRUKCJE:
            1. **ICEBREAKER**: "Cześć {decision_maker}, widzę, że mocno rozwijacie się w [Technologia z Researchu]."
            2. **VALUE**: "Jestem [Twoja Rola]. W ostatnim projekcie [Twoje Case Study]."
            3. **MATCH**: Podkreśl zgodność Twoich skilli z ich stackiem technologicznym.
            4. **CTA**: Niskie ryzyko. Np. "Szukacie kogoś do pomocy? Chętnie pokażę kod."
            """
        else:
            strategy_prompt = f"""
            RODZAJ: FOLLOW-UP (Przypomnienie o Kandydaturze)
            TREŚĆ: "Cześć {decision_maker}, podbijam temat. Czy rekrutujecie teraz do zespołu [Technologia]?"
            DŁUGOŚĆ: Ultra krótko.
            """
            
    else:
        # --- TRYB SPRZEDAŻY (STANDARD) ---
        system_role = f"""
        Jesteś światowej klasy Copywriterem B2B.
        Piszesz w imieniu: {sender} z firmy {client.name}.
        
        DNA:
        - UVP: {uvp}
        - Tone: {tone}
        - Constraints: {constraints}
        """
        
        if step == 1:
            strategy_prompt = f"""
            RODZAJ: COLD EMAIL (Initial Outreach)
            STRUKTURA: "The Bridge Model" (Icebreaker -> Problem -> Rozwiązanie -> CTA)
            CEL: Sprzedać ROZMOWĘ, a nie produkt.
            DŁUGOŚĆ: Krótko (max 100-120 słów). Szanuj czas CEO.
            
            INSTRUKCJE SPECJALNE:
            1. **ICEBREAKER**: Zacznij od odniesienia się do firmy: "Cześć {decision_maker}".
            2. **PROBLEM**: Nawiąż do branży (z analizy).
            3. **DOWÓD**: Wykorzystaj Case Study (jeśli pasuje): "{cases[:200]}..."
            4. **CTA**: Niskie ryzyko. Np. "Warto pogadać?".
            """
        else:
            strategy_prompt = f"""
            RODZAJ: FOLLOW-UP (Przypomnienie)
            STRUKTURA: "Quick Bump"
            TREŚĆ: "Cześć {decision_maker}, podbijam temat. Czy (krótka korzyść) jest teraz priorytetem?"
            """

    # --- WSPÓLNY KONIEC ---
    full_system_prompt = f"""
    {system_role}
    
    TARGET:
    - Firma: {company.name}
    - Decydent: {decision_maker}
    - Analiza Researchera: {lead_summary}
    
    ZADANIE:
    Napisz treść maila zgodnie ze strategią.
    
    {strategy_prompt}
    
    FORMATOWANIE:
    Używaj tagów HTML (<p>, <b>, <br>).
    NIE dodawaj tematu w treści body.
    
    PODPIS:
    {signature_instruction}
    """
    
    user_message = "Napisz ten draft."
    if feedback:
        user_message += f"\n\n🚨 KOREKTA: Audytor zgłosił: '{feedback}'. Popraw."

    prompt = ChatPromptTemplate.from_messages([("system", full_system_prompt), ("human", user_message)])
    return (prompt | writer_llm).invoke({})

def _call_auditor(draft, company, client):
    """
    Strażnik Marki i Prawdy.
    """
    system_prompt = f"""
    Jesteś Audytorem Jakości (QA).
    
    ZADANIE:
    Sprawdź draft pod kątem:
    1. Halucynacji (Technologie: {company.tech_stack or "Brak danych"})
    2. Zgodności z marką (Zakazy: {client.negative_constraints or "Brak"})
    3. Personalizacji (Czy nie wygląda jak spam?)
    4. Placeholderów (Czy nie ma [Wstaw Imię]?)
    
    DRAFT:
    Subject: {draft.subject}
    Body: {draft.body}
    
    Decyzja: True/False.
    Feedback: Co poprawić.
    """
    
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", "Sprawdź to.")])
    return (prompt | auditor_llm).invoke({})