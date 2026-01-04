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

        ### ZASADY GENEROWANIA ZAPYTAŃ (Protocol 11/10):
        1. **Precyzja Geograficzna:** Nie wpisuj "Warszawa". Wpisuj dzielnice biznesowe (np. "Wola", "Mokotów", "Zabłocie") lub miasta satelickie, gdzie konkurencja kandydatów jest mniejsza.
        2. **Dywersyfikacja Semantyczna:**
           - Zamiast "Software House", użyj: "Agencja Python", "SaaS Development", "Fintech Startup", "AI Lab", "E-commerce implementation".
           - Szukaj po technologiach, jeśli to ma sens (np. "React Agency").
        3. **Wykluczenia:** Nie szukaj "Biuro pracy" ani "Agencja rekrutacyjna". Szukamy BEZPOŚREDNICH pracodawców.
        4. **Format wyjściowy:** Czysty string zapytania, np. "React Native Studio Wrocław Krzyki".

        Twoje zapytania muszą być RÓŻNORODNE. Nie generuj 5 razy tego samego z inną dzielnicą. Mieszaj branże z lokalizacjami.
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

        ### STRATEGIA "LATERAL SEARCH" (Protocol 11/10):
        Google Maps to wyszukiwarka słów kluczowych, a nie intencji. Musisz przekładać ICP na fizyczne szyldy firm.
        
        1. **Zasada Synonimów Biznesowych:**
           - Jeśli szukamy "Restauracji", szukaj też: "Bistro", "Gastrobar", "Sushi", "Pizzeria", "Fine Dining".
           - Jeśli szukamy "Firm budowlanych", szukaj też: "Deweloper", "Generalny Wykonawca", "Remonty biur", "Usługi dekarskie".
        2. **Mikro-Lokalizacje:**
           - Unikaj ogólnych miast (np. "Warszawa"). Algorytm Google utnie wyniki po 20 rekordach.
           - Wchodź w dzielnice, ulice biznesowe, miasta ościenne. To tam są nieodkryci klienci.
        3. **Kreatywne Nisze:**
           - Zastanów się, kto MA PIENIĄDZE i potrzebuje usług {sender_industry}, ale nie jest oczywistym celem.
        4. **Anti-Duplication Shield:**
           - Pod żadnym pozorem nie powtarzaj zapytań z sekcji "HISTORIA ZAPYTAŃ". To marnowanie budżetu.

        Masz wygenerować od 5 do 8 chirurgicznie precyzyjnych zapytań w formacie: "[Nisza/Branża] [Konkretna Lokalizacja]".
        """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Analizuj ICP i generuj listę celów.")
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