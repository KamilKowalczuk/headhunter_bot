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
    temperature=0.72,
    top_p=0.85,
    top_k=40,
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(EmailDraft)

auditor_llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.0,
    google_api_key=os.getenv("GEMINI_API_KEY")
).with_structured_output(AuditResult)

# --- SAFETY NET: HTML VALIDATOR ---
def _sanitize_and_validate_html(html_content: str) -> str:
    """
    Naprawia i czyści HTML wygenerowany przez AI.
    Chroni przed rozsypaniem się maila w Outlooku.
    """
    if not html_content:
        return ""

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
            clean_html += f"</{tag}>" * missing

    # 3. Usuwanie wielokrotnych <br>
    clean_html = re.sub(r'(<br\s*/?>){3,}', '<br><br>', clean_html)
    
    return clean_html.strip()


def _extract_name_from_email(email: str) -> str:
    """Wyciąga imię z email (marta@firma.pl → Marta)."""
    if not email:
        return ""
    
    try:
        prefix = email.split('@')[0].lower()
        # Usuń liczby i znaki specjalne
        name = re.sub(r'[0-9._-]', ' ', prefix).strip()
        # Weź pierwsze słowo
        first_word = name.split()[0] if name.split() else ""
        # Kapitalizuj
        return first_word.capitalize() if len(first_word) > 2 else ""
    except:
        return ""


def _extract_decision_maker_name(dm_data) -> tuple:
    """
    Wyciąga imię decydenta z research.
    Returns: (name, confidence_score)
    """
    if not dm_data:
        return None, 0
    
    try:
        if isinstance(dm_data, list) and len(dm_data) > 0:
            first = dm_data[0]
            if isinstance(first, dict):
                name_str = first.get('name', '')
            else:
                name_str = str(first)
        else:
            name_str = str(dm_data)
        
        if not name_str:
            return None, 0
        
        # Parse: "Marta Kowalska (CEO)" → "Marta"
        name_clean = name_str.split('(')[0].split(',')[0].strip()
        first_name = name_clean.split(' ')[0]
        
        if len(first_name) > 2:
            return first_name, 70
        else:
            return None, 0
    except Exception as e:
        logger.warning(f"Decision maker parse error: {e}")
        return None, 0


def _match_email_to_decision_maker(email: str, decision_maker_name: str) -> tuple:
    """
    Inteligentne dopasowanie email → decision maker.
    Returns: (greeting_name, confidence_score)
    
    STRATEGY DLA MAKSYMALNEJ OTWIERALNOŚCI:
    - Jeśli mamy imię z research → ZAWSZE używaj
    - Jeśli generic email (kontakt@, info@) → Skip greeting, ale zacznij od hook'a
    - Jeśli email prefix to imię → użyj
    - Fallback: Start z hook'a bez powitania
    """
    
    # Extract imię z email
    email_name = _extract_name_from_email(email)
    
    # Check czy to generic mailbox
    generic_prefixes = ['kontakt', 'info', 'biuro', 'support', 'hello', 
                        'mail', 'contact', 'no-reply', 'noreply', 'team', 'sales', 'business']
    if email_name and email_name.lower() in generic_prefixes:
        logger.info(f"   🎯 Generic email detected: {email} → Skip greeting, start with hook")
        return None, 0
    
    # PRIORITY 1: Research decision maker (70% confidence = good enough)
    if decision_maker_name:
        logger.info(f"   ✅ Using research DM: {decision_maker_name}")
        return decision_maker_name, 85  # Lift to 85% - research is valuable
    
    # PRIORITY 2: Email prefix (if not generic)
    if email_name and len(email_name) > 2:
        logger.info(f"   📧 Using email prefix: {email_name}")
        return email_name, 75
    
    # PRIORITY 3: No name available
    logger.info(f"   ⚠️ No name found, starting with hook")
    return None, 0



def _detect_hallucination_markers(text: str) -> list:
    """Detektuje znaki halucynacji (placeholders, niespójności)."""
    markers = []
    
    # CRITICAL: Placeholder detection
    if re.search(r'\[.*?\]', text):
        markers.append("placeholder_detected")
        logger.error("   🚨 PLACEHOLDER FOUND - REGENERATING")
    
    if re.search(r'\{.*?\}', text):
        markers.append("curly_placeholder_detected")
        logger.error("   🚨 CURLY PLACEHOLDER FOUND - REGENERATING")
    
    # Generic corporate speak
    generic_phrases = [
        r'mamy przyjemność',
        r'wychodzimy naprzeciw',
        r'kompleksowe rozwiązania',
        r'w dzisiejszych czasach',
        r'transformacja cyfrowa',
        r'innowacyjne podejście'
    ]
    
    for phrase in generic_phrases:
        if re.search(phrase, text, re.IGNORECASE):
            markers.append(f"generic_phrase: {phrase}")
    
    # Repeated patterns
    words = text.split()
    if len(words) > 10:
        word_freq = {}
        for word in words:
            word_freq[word] = word_freq.get(word, 0) + 1
        
        if max(word_freq.values()) > 5:
            markers.append("word_repetition_detected")
    
    return markers


def _validate_against_data(email_body: str, company_data: dict, client_data: dict) -> dict:
    """Sprawdza czy mail nie halucynuje."""
    validation_result = {
        "is_hallucinating": False,
        "violations": [],
        "confidence_score": 100
    }
    
    # Hallucination markers
    hallucination_markers = _detect_hallucination_markers(email_body)
    if hallucination_markers:
        validation_result["is_hallucinating"] = True
        validation_result["violations"].extend(hallucination_markers)
        validation_result["confidence_score"] -= 50
    
    return validation_result

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

    # --- 1. EXTRACT RESEARCH DECISION MAKER ---
    research_dm_name, dm_confidence = _extract_decision_maker_name(company.decision_makers)
    logger.info(f"   🔍 Research DM: {research_dm_name} (confidence: {dm_confidence}%)")

    # --- 2. EMAIL-TO-NAME MATCHING ---
    # --- 2. EMAIL-TO-NAME MATCHING ---
    target_email = lead.target_email or ""
    greeting_name, email_confidence = _match_email_to_decision_maker(
        email=target_email,
        decision_maker_name=research_dm_name
    )
    
    # FALLBACK: Jeśli nic nie ma, użyj imienia z researchu
    if not greeting_name and research_dm_name:
        greeting_name = research_dm_name
        email_confidence = 85
        logger.info(f"   🔄 Fallback to research DM: {greeting_name}")
    
    logger.info(f"   📧 Final greeting: {greeting_name} (confidence: {email_confidence}%)")


    # --- 3. SENDER INFO ---
    sender_name = client.sender_name or None
    sender_company = client.name or None
    
    if sender_name and sender_company:
        sender_info = f"{sender_name} @ {sender_company}"
    elif sender_name:
        sender_info = sender_name
    else:
        sender_info = None
    
    logger.info(f"   👤 Sender: {sender_info}")

    # --- 4. FOOTER CHECK ---
    has_footer = bool(getattr(client, "html_footer", None))
    logger.info(f"   📎 Footer available: {has_footer}")

    # --- 5. GENERATE EMAIL ---
    try:
        draft = _call_writer(
            client=client,
            company=company,
            greeting_name=greeting_name,
            research_dm_name=research_dm_name,
            lead_summary=lead.ai_analysis_summary or "Brak specyficznych danych.",
            step=lead.step_number,
            mode=mode,
            sender_name=sender_name,
            sender_company=sender_company,
            has_footer=has_footer
        )
    except Exception as e:
        logger.error(f"❌ Writer error: {e}")
        return
    
    # --- 6. VALIDATE HTML ---
    safe_body = _sanitize_and_validate_html(draft.body)

    # --- 7. HALLUCINATION CHECK ---
    validation = _validate_against_data(
        safe_body,
        {'tech_stack': company.tech_stack, 'pain_points': company.pain_points},
        {'case_studies': client.case_studies}
    )
    
    if validation["is_hallucinating"]:
        logger.warning(f"⚠️  HALLUCINATION DETECTED: {validation['violations']}")
        logger.info(f"   🔄 Regenerating with strict mode...")
        
        draft = _call_writer(
            client=client,
            company=company,
            greeting_name=greeting_name,
            research_dm_name=research_dm_name,
            lead_summary=lead.ai_analysis_summary or "Brak specyficznych danych.",
            step=lead.step_number,
            mode=mode,
            sender_name=sender_name,
            sender_company=sender_company,
            has_footer=has_footer,
            strict_mode=True
        )
        safe_body = _sanitize_and_validate_html(draft.body)
        validation = _validate_against_data(safe_body, {}, {})

    score = validation["confidence_score"]
    
    # --- 8. SAVE TO DATABASE ---
    lead.generated_email_subject = draft.subject
    lead.generated_email_body = safe_body
    lead.ai_confidence_score = int(score)
    
    if lead.status != "MANUAL_CHECK":
        lead.status = "DRAFTED"
    
    lead.last_action_at = datetime.now()
    session.commit()
    logger.info(f"   💾 Draft saved (Confidence: {score:.0f}%): '{draft.subject}'")


def _call_writer(
    client,
    company,
    greeting_name,
    research_dm_name,
    lead_summary,
    step=1,
    feedback=None,
    mode="SALES",
    sender_name=None,
    sender_company=None,
    has_footer=False,
    strict_mode=False
):
    """
    ENGINE: Silnik generujący treść.
    """
    
    uvp = client.value_proposition or "Wspieramy firmy B2B"
    cases = client.case_studies or ""
    tone = client.tone_of_voice or "Profesjonalny, konkretny"
    constraints = client.negative_constraints or ""
    
    # --- GREETING LOGIC ---
    if greeting_name:
        greeting_instruction = f"Zacznij: 'Cześć {greeting_name},'"
    else:
        greeting_instruction = "Nie używaj powitania - zacznij prosto od hook'a: 'Widzę, że...'"
    
    # --- SIGNATURE LOGIC ---
    if has_footer:
        signature_instruction = (
            "KONIEC MAILA: Mail kończy się TUŻ PO Call to Action lub jednym zdaniu. "
            "Nie pisz ŻADNEGO podpisu (Pozdrawiam, itp.) - zostanie doklejony automatycznie. "
            "Ostatnie słowo to albo pytanie albo propozycja."
        )
    elif sender_name and sender_company:
        signature_instruction = f"Zakończ maila: 'Pozdrawiam,<br/>{sender_name}<br/>@ {sender_company}'"
    elif sender_name:
        signature_instruction = f"Zakończ maila: 'Pozdrawiam,<br/>{sender_name}'"
    else:
        signature_instruction = (
            "Zakończ mail naturalnie - ostatnie zdanie to pytanie lub propozycja, bez podpisu."
        )

    # --- SYSTEM PROMPT ---
    system_prompt = f"""Jesteś Business Developerem z 15-letnim doświadczeniem w sprzedaży B2B.
Pisz maile, które wyglądają jak napisane przez człowieka, który spędził 30-60 minut na research i drafting.

TONE: Casual, direct, curious, humble, human. Słowa: "myślę", "chyba", "może".
NEVER: "mamy przyjemność", "wychodzimy naprzeciw", "kompleksowe rozwiązania".

VERIFIED DATA:
- Company: {company.name}
- Lead Notes: {lead_summary}
- Your UVP: {uvp}
- Cases: {cases if cases else "(Brak)"}

CONSTRAINTS: {constraints if constraints else "(Brak)"}

GREETING: {greeting_instruction}
SIGNATURE: {signature_instruction}

CRITICAL RULES:
1. NO placeholders: [imię], {{{{firma}}}}, [data]
2. NO generic phrases
3. Use ONLY verified data
4. If no data about something - SKIP that topic
5. Max 150 words
6. Subject line max 4 words"""

    if strict_mode:
        system_prompt += "\n\nSTRICT MODE: ONLY verified data. No speculation. No guesses."

    # --- TASK PROMPT ---
    if mode == "JOB_HUNT":
        if step == 1:
            task_prompt = """SCENARIO: Job Application

STRUCTURE:
1. Hook (1 line): Something specific from research
2. Your Superpower (1-2 lines): What you do
3. Proof (1 line): Specific project or skill
4. Soft CTA (1 line): "Szukacie teraz?", "Warte dyskusji?"

LENGTH: Max 4 lines. Mobile-readable.
TONE: "I'm expert, looking for good fit" (not desperate)"""
        else:
            task_prompt = """SCENARIO: Follow-Up

STRUCTURE:
1. Natural continuation
2. Add NEW value (not just bumping)
3. Soft touch

TONE: Helpful, assertive"""
    else:  # SALES
        if step == 1:
            task_prompt = """SCENARIO: Cold Email (OPENING)

SUBJECT: Intriguing, not salesy. Max 4 words. Examples: "React i wydajność?", "Skalowanie?", "Może się przyda"

BODY:
- Hook (1 line): "Widzę, że używacie X", "Wiele zespołów ma problem z Y", "Chyba ostatnio się rozszerzyliście?"
- Bridge (1-2 lines): Show you understand their challenge
- Offer (1-2 lines): "Pomagamy w X", "Robimy Y"
- CTA (1 line): "Macie 15 minut?", "Opowiedz - jak wygląda Wasz proces?"

LENGTH: 100-150 words. Phone-readable.
TONE: Colleague from industry, not salesman"""
        else:
            task_prompt = """SCENARIO: Follow-Up

STRUCTURE:
1. Natural continuation
2. NEW insight/observation/advice (not just bumping)
3. Soft CTA

TONE: Helpful, not pushy"""

    full_prompt = system_prompt + "\n\n" + task_prompt

    user_message = "Generuj email subject + body (HTML). Zero placeholders. Verified data only."
    if feedback:
        user_message += f"\n\nFeedback: {feedback}"

    prompt = ChatPromptTemplate.from_messages([
        ("system", full_prompt),
        ("human", user_message)
    ])

    return (prompt | writer_llm).invoke({})


def _call_auditor(draft, company, client):
    """
    Opcjonalny krok weryfikacji.
    """
    system_prompt = f"""Jesteś krytycznym korektorem emaili.

CRITERIA:
1. Human-like: Czy brzmi jak człowiek?
2. Specific: Concrete details from research
3. No hallucinations: All verified data
4. Mobile-readable: Short paragraphs
5. Clear CTA: Do they know what to do?

SCORING: 0-100 (90+ = send, 70-89 = improve, <70 = reject)"""

    user_prompt = f"""Subject: {draft.subject}
Body: {draft.body}

Oceń i daj konkretny feedback."""

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", user_prompt)
    ])

    return (prompt | auditor_llm).invoke({})
