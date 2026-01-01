from typing import List, Optional
from pydantic import BaseModel, Field

class StrategyOutput(BaseModel):
    """Struktura wyjściowa Agenta Strategicznego"""
    
    thinking_process: str = Field(
        description="Krótkie uzasadnienie strategii. Dlaczego wybrałeś te słowa kluczowe?"
    )
    
    search_queries: List[str] = Field(
        description="Lista 10-20 precyzyjnych zapytań do wpisania w Google Maps. Np. 'Software House wrocław', 'Python development kraków'."
    )
    
    target_locations: List[str] = Field(
        description="Lista miast lub regionów, na których należy się skupić, jeśli dotyczy."
    )

class CompanyResearch(BaseModel):
    """Wynik analizy strony WWW firmy (Titan Enterprise Edition)"""
    
    company_name: str = Field(description="Oficjalna nazwa firmy zidentyfikowana na stronie.")
    
    summary: str = Field(
        description="Krótkie, menedżerskie podsumowanie co firma robi (max 2 zdania). Skup się na modelu biznesowym."
    )
    
    target_audience: str = Field(
        description="Kto jest ich idealnym klientem (ICP)? Np. 'e-commerce', 'banki', 'małe firmy budowlane'."
    )
    
    key_products: List[str] = Field(
        description="Główne produkty lub usługi oferowane przez firmę."
    )
    
    tech_stack: List[str] = Field(
        description="Wykryte technologie, języki programowania, frameworki (np. Python, React, AWS, HubSpot)."
    )
    
    decision_makers: List[str] = Field(
        description="Lista kluczowych osób w formacie 'Imie Nazwisko (Rola)'. Szukaj: CEO, CTO, Founder, Head of Sales. "
                    "WAŻNE: Pobieraj TYLKO osoby z zespołu firmy (Team, About Us). "
    )

    contact_emails: List[str] = Field(
        description="Lista adresów email znalezionych na stronie (np. contact@..., hello@..., sales@...)."
    )
    
    hiring_signals: List[str] = Field(
        description="Kogo aktualnie zatrudniają? (np. 'Szukają Senior Python Dev', 'Rekrutują Sales Managera'). "
                    "To kluczowy sygnał o budżecie i potrzebach."
    )
    
    icebreaker: str = Field(
        description="Hiper-personalizowane zdanie otwierające maila. Musi udowadniać, że zrobiliśmy research. "
                    "Np. 'Gratuluję nagrody Diamenty Forbesa', 'Widziałem, że rozwijacie zespół mobile'."
    )
    
    pain_points_or_opportunities: List[str] = Field(
        description="2-3 punkty zaczepienia do sprzedaży. Np. 'Szukają handlowców (potrzeba leadów)', "
                    "'Mają przestarzałą stronę (potrzeba redesignu)'."
    )

class EmailDraft(BaseModel):
    """Wygenerowany Draft Maila"""
    subject: str = Field(description="Temat wiadomości (krótki, intrygujący, max 5-7 słów).")
    body: str = Field(description="Treść maila w formacie HTML (używaj <p>, <b>, <br>).")
    rationale: str = Field(description="Dlaczego napisałeś to w ten sposób? Wyjaśnij strategię.")

class AuditResult(BaseModel):
    """Wynik kontroli jakości (Hallucination Killer)"""
    passed: bool = Field(description="Czy mail przeszedł test prawdy? True/False")
    feedback: str = Field(description="Jeśli False - co trzeba poprawić? Jeśli True - wpisz 'OK'.")
    hallucinations_detected: List[str] = Field(description="Lista faktów, które nie zgadzają się z danymi firmy.")

class ReplyAnalysis(BaseModel):
    """Analiza odpowiedzi od klienta"""
    is_interested: bool = Field(description="Czy klient wyraża chęć rozmowy/współpracy?")
    sentiment: str = Field(description="POSITIVE, NEGATIVE, lub NEUTRAL")
    summary: str = Field(description="Jednozdaniowe streszczenie intencji klienta.")
    suggested_action: str = Field(description="Co powinien zrobić człowiek? Np. 'Wyślij Calendly', 'Odpuść', 'Odpowiedz na pytanie X'.")