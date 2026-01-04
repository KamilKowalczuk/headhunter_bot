import os
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

# Importy z Twojej aplikacji
from app.database import Client
from app.schemas import StrategyOutput
from app.memory_utils import load_used_queries, save_used_queries

load_dotenv()

# Inicjalizacja modelu
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
    Obsługuje dwa tryby: SALES (Szukanie klientów) oraz JOB_HUNT (Szukanie pracodawców).
    """
    
    # 1. ŁADUJEMY PAMIĘĆ
    used_queries = load_used_queries(campaign_id)
    used_queries_str = ", ".join(used_queries[-50:]) if used_queries else "BRAK"
    
    # 2. WYBÓR TRYBU (POLIMORFIZM)
    mode = getattr(client, "mode", "SALES") # Domyślnie SALES jeśli brak pola
    
    if mode == "JOB_HUNT":
        # --- STRATEGIA REKRUTACYJNA ---
        system_prompt = """
        Jesteś Ekspertem Rynku Pracy i Headhunterem Technologicznym.
        Twoim celem jest znalezienie firm (Pracodawców), do których użytkownik może aplikować o pracę.
        Szukamy firm z potencjałem rekrutacyjnym, nawet jeśli nie mają oficjalnych ogłoszeń (Ukryty Rynek Pracy).
        
        DANE KANDYDATA (UŻYTKOWNIKA):
        - Imię/Marka: {sender_name}
        - Specjalizacja: {sender_industry}
        - Umiejętności (Bio): {value_proposition}
        - Wymarzony Pracodawca (Target): {icp}
        
        CEL POSZUKIWAŃ: {intent}
        
        !!! HISTORIA ZAPYTAŃ (TE HASŁA JUŻ BYŁY - UNIKAJ ICH):
        [{used_queries_str}]
        
        TAKTYKA "JOB HUNTER":
        1. Szukaj firm pasujących do profilu technologicznego kandydata.
        2. Używaj fraz określających typ firmy: "Software House", "Agencja Interaktywna", "Startup AI", "Fintech".
        3. Łącz to z lokalizacjami (Dzielnice, Miasta).
        4. UNIKAJ ogólnych haseł typu "Praca Warszawa". Szukamy FIRM, a nie ogłoszeń.
        5. Format zapytania do map: "[Typ Firmy/Technologia] [Miasto/Dzielnica]".
           Np. "Django Software House Wrocław", "Agencja SEO Mokotów".
        
        Wygeneruj od 5 do 8 unikalnych zapytań do Google Maps.
        """
    else:
        # --- STRATEGIA SPRZEDAŻOWA (STANDARD) ---
        system_prompt = """
        Jesteś Ekspertem Strategii B2B i OSINT.
        Twoim celem jest wygenerowanie zapytań do GOOGLE MAPS, aby znaleźć potencjalnych KLIENTÓW.
        
        DANE MOJEGO KLIENTA (SPRZEDAWCY):
        - Nazwa: {sender_name}
        - Branża: {sender_industry}
        - Oferta (UVP): {value_proposition}
        - Kogo szukamy (ICP): {icp}
        
        CEL KAMPANII: {intent}
        
        !!! HISTORIA ZAPYTAŃ (TE HASŁA JUŻ BYŁY - UNIKAJ ICH):
        [{used_queries_str}]
        
        TAKTYKA "INFINITE SEARCH":
        1. Unikaj duplikatów z Historii.
        2. Eksploruj dzielnice i miasta satelickie.
        3. Używaj synonimów branż i nisz (np. zamiast "Sklep", wpisz "Hurtownia odzieży").
        4. Format: "[Rodzaj Firmy] [Lokalizacja]".
        
        Wygeneruj od 5 do 8 zupełnie nowych, precyzyjnych zapytań.
        """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Przygotuj strategię wyszukiwania.")
    ])

    chain = prompt | structured_llm

    print(f"🧠 STRATEGY [{mode}]: Analizuję historię... Generuję zapytania.")

    # Przekazujemy dane (klucze są te same, ale prompt interpretuje je inaczej)
    result = chain.invoke({
        "sender_name": client.name,
        "sender_industry": client.industry,
        "value_proposition": client.value_proposition,
        "icp": client.ideal_customer_profile,
        "intent": raw_intent,
        "used_queries_str": used_queries_str
    })

    # 3. ZAPISUJEMY NOWE ZAPYTANIA DO PAMIĘCI
    if result.search_queries:
        save_used_queries(campaign_id, result.search_queries)

    return result