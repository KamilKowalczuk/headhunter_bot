import os
import re
import logging
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

# Importy z Twojej aplikacji
from app.database import Client
from app.schemas import StrategyOutput
from app.memory_utils import load_used_queries, save_used_queries

load_dotenv()

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("strategy")

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
    mode = getattr(client, "mode", "SALES")
    
    if mode == "JOB_HUNT":
        # --- STRATEGIA REKRUTACYJNA (HUNTER MODE) ---
        system_prompt = """
        Jesteś Architektem Kariery i Specjalistą OSINT. Twoim zadaniem jest zhackowanie algorytmu wyszukiwania Google Maps, aby znaleźć ukryte perły rynku pracy dla Twojego klienta.

        ### PROFIL KANDYDATA (Input):
        - Imię: {sender_name}
        - Branża: {sender_industry}
        - Supermoce (Skills): {value_proposition}
        - Cel (Dream Company): {icp}
        - Intencja: {intent}

        ### HISTORIA ZAPYTAŃ (Blacklist - Tego NIE wolno Ci użyć):
        [{used_queries_str}]

        ### TWOJA MISJA:
        Musisz wygenerować 5-8 zapytań do Google Maps, które odkryją firmy technologiczne, startupy i software house'y, które NIEkoniecznie mają wystawione ogłoszenia na portalach pracy (Ukryty Rynek).

        ### MASTER STRATEGY GUIDELINES (Non-Negotiable):
        
        1. **Precyzja Geograficzna:** 
           - Zamiast "Warszawa", użyj dzielnic ORAZ punktów orientacyjnych (np. "Software House near Rondo ONZ Warsaw", "AI Startup near Galeria Krakowska")
           - Google Maps lepiej rozumie POI (Points of Interest) niż same nazwy dzielnic
           - Miasta satelickie to złoto (mniejsza konkurencja)

        2. **Dywersyfikacja Semantyczna:**
           - Zamiast "Software House", użyj: "Agencja Python", "SaaS Development", "Fintech Startup", "AI Lab", "E-commerce implementation"
           - Szukaj po technologiach: "React Agency", "Django Studio", "Cloud Native Company"

        3. **HIDDEN JOB MARKET TACTICS:**
           - "Series A Startup [Tech]" - Firmy z funding = hiring mode
           - "[Tech] Scale-up 20-50 employees" - Growth phase = potrzebują ludzi
           - "Cloud transformation [City]" - Migration projects = need talent
           - "Startup of the Year [Location]" - Award winners = expansion

        4. **Wykluczenia:** 
           - NIGDY: "Biuro pracy", "Agencja rekrutacyjna" - szukamy BEZPOŚREDNICH pracodawców

        5. **STRICT SEMANTIC UNIQUENESS:**
           - NIE generuj zapytań semantycznie identycznych (zmiana kolejności słów = DUPLIKAT)
           - "Software House Kraków" vs "Kraków Software House" ← TO SAMO, ZABRONIONE
           - Jeśli lokalizacja + branża się powtarza → ZMIEŃ NISZĘ lub TECHNOLOGIĘ

        ### FORMAT WYJŚCIOWY:
        Czysty string zapytania, np. "React Native Studio near Dworzec Główny Wrocław"

        Twoje zapytania muszą być RÓŻNORODNE. Nie generuj 5 razy tego samego z inną dzielnicą. Mieszaj branże, technologie i lokalizacje.
        """
    else:
        # --- STRATEGIA SPRZEDAŻOWA (SALES SNIPER MODE) ---
        system_prompt = """
        Jesteś Strategiem Lead Generation B2B o IQ 190. Twoim jedynym celem jest nakarmienie lejka sprzedażowego kalorycznymi leadami, których konkurencja nie widzi.

        ### DANE SPRZEDAWCY (Twój Klient):
        - Nazwa: {sender_name}
        - Branża: {sender_industry}
        - Value Proposition: {value_proposition}
        - Idealny Klient (ICP): {icp}
        - Cel Kampanii: {intent}

        ### HISTORIA ZAPYTAŃ (Blacklist - Tego NIE wolno Ci użyć):
        [{used_queries_str}]

        ### STRATEGIA "LATERAL SEARCH" - MASTER GUIDELINES:
        Google Maps to wyszukiwarka słów kluczowych, a nie intencji. Musisz przekładać ICP na fizyczne szyldy firm.
        
        1. **Zasada Synonimów Biznesowych:**
           - Jeśli szukamy "Restauracji", szukaj też: "Bistro", "Gastrobar", "Sushi", "Pizzeria", "Fine Dining"
           - Jeśli szukamy "Firm budowlanych", szukaj też: "Deweloper", "Generalny Wykonawca", "Remonty biur", "Usługi dekarskie"

        2. **Mikro-Lokalizacje + POI:**
           - Unikaj ogólnych miast (np. "Warszawa"). Algorytm Google utnie wyniki po 20 rekordach
           - Wchodź w dzielnice + konkretne punkty (np. "Bistro near Stare Miasto Kraków", "Deweloper near Galeria Mokotów")
           - Miasta satelickie to ukryte złoto

        3. **Kreatywne Nisze:**
           - Zastanów się, kto MA PIENIĄDZE i potrzebuje usług {sender_industry}, ale nie jest oczywistym celem
           - Szukaj branż w fazie wzrostu lub transformacji

        4. **STRICT SEMANTIC UNIQUENESS:**
           - NIE generuj zapytań semantycznie identycznych (zmiana kolejności słów = DUPLIKAT)
           - "Restaurant Warsaw Mokotów" vs "Mokotów Restaurant Warsaw" ← TO SAMO, ZABRONIONE
           - Pod żadnym pozorem nie powtarzaj zapytań z "HISTORIA ZAPYTAŃ"
           - Jeśli lokalizacja + branża się powtarza → ZMIEŃ NISZĘ lub MIKRO-LOKALIZACJĘ

        ### FORMAT WYJŚCIOWY:
        "[Nisza/Branża] [Konkretna Lokalizacja + POI jeśli duże miasto]"
        
        Przykłady:
        - "Fine Dining near Rynek Główny Kraków"
        - "Software House Gdańsk Oliwa"
        - "Dental Clinic near Galeria Krakowska"

        Masz wygenerować od 5 do 8 chirurgicznie precyzyjnych zapytań.
        """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Analizuj ICP i generuj listę celów.")
    ])

    chain = prompt | structured_llm

    print(f"🧠 STRATEGY [{mode}]: Analizuję historię... Generuję zapytania.")

    # Przekazujemy dane
    result = chain.invoke({
        "sender_name": client.name,
        "sender_industry": client.industry,
        "value_proposition": client.value_proposition,
        "icp": client.ideal_customer_profile,
        "intent": raw_intent,
        "used_queries_str": used_queries_str
    })

    # 3. VALIDATION & DEDUPLICATION
    if result.search_queries:
        # Remove duplicates (case-insensitive + semantic)
        unique_queries = []
        seen_normalized = set()
        
        for q in result.search_queries:
            # Clean query
            q_clean = q.strip()
            
            # Skip empty or too short
            if not q_clean or len(q_clean) < 5:
                logger.warning(f"⚠️ Skipping too short query: '{q_clean}'")
                continue
            
            # Check for placeholders
            if '[' in q_clean or '{' in q_clean:
                logger.warning(f"🚨 PLACEHOLDER DETECTED: '{q_clean}' - SKIPPING")
                continue
            
            # Normalize (lowercase + sorted words for semantic dedup)
            words = sorted(q_clean.lower().split())
            normalized = " ".join(words)
            
            # Check if semantically unique
            if normalized in seen_normalized:
                logger.warning(f"⚠️ SEMANTIC DUPLICATE: '{q_clean}' - SKIPPING")
                continue
            
            # Passed all checks
            unique_queries.append(q_clean)
            seen_normalized.add(normalized)
        
        logger.info(f"✅ Generated {len(unique_queries)} unique queries (filtered from {len(result.search_queries)})")
        
        # Update result
        result.search_queries = unique_queries
        
        # Save to memory
        if unique_queries:
            save_used_queries(campaign_id, unique_queries)
        else:
            logger.error(f"❌ No valid queries after validation - regeneration needed")
    
    return result
