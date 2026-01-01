import os
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

# Importy z Twojej aplikacji
from app.database import Client
from app.schemas import StrategyOutput
# Upewnij się, że masz plik memory_utils.py (z poprzedniego kroku)
from app.memory_utils import load_used_queries, save_used_queries

load_dotenv()

# Inicjalizacja modelu
# Zmieniamy temperaturę na 0.7, żeby AI było bardziej kreatywne w wymyślaniu nisz
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.7, 
    google_api_key=os.getenv("GEMINI_API_KEY")
)

# Wymuszamy strukturę wyjściową
structured_llm = llm.with_structured_output(StrategyOutput)

def generate_strategy(client: Client, raw_intent: str, campaign_id: int) -> StrategyOutput:
    """
    Generuje UNIKALNE zapytania do Google Maps.
    Analizuje historię (memory_utils) i wymusza eksplorację nowych nisz/lokalizacji.
    """
    
    # 1. ŁADUJEMY PAMIĘĆ (Co już było szukane?)
    used_queries = load_used_queries(campaign_id)
    # Bierzemy ostatnie 50 zapytań, żeby dać kontekst AI, ale nie przeładować promptu
    used_queries_str = ", ".join(used_queries[-50:]) if used_queries else "BRAK (To pierwsze uruchomienie)"
    
    system_prompt = """
    Jesteś Ekspertem Strategii B2B i OSINT (Open Source Intelligence).
    Twoim celem jest wygenerowanie zapytań do GOOGLE MAPS, aby znaleźć firmy, których JESZCZE NIE MAMY w bazie.
    
    DANE MOJEGO KLIENTA:
    - Nazwa: {sender_name}
    - Branża: {sender_industry}
    - Oferta: {value_proposition}
    - Kogo szukamy (ICP): {icp}
    
    CEL KAMPANII: {intent}
    
    !!! HISTORIA ZAPYTAŃ (TE HASŁA SĄ JUŻ SPALONE - NIE UŻYWAJ ICH):
    [{used_queries_str}]
    
    TAKTYKA "INFINITE SEARCH" (Jak znaleźć nowe firmy?):
    1. Unikaj duplikatów z Historii.
    2. Jeśli ogólne miasto (np. "Warszawa") było już użyte -> UŻYJ DZIELNIC lub MIAST SATELICKICH.
       (np. "Software House Mokotów", "Agencja Marketingowa Piaseczno").
    3. Jeśli ogólna branża była użyta -> UŻYJ SYNONIMÓW lub NISZ.
       (np. zamiast "Software House", wpisz: "Python Development", "Sklep PrestaShop", "Wdrożenia CRM").
    4. Format: "[Rodzaj Firmy] [Lokalizacja]".
    
    Wygeneruj od 5 do 8 zupełnie nowych, precyzyjnych zapytań.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Przygotuj unikalną strategię pod Google Maps.")
    ])

    chain = prompt | structured_llm

    print(f"🧠 STRATEGY: Analizuję historię ({len(used_queries)} rekordów)... Generuję świeże zapytania.")

    # Przekazujemy dane
    result = chain.invoke({
        "sender_name": client.name,
        "sender_industry": client.industry,
        "value_proposition": client.value_proposition,
        "icp": client.ideal_customer_profile,
        "intent": raw_intent,
        "used_queries_str": used_queries_str # Przekazujemy historię do promptu
    })

    # 2. ZAPISUJEMY NOWE ZAPYTANIA DO PAMIĘCI
    if result.search_queries:
        save_used_queries(campaign_id, result.search_queries)

    return result