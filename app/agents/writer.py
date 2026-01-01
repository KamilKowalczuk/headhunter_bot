import os
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv

from app.database import Lead, Client, GlobalCompany
from app.schemas import EmailDraft, AuditResult

load_dotenv()

# Dwa modele: Jeden kreatywny (Pisarz), drugi surowy (Audytor)
writer_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.7, # Kreatywność włączona
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(EmailDraft)

auditor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.0, # Zero litości, same fakty
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(AuditResult)

def generate_email(session: Session, lead_id: int):
    """
    Proces: Pisanie -> Audyt -> (Ewentualna Poprawka) -> Zapis
    """
    lead = session.query(Lead).filter(Lead.id == lead_id).first()
    if not lead or not lead.campaign or not lead.campaign.client:
        print("❌ Błąd danych leada.")
        return

    client = lead.campaign.client
    company = lead.company
    
    print(f"✍️  WRITER: Piszę dla {company.name}...")

    # --- 1. PRZYGOTOWANIE KONTEKSTU ---
    # Definiujemy wartość domyślną NA SAMYM POCZĄTKU
    decision_maker_name = "Zespole" 
    
    # Dopiero teraz sprawdzamy, czy mamy lepsze dane
    if company.decision_makers and len(company.decision_makers) > 0:
        # Sprawdzamy czy to string (JSON) czy lista
        dms = company.decision_makers
        
        # Jeśli baza zwróciła listę (np. JSONB w Pythonie to lista)
        if isinstance(dms, list) and len(dms) > 0:
            raw_dm = dms[0]
        # Jeśli to string (w starszych wersjach bazy), to pewnie JSON-string
        elif isinstance(dms, str):
            import json
            try:
                parsed = json.loads(dms)
                if parsed and len(parsed) > 0:
                    raw_dm = parsed[0]
                else:
                    raw_dm = "Zespole"
            except:
                raw_dm = "Zespole"
        else:
            raw_dm = "Zespole"

        # Jeśli udało się wyciągnąć stringa (np. "Jan Kowalski (CEO)")
        if isinstance(raw_dm, str) and "(" in raw_dm:
            decision_maker_name = raw_dm.split("(")[0].strip()
        elif isinstance(raw_dm, str):
            decision_maker_name = raw_dm

    # --- 2. PISANIE (DRAFT 1) ---
    # Teraz decision_maker_name ZAWSZE ma wartość (albo imię, albo "Zespole")
    draft = _call_writer(
        client, 
        company, 
        decision_maker_name, 
        lead.ai_analysis_summary, 
        step=lead.step_number
    )
    
    # --- 3. AUDYT (HALLUCINATION KILLER) ---
    print("   👮 AUDITOR: Sprawdzam fakty...")
    audit = _call_auditor(draft, company)
    
    if not audit.passed:
        print(f"   ⚠️ AUDIT FAIL: {audit.feedback}")
        print("   🔄 WRITER: Poprawiam maila...")
        # Druga próba z feedbackiem audytora
        draft = _call_writer(client, company, decision_maker_name, lead.ai_analysis_summary, feedback=audit.feedback)
        
        # Drugi audyt (już tylko dla logów, zakładamy że poprawił)
        audit = _call_auditor(draft, company)
        if audit.passed:
             print("   ✅ AUDIT PASS (po poprawce).")
        else:
             print("   ⚠️ AUDIT FAIL (nawet po poprawce). Zapisuję, ale oznaczam do ręcznego sprawdzenia.")
             lead.status = "MANUAL_CHECK" # Nowy status dla trudnych przypadków
    else:
        print("   ✅ AUDIT PASS.")

    # --- 4. ZAPIS DO BAZY ---
    lead.generated_email_subject = draft.subject
    lead.generated_email_body = draft.body
    lead.ai_confidence_score = 90 if audit.passed else 40
    
    if lead.status != "MANUAL_CHECK":
        lead.status = "DRAFTED"
    
    session.commit()
    print(f"   💾 Draft zapisany (Temat: {draft.subject})")

def _call_writer(client, company, decision_maker, analysis, feedback=None, step=1):
    """
    Pomocnicza funkcja wywołująca LLM Pisarza.
    Obsługuje różne etapy sekwencji (Step 1, Step 2, Step 3).
    """
    sender_signature = client.sender_name if client.sender_name else f"Zespół {client.name}"

    # --- RÓŻNE STRATEGIE DLA RÓŻNYCH KROKÓW ---
    if step == 1:
        # KLASYCZNY OPENER (To co mieliśmy)
        goal_prompt = f"""
        TO JEST PIERWSZY KONTAKT (Cold Email).
        Cel: Zaintryguj i zachęć do rozmowy.
        Długość: Max 120 słów.
        Kontekst: Użyj informacji o stacku technologicznym ({company.tech_stack}).
        """
    elif step == 2:
        # FOLLOW-UP 1 (Szybkie przypomnienie)
        goal_prompt = f"""
        TO JEST FOLLOW-UP (Przypomnienie).
        Wysyłamy to 3 dni po pierwszym mailu, na który nie odpisali.
        Cel: Delikatnie przypomnij o sobie. Zapytaj, czy widzieli poprzednią wiadomość.
        Styl: Bardzo krótki i luźny (Max 50 słów). "Cześć, podbijam temat...".
        Nie powtarzaj całej oferty, tylko nawiąż do niej.
        """
    elif step == 3:
        # BREAK-UP EMAIL (Ostatnia próba)
        goal_prompt = f"""
        TO JEST OSTATNIA WIADOMOŚĆ (Break-up).
        Cel: Wywołaj "Fear Of Missing Out" albo daj im spokój.
        Styl: "Chyba jesteście zajęci, więc nie będę męczył. Jeśli jednak temat AI Was interesuje, mój kalendarz jest otwarty."
        Długość: Max 60 słów.
        """
    else:
        goal_prompt = "Napisz standardowy mail biznesowy."

    # --- GŁÓWNY PROMPT ---
    system_prompt = f"""
    Jesteś Copywriterem B2B. Piszesz w imieniu: {sender_signature}.
    
    ETAP KAMPANII: KROK {step}
    {goal_prompt}
    
    ODBIORCA: {company.name} (Stack: {company.tech_stack})
    DECYDENT: {decision_maker}
    
    ZASADY:
    1. Bądź naturalny. Zero korpo-bełkotu.
    2. PODPIS: {sender_signature}
    """
    
    user_prompt = "Napisz treść maila."
    if feedback:
        user_prompt += f"\n\nPOPRZEDNIA WERSJA ODRZUCONA: {feedback}"

    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", user_prompt)])
    return (prompt | writer_llm).invoke({})

def _call_auditor(draft, company):
    """Pomocnicza funkcja wywołująca LLM Audytora"""
    
    system_prompt = f"""
    Jesteś Audytorem Faktów (Hallucination Killer).
    Twoim zadaniem jest sprawdzić, czy Copywriter nie kłamie.
    
    FAKTY O FIRMIE (Prawda):
    - Nazwa: {company.name}
    - Stack: {company.tech_stack}
    - Problemy: {company.pain_points}
    
    DRAFT MAILA DO SPRAWDZENIA:
    Temat: {draft.subject}
    Treść: {draft.body}
    
    ZASADY AUDYTU:
    1. Czy w mailu wymieniono technologię, której NIE MA w liście 'Stack'? (HALLUCINATION ALERT)
    2. Czy mail obiecuje coś, co jest niemożliwe?
    3. Czy mail jest obraźliwy?
    
    Jeśli wykryjesz kłamstwo -> passed=False.
    """
    
    prompt = ChatPromptTemplate.from_messages([("system", system_prompt), ("human", "Dokonaj audytu.")])
    return (prompt | auditor_llm).invoke({})