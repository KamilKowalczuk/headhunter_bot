import os
import logging
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
    temperature=0.75, # Zwiększona kreatywność dla lepszego flow
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(EmailDraft)

auditor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.0, # Zero litości
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(AuditResult)

def generate_email(session: Session, lead_id: int):
    """
    MASTER PROCESS: Generowanie maila z wykorzystaniem pełnego DNA Klienta.
    """
    lead = session.query(Lead).filter(Lead.id == lead_id).first()
    if not lead or not lead.campaign or not lead.campaign.client:
        logger.error(f"❌ Błąd danych leada ID {lead_id}.")
        return

    client = lead.campaign.client
    company = lead.company
    
    logger.info(f"✍️  [WRITER] Piszę dla {company.name} (Step {lead.step_number})...")

    # --- 1. PRZYGOTOWANIE PERSONY (DECYDENTA) ---
    decision_maker_name = "Zespole"
    dm_data = company.decision_makers
    
    # Inteligentne parsowanie pola decision_makers
    if dm_data:
        try:
            # Obsługa listy z SQLAlchemy (często JSONB wraca jako lista)
            first_dm = dm_data[0] if isinstance(dm_data, list) and dm_data else str(dm_data)
            
            # Jeśli mamy format "Jan Kowalski (CEO)", bierzemy imię
            if "(" in first_dm:
                parts = first_dm.split("(")
                full_name = parts[0].strip()
                # Próba wyciagnięcia imienia: "Jan Kowalski" -> "Jan"
                decision_maker_name = full_name.split(" ")[0]
            else:
                decision_maker_name = first_dm.split(" ")[0]
        except Exception as e:
            logger.warning(f"Błąd parsowania decydenta: {e}")
            decision_maker_name = "Zespole"

    # --- 2. GENEROWANIE TREŚCI (ITERACJA 1) ---
    draft = _call_writer(
        client=client, 
        company=company, 
        decision_maker=decision_maker_name, 
        lead_summary=lead.ai_analysis_summary, # Tu siedzi Icebreaker i Tech Stack
        step=lead.step_number
    )
    
    # --- 3. AUDYT JAKOŚCI (SAFETY NET) ---
    logger.info("   👮 [AUDITOR] Weryfikacja faktów...")
    audit = _call_auditor(draft, company, client)
    
    final_status = "DRAFTED"
    score = 85
    
    if not audit.passed:
        logger.warning(f"   ⚠️ AUDIT FAIL: {audit.feedback}. Poprawiam...")
        # Druga próba - Writer dostaje opierdziel od Audytora
        draft = _call_writer(
            client, company, decision_maker_name, lead.ai_analysis_summary, 
            step=lead.step_number, 
            feedback=audit.feedback
        )
        
        # Szybki re-audyt (dla formalności)
        audit2 = _call_auditor(draft, company, client)
        if not audit2.passed:
             logger.error("   ❌ AUDIT FAIL #2. Oznaczam do ręcznej poprawki.")
             final_status = "MANUAL_CHECK"
             score = 30
    
    # --- 4. ZAPIS WYNIKU ---
    lead.generated_email_subject = draft.subject
    lead.generated_email_body = draft.body
    lead.ai_confidence_score = score
    
    # Jeśli lead był "ANALYZED" lub "NEW", teraz staje się "DRAFTED" (gotowy do wysyłki)
    if lead.status != "MANUAL_CHECK":
        lead.status = final_status
    
    session.commit()
    logger.info(f"   💾 Zapisano draft: '{draft.subject}'")

def _call_writer(client, company, decision_maker, lead_summary, step=1, feedback=None):
    """
    ENGINE: Silnik generujący treść.
    Korzysta z DNA Klienta (UVP, Case Studies) i Danych Firmy (Icebreaker).
    """
    
    # Wyciągamy DNA Agenta
    sender = client.sender_name
    uvp = client.value_proposition
    cases = client.case_studies
    tone = client.tone_of_voice
    constraints = client.negative_constraints
    
    # Dobieramy strategię do kroku kampanii
    if step == 1:
        strategy_prompt = f"""
        RODZAJ: COLD EMAIL (Initial Outreach)
        STRUKTURA: "The Bridge Model" (Icebreaker -> Problem -> Rozwiązanie -> CTA)
        CEL: Sprzedać ROZMOWĘ, a nie produkt.
        DŁUGOŚĆ: Krótko (max 100-120 słów). Szanuj czas CEO.
        
        INSTRUKCJE SPECJALNE:
        1. **ICEBREAKER**: Musisz zacząć od odniesienia się do firmy odbiorcy (użyj danych z 'ANALIZA RESEARCHERA'). 
           Nie pisz "Szanowni Państwo". Pisz "Cześć {decision_maker}".
        2. **PROBLEM**: Nawiąż do ich technologii lub branży (z analizy).
        3. **DOWÓD (Social Proof)**: Wykorzystaj to case study: "{cases[:200]}..."
        4. **CTA**: Niskie ryzyko. Np. "Warto pogadać?", "Czy to ma sens?".
        """
    elif step == 2:
        strategy_prompt = f"""
        RODZAJ: FOLLOW-UP (Przypomnienie)
        KONTEKST: Minęły 3 dni, brak odpowiedzi.
        STRUKTURA: "Quick Bump"
        TREŚĆ: "Cześć {decision_maker}, podbijam temat, żeby nie uciekł w gąszczu maili. Czy (krótka korzyść z UVP) jest teraz dla Was priorytetem?"
        DŁUGOŚĆ: Ultra krótko (3-4 zdania).
        """
    else:
        strategy_prompt = """
        RODZAJ: BREAK-UP EMAIL
        TREŚĆ: "Chyba nie trafiłem w dobry moment. Nie będę więcej męczył. Jeśli temat wróci na tapetę - jestem tutaj."
        CEL: Zostawić dobre wrażenie i furtkę na przyszłość.
        """

    # --- PROMPT INŻYNIERYJNY ---
    system_prompt = f"""
    Jesteś światowej klasy Copywriterem B2B, specjalistą od Cold Emailingu.
    Piszesz w imieniu: {sender} z firmy {client.name}.
    
    TWOJE DNA (OFINT):
    - Co robimy (UVP): {uvp}
    - Tone of Voice: {tone}
    - Czego NIE pisać: {constraints}
    
    ODBIORCA (TARGET):
    - Firma: {company.name}
    - Decydent: {decision_maker}
    - Analiza Researchera (BARDZO WAŻNE): 
    {lead_summary}
    
    ZADANIE:
    Napisz treść maila zgodnie z poniższą strategią.
    
    {strategy_prompt}
    
    FORMATOWANIE:
    Używaj tagów HTML: <p> dla akapitów, <b> dla kluczowych fraz (oszczędnie), <br> dla odstępów.
    NIE dodawaj tematu w treści body. Temat ma być osobno w polu 'subject'.
    """
    
    user_message = "Napisz ten draft."
    if feedback:
        user_message += f"\n\n🚨 KOREKTA PO AUDYCIE: Audytor odrzucił poprzednią wersję z uwagą: '{feedback}'. Popraw to natychmiast."

    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", user_message)])
    return (prompt | writer_llm).invoke({})

def _call_auditor(draft, company, client):
    """
    Strażnik Marki i Prawdy.
    """
    system_prompt = f"""
    Jesteś Audytorem Jakości (QA) w agencji marketingowej.
    
    TWOJE ZADANIE:
    Sprawdź draft maila pod kątem:
    1. **Halucynacji**: Czy mail wspomina o technologiach, których firma {company.name} NIE używa? (Sprawdź Stack: {company.tech_stack})
    2. **Zgodności z marką**: Czy mail narusza zakazy klienta? (Zakazy: {client.negative_constraints})
    3. **Personalizacji**: Czy mail wygląda na masowy spam? Jeśli tak -> ODRZUĆ.
    4. **Placeholderów**: Czy w tekście zostały nawiasy typu [Wstaw Imię]? -> ODRZUĆ.
    
    DRAFT:
    Temat: {draft.subject}
    Treść: {draft.body}
    
    Decyzja: True (Puszczamy) / False (Poprawka).
    Feedback: Konkretnie co poprawić.
    """
    
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", "Sprawdź to.")])
    return (prompt | auditor_llm).invoke({})