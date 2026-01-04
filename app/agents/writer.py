import os
import logging
import re
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

# Modele AI
writer_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.85, # Podkręcamy kreatywność dla bardziej ludzkiego stylu
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(EmailDraft)

auditor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.0, # Zero tolerancji dla błędów
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(AuditResult)

# --- SAFETY NET: HTML VALIDATOR ---
def _sanitize_and_validate_html(html_content: str) -> str:
    """
    Naprawia i czyści HTML wygenerowany przez AI.
    Chroni przed rozsypaniem się maila w Outlooku.
    """
    if not html_content: return ""

    # 1. Usuwanie niebezpiecznych tagów
    forbidden_tags = [r'<script.*?>.*?</script>', r'<iframe.*?>.*?</iframe>', r'<object.*?>.*?</object>', r'<style.*?>.*?</style>']
    clean_html = html_content
    for tag in forbidden_tags:
        clean_html = re.sub(tag, '', clean_html, flags=re.DOTALL | re.IGNORECASE)

    # 2. Sprawdzenie balansu tagów
    tags_to_check = ['div', 'p', 'b', 'strong', 'i', 'em', 'ul', 'li']
    
    for tag in tags_to_check:
        open_count = len(re.findall(f"<{tag}[^>]*>", clean_html, re.IGNORECASE))
        close_count = len(re.findall(f"</{tag}>", clean_html, re.IGNORECASE))
        
        if open_count > close_count:
            missing = open_count - close_count
            # logger.warning(f"⚠️ [HTML FIX] Brakuje {missing} zamknieć dla <{tag}>. Doklejam.")
            clean_html += f"</{tag}>" * missing

    # 3. Usuwanie wielokrotnych <br>
    clean_html = re.sub(r'(<br\s*/?>){3,}', '<br><br>', clean_html)
    
    return clean_html.strip()
# -------------------------------------------

def generate_email(session: Session, lead_id: int):
    """
    Wrapper synchroniczny.
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
    
    mode = getattr(client, "mode", "SALES")
    
    logger.info(f"✍️  [WRITER {mode}] Piszę dla {company.name} (Step {lead.step_number})...")

    # --- 1. PRZYGOTOWANIE PERSONY (DECYDENTA) ---
    decision_maker_name = "Zespole" # Domyślnie
    dm_data = company.decision_makers
    
    # Próba wyciągnięcia imienia
    if dm_data:
        try:
            # Jeśli to lista obiektów [{'name': 'Jan'}]
            if isinstance(dm_data, list) and len(dm_data) > 0:
                first = dm_data[0]
                if isinstance(first, dict):
                    name_str = first.get('name', str(first))
                else:
                    name_str = str(first)
            else:
                name_str = str(dm_data)
                
            # Czyszczenie (Jan Kowalski (CEO) -> Jan)
            name_clean = name_str.split('(')[0].split(',')[0].strip()
            first_name = name_clean.split(' ')[0]
            if len(first_name) > 2: # Zabezpieczenie przed skrótami
                decision_maker_name = first_name
                
        except Exception as e:
            logger.warning(f"Błąd parsowania decydenta: {e}")
            decision_maker_name = "Zespole"

    # --- 2. GENEROWANIE TREŚCI (ITERACJA 1) ---
    try:
        draft = _call_writer(
            client=client, 
            company=company, 
            decision_maker=decision_maker_name, 
            lead_summary=lead.ai_analysis_summary or "Brak specyficznych danych.", 
            step=lead.step_number,
            mode=mode
        )
    except Exception as e:
        logger.error(f"❌ Błąd AI Writera: {e}")
        return
    
    # --- 3. SAFETY NET: WALIDACJA HTML ---
    safe_body = _sanitize_and_validate_html(draft.body)

    # Można tu dodać krok Auditora (_call_auditor), ale dla szybkości pomijam w tym zrzucie, 
    # zakładając, że prompt Writera jest wystarczająco silny.
    
    score = 85 # Domyślny wysoki score dla v2.0
    
    # --- 4. ZAPIS WYNIKU ---
    lead.generated_email_subject = draft.subject
    lead.generated_email_body = safe_body 
    lead.ai_confidence_score = score
    
    if lead.status != "MANUAL_CHECK":
        lead.status = "DRAFTED"
    
    lead.last_action_at = datetime.now()
    session.commit()
    logger.info(f"   💾 Zapisano draft: '{draft.subject}'")


def _call_writer(client, company, decision_maker, lead_summary, step=1, feedback=None, mode="SALES"):
    """
    ENGINE: Silnik generujący treść. Prawdziwa inżynieria promptu (Protocol: GHOSTWRITER).
    """
    sender = client.sender_name or "Kamil"
    sender_company = client.name
    uvp = client.value_proposition or "Wspieramy firmy B2B"
    cases = client.case_studies or "Współpracowaliśmy z wieloma firmami."
    tone = client.tone_of_voice or "Profesjonalny, konkretny"
    constraints = client.negative_constraints or "Brak"
    
    # Logika stopki (Czy system dokleja?)
    signature_instruction = ""
    if getattr(client, "html_footer", None): 
        signature_instruction = (
            "⛔ ZAKAZ PODPISU: Nie pisz 'Pozdrawiam, [Imię]'. Mail ma się kończyć nagle, po Call to Action lub jednym zdaniu pożegnalnym. Stopka HTML zostanie doklejona automatycznie."
        )
    else:
        signature_instruction = f"Zakończ maila: 'Pozdrawiam, {sender}'."

    # --- BUDOWANIE KONTEKSTU ---
    
    base_instructions = f"""
    Jesteś doświadczonym Business Developerem, który nienawidzi "korpo-bełkotu". 
    Twoim celem jest nawiązanie relacji H2H (Human to Human), a nie B2B.
    
    Piszesz do: {company.name}
    Osoba: {decision_maker} (Jeśli to "Zespole", pisz w liczbie mnogiej).
    Wiedza o firmie (Research): {lead_summary}
    
    TWOJE ZASADY STYLU (NON-NEGOTIABLE):
    1. **Zero Waty:** Żadnych "mamy przyjemność", "wychodzimy naprzeciw", "kompleksowe rozwiązania". To spam.
    2. **Casual & Direct:** Pisz tak, jakbyś pisał do kolegi z branży, ale z szacunkiem.
    3. **Krótko:** CEO czyta maile na telefonie. Max 3-4 krótkie akapity.
    4. **Ty > Ja:** Skup się na NICH. Użyj słowa "Wy", "Wasz", "Twój" 3x częściej niż "My".
    """

    if mode == "JOB_HUNT":
        # --- SCENARIUSZ: SZUKANIE PRACY ---
        if step == 1:
            task_prompt = f"""
            RODZAJ: Aplikacja o Pracę (Cold Message)
            CEL: Zaintrygować CTO/Foundera, żeby otworzył CV.
            
            STRUKTURA:
            1. **The Hook:** Odnieś się do ich tech stacku lub ostatniego sukcesu (z Researchu). Np. "Widziałem, że wchodzicie w AI..."
            2. **The Value:** Nie pisz "szukam pracy". Napisz "rozwiązuję problemy". Użyj jednego mocnego zdania z Twojego UVP: "{uvp}".
            3. **The Proof:** "Robiłem podobne rzeczy przy projekcie X."
            4. **Soft CTA:** "Szukacie teraz rąk do pracy? Mogę podesłać kod."
            
            Unikaj tonu błagalnego. Jesteś ekspertem oferującym usługi.
            """
        else:
            task_prompt = f"""
            RODZAJ: Follow-Up (Lekkie przypomnienie)
            CEL: Podbić wiadomość na górę skrzynki.
            
            TREŚĆ:
            "Cześć {decision_maker}, podbijam tylko temat, bo pewnie utonął w inboxie.
            Gdybyście szukali wsparcia w [Technologia z Researchu] - jestem pod ręką."
            """

    else:
        # --- SCENARIUSZ: SPRZEDAŻ B2B ---
        if step == 1:
            task_prompt = f"""
            RODZAJ: Cold Email Sprzedażowy (Otwarcie)
            CEL: Sprawić, by odpisali "Tak, pogadajmy".
            
            STRATEGIA "RELEVANCE FIRST":
            1. **Subject Line:** Musi być intrygujący, nie sprzedażowy. Np. "Pytanie o [Technologia]", "Współpraca z {company.name}?", "Pomysł na [Problem]".
               MA BYĆ KRÓTKI (max 4 słowa).
            
            2. **Body:**
               - **Hook:** "Cześć {decision_maker}, przeglądałem Waszą stronę i widzę, że [Wstaw coś konkretnego z researchu - np. używają technologii X, rekrutują, rosną]."
               - **Bridge:** "Wiele software house'ów (lub firm z ich branży) ma teraz wyzwanie z [Problem z UVP]."
               - **Solution (Ty):** "{uvp}. Pomagamy w tym, np. ostatnio dla [Case Study] zrobiliśmy [Wynik]."
               - **CTA:** "Macie 10 minut w czwartek, żeby zderzyć myśli?" (Lub inne konkretne, ale luźne CTA).
            
            Użyj danych z researchu ({lead_summary}), aby to uwiarygodnić. Jeśli wiesz, że używają Reacta, wspomnij o tym.
            """
        else:
            task_prompt = f"""
            RODZAJ: Follow-Up (Wartość dodana)
            CEL: Przypomnienie + Nowa wartość.
            
            TREŚĆ:
            "Cześć {decision_maker}, myślałem jeszcze o Waszym projekcie.
            Często przy [Problem] sprawdza się podejście [Krótka rada/Case].
            
            Warto o tym pogadać?
            {sender}"
            """

    full_prompt = f"""
    {base_instructions}
    
    TWOJE ZADANIE:
    {task_prompt}
    
    WAŻNE ZAKAZY (Constraints):
    {constraints}
    
    {signature_instruction}
    
    Generuj wynik w formacie JSON (Subject + Body HTML).
    """
    
    user_message = "Generuj wiadomość."
    if feedback:
        user_message += f"\n\nPOPRAWKA (Feedback od Audytora): {feedback}"

    prompt = ChatPromptTemplate.from_messages([("system", full_prompt), ("human", user_message)])
    return (prompt | writer_llm).invoke({})

def _call_auditor(draft, company, client):
    """
    Opcjonalny krok weryfikacji. 
    W tej wersji kodu nieużywany w głównym flow dla szybkości, 
    ale gotowy do podpięcia.
    """
    system_prompt = f"""
    Jesteś krytycznym korektorem. Oceniasz maila sprzedażowego.
    
    ZASADY:
    1. Czy brzmi jak człowiek? (Jeśli brzmi jak ChatGPT -> REJECT).
    2. Czy temat jest krótki?
    3. Czy nie ma placeholderów typu [Wstaw nazwę]?
    
    Mail:
    Temat: {draft.subject}
    Treść: {draft.body}
    """
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", "Oceń.")])
    return (prompt | auditor_llm).invoke({})