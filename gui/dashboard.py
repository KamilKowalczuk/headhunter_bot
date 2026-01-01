import streamlit as st
import pandas as pd
import time
import sys
import os
from sqlalchemy.orm import Session
from sqlalchemy import func, exc

# --- IMPORTY BACKENDU ---
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from app.database import engine, Client, Campaign, Lead, GlobalCompany
    from app.agents.strategy import generate_strategy
    from app.agents.scout import run_scout
    from app.agents.researcher import analyze_lead
    from app.agents.writer import generate_email
    from app.scheduler import process_followups, save_draft_via_imap
    from app.agents.inbox import check_inbox
except ImportError as e:
    st.error(f"❌ BŁĄD IMPORTÓW: Nie można załadować modułów backendu. Sprawdź strukturę katalogów.\nDetale: {e}")
    st.stop()

# --- KONFIGURACJA UI ---
st.set_page_config(
    page_title="Agency OS | Titan Edition",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .block-container {padding-top: 1rem; padding-bottom: 2rem;}
    h1 {font-size: 2.2rem; font-weight: 800; color: #111827;}
    h2 {font-size: 1.6rem; font-weight: 700; color: #374151;}
    h3 {font-size: 1.3rem; font-weight: 600; color: #4B5563;}
    
    [data-testid="metric-container"] {
        background-color: #FFFFFF;
        border: 1px solid #E5E7EB;
        padding: 16px !important;
        border-radius: 12px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        transition: all 0.2s ease-in-out;
    }
    [data-testid="metric-container"]:hover {
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.05);
        border-color: #D1D5DB;
    }
    [data-testid="metric-container"] label { color: #6B7280 !important; font-weight: 500; }
    [data-testid="metric-container"] div[data-testid="stMetricValue"] { color: #111827 !important; font-weight: 700; }
    .stButton button { border-radius: 8px; font-weight: 600; padding: 0.5rem 1rem; }
</style>
""", unsafe_allow_html=True)

# Folder plików
FILES_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'files')
os.makedirs(FILES_DIR, exist_ok=True)

def get_db():
    return Session(engine)

def save_uploaded_file(uploaded_file):
    if uploaded_file is not None:
        file_path = os.path.join(FILES_DIR, uploaded_file.name)
        with open(file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        return uploaded_file.name
    return None

# Inicjalizacja sesji z obsługą błędów
session = get_db()

try:
    # ==============================================================================
    # SIDEBAR: NAWIGACJA
    # ==============================================================================
    with st.sidebar:
        st.image("https://cdn-icons-png.flaticon.com/512/12565/12565256.png", width=60)
        st.title("TITAN OS")
        st.caption("AI Sales Engine v3.1")
        st.markdown("---")

        all_clients = session.query(Client).all()
        client_names = [c.name for c in all_clients]
        client_names.insert(0, "➕ DODAJ FIRMĘ")

        selected_option = st.radio("WYBIERZ AGENTA:", client_names, index=1 if len(all_clients) > 0 else 0)

        client = None
        if selected_option != "➕ DODAJ FIRMĘ":
            client = session.query(Client).filter(Client.name == selected_option).first()
            st.markdown("---")
            
            if client:
                status_color = "🟢" if client.status == "ACTIVE" else "🔴"
                st.markdown(f"### Status: {status_color} {client.status}")
                
                c1, c2 = st.columns(2)
                if client.status == "ACTIVE":
                    if c1.button("PAUZA"):
                        client.status = "PAUSED"
                        session.commit()
                        st.rerun()
                else:
                    if c1.button("START"):
                        client.status = "ACTIVE"
                        session.commit()
                        st.rerun()
                
                # Bezpieczne obliczanie limitu
                sent_today = session.query(Lead).join(Campaign).filter(
                    Campaign.client_id == client.id, 
                    Lead.status == "SENT",
                    func.date(Lead.sent_at) == func.current_date()
                ).count()
                
                limit = client.daily_limit if (client.daily_limit and client.daily_limit > 0) else 50
                progress_val = min(sent_today / limit, 1.0)
                st.progress(progress_val, text=f"Limit: {sent_today}/{limit}")

    # ==============================================================================
    # VIEW: ONBOARDING
    # ==============================================================================
    if selected_option == "➕ DODAJ FIRMĘ":
        st.title("📝 Onboarding Nowej Firmy")
        st.markdown("Skonfiguruj pełne DNA nowego agenta, aby generował leady klasy Enterprise.")
        
        with st.form("new_client_form"):
            st.subheader("1. Tożsamość i Marka")
            col1, col2, col3 = st.columns(3)
            with col1: name = st.text_input("Nazwa Firmy (ID)", placeholder="Titan Solutions")
            with col2: industry = st.text_input("Branża", placeholder="Software House / Marketing")
            with col3: sender = st.text_input("Nadawca (Imię Nazwisko)", placeholder="Jan Kowalski")

            st.subheader("2. Mózg Strategiczny (AI Context)")
            c_uvp, c_icp = st.columns(2)
            with c_uvp: uvp = st.text_area("Value Proposition", height=100)
            with c_icp: icp = st.text_area("Ideal Customer Profile", height=100)
            
            c_tone, c_case = st.columns(2)
            with c_tone:
                tone = st.text_input("Tone of Voice")
                neg = st.text_area("Negative Constraints", height=80)
            with c_case: cases = st.text_area("Case Studies", height=150)

            st.subheader("3. Infrastruktura Wysyłkowa")
            t1, t2, t3 = st.columns(3)
            with t1:
                smtp_host = st.text_input("SMTP Host", "smtp.gmail.com")
                imap_host = st.text_input("IMAP Host", "imap.gmail.com")
                limit_inp = st.number_input("Limit Dzienny", 50)
            with t2:
                smtp_port = st.number_input("SMTP Port", 465)
                imap_port = st.number_input("IMAP Port", 993)
                uploaded_file = st.file_uploader("Załącznik (PDF/DOCX)", type=['pdf', 'docx'])
            with t3:
                smtp_user = st.text_input("Email User")
                smtp_pass = st.text_input("Hasło Aplikacji", type="password")

            if st.form_submit_button("🚀 Uruchom Agenta", type="primary"):
                if not name:
                    st.error("Nazwa firmy jest wymagana.")
                else:
                    fname = save_uploaded_file(uploaded_file) if uploaded_file else None
                    new_c = Client(
                        name=name, industry=industry, sender_name=sender, 
                        value_proposition=uvp, ideal_customer_profile=icp, tone_of_voice=tone,
                        negative_constraints=neg, case_studies=cases,
                        smtp_server=smtp_host, smtp_port=smtp_port, smtp_user=smtp_user, smtp_password=smtp_pass,
                        imap_server=imap_host, imap_port=imap_port, daily_limit=limit_inp,
                        attachment_filename=fname, status="ACTIVE"
                    )
                    session.add(new_c)
                    session.commit()
                    st.success("Firma dodana pomyślnie!")
                    time.sleep(1)
                    st.rerun()

    # ==============================================================================
    # VIEW: DASHBOARD KLIENTA
    # ==============================================================================
    elif client:
        st.title(f"{client.name}")
        st.markdown(f"**Branża:** {client.industry} | **Nadawca:** {client.sender_name}")
        
        # METRYKI
        st.markdown("### 📊 Przegląd Operacyjny")
        c_new = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "NEW").count()
        c_ready = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "ANALYZED").count()
        c_draft = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "DRAFTED").count()
        c_hot = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "HOT_LEAD").count()

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("1. Kolejka Badawcza", c_new)
        k2.metric("2. Kolejka Pisania", c_ready)
        k3.metric("3. Gotowe Drafty", c_draft)
        k4.metric("🔥 HOT LEADS", c_hot, delta_color="inverse")

        # PANEL AKCJI RĘCZNYCH
        st.markdown("### 🛠️ Sterowanie Manualne")
        
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        
        # 1. SCOUT
        with col_m1:
            if st.button("1. Szukaj (Scout)", use_container_width=True):
                try:
                    camp = session.query(Campaign).filter(Campaign.client_id == client.id, Campaign.status == "ACTIVE").order_by(Campaign.id.desc()).first()
                    if not camp:
                        st.error("Brak aktywnej kampanii! Ustaw cel poniżej.")
                    else:
                        with st.spinner(f"Szukam celów dla: {camp.strategy_prompt[:30]}..."):
                            # >>> POPRAWKA NEXUSA: DODANO camp.id <<<
                            strategy = generate_strategy(client, camp.strategy_prompt, camp.id)
                            
                            if strategy and strategy.search_queries:
                                strategy.search_queries = strategy.search_queries[:2]
                                found = run_scout(session, camp.id, strategy)
                                st.success(f"Znaleziono: {found}")
                                time.sleep(1.5)
                                st.rerun()
                            else:
                                st.error("AI nie wygenerowało strategii.")
                except Exception as e:
                    st.error(f"Błąd Scouta: {str(e)}")

        # 2. ANALYZE
        with col_m2:
            if st.button(f"2. Analizuj ({c_new})", use_container_width=True):
                try:
                    with st.status("Analiza stron www..."):
                        leads = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "NEW").limit(5).all()
                        if not leads:
                            st.warning("Brak leadów do analizy.")
                        else:
                            for l in leads: analyze_lead(session, l.id)
                            st.success("Zakończono.")
                            time.sleep(1)
                            st.rerun()
                except Exception as e:
                    st.error(f"Błąd Analizy: {str(e)}")

        # 3. WRITE
        with col_m3:
            if st.button(f"3. Pisz Maile ({c_ready})", use_container_width=True):
                try:
                    with st.status("Copywriting AI..."):
                        leads = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "ANALYZED").limit(5).all()
                        if not leads:
                            st.warning("Brak leadów do pisania.")
                        else:
                            for l in leads: generate_email(session, l.id)
                            st.success("Gotowe.")
                            time.sleep(1)
                            st.rerun()
                except Exception as e:
                    st.error(f"Błąd Pisania: {str(e)}")

        # 4. SEND/SYNC
        with col_m4:
            if st.button(f"4. Wyślij/Zapisz ({c_draft})", use_container_width=True):
                try:
                    with st.status("Synchronizacja IMAP..."):
                        process_followups(session, client)
                        leads = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "DRAFTED").limit(5).all()
                        if not leads:
                             st.warning("Brak draftów.")
                        else:
                            for l in leads: 
                                save_draft_via_imap(l, client)
                                l.status = "SENT"
                                l.sent_at = func.now()
                                l.last_action_at = func.now()
                                session.commit()
                            st.success("Wysłano.")
                            time.sleep(1)
                            st.rerun()
                except Exception as e:
                    st.error(f"Błąd IMAP: {str(e)}")

        # ZAKŁADKI GŁÓWNE
        st.markdown("---")
        tab_conf, tab_camp, tab_data = st.tabs(["⚙️ KONFIGURACJA AGENTA", "🚀 CELE KAMPANII", "📂 BAZA DANYCH"])

        # --- TAB 1: PEŁNA KONFIGURACJA ---
        with tab_conf:
            st.markdown("#### Edycja DNA i Ustawień")
            with st.form("edit_client_full"):
                
                c1, c2, c3 = st.columns(3)
                with c1: e_name = st.text_input("Nazwa Firmy", client.name)
                with c2: e_ind = st.text_input("Branża", client.industry)
                with c3: e_sender = st.text_input("Nadawca", client.sender_name)

                ec_uvp, ec_icp = st.columns(2)
                with ec_uvp: e_uvp = st.text_area("Value Proposition", client.value_proposition, height=120)
                with ec_icp: e_icp = st.text_area("Ideal Customer Profile", client.ideal_customer_profile, height=120)

                ec_tone, ec_neg = st.columns(2)
                with ec_tone: e_tone = st.text_input("Tone of Voice", client.tone_of_voice)
                with ec_neg: e_neg = st.text_area("Negative Constraints", client.negative_constraints, height=70)
                
                e_cases = st.text_area("Case Studies", client.case_studies, height=100)

                et1, et2, et3 = st.columns(3)
                with et1:
                    e_host = st.text_input("SMTP Host", client.smtp_server)
                    e_imap = st.text_input("IMAP Host", client.imap_server)
                with et2:
                    e_port = st.number_input("SMTP Port", value=client.smtp_port or 465)
                    e_iport = st.number_input("IMAP Port", value=client.imap_port or 993)
                with et3:
                    e_user = st.text_input("SMTP User", client.smtp_user)
                    e_pass = st.text_input("Hasło Aplikacji", client.smtp_password, type="password")
                
                e_limit = st.number_input("Limit Dzienny", value=client.daily_limit or 50)
                
                curr_file = client.attachment_filename or "Brak pliku"
                st.info(f"Obecny załącznik: {curr_file}")
                e_file = st.file_uploader("Zmień załącznik", type=['pdf', 'docx'])

                if st.form_submit_button("💾 Zapisz Pełną Konfigurację", type="primary"):
                    client.name = e_name
                    client.industry = e_ind
                    client.sender_name = e_sender
                    client.value_proposition = e_uvp
                    client.ideal_customer_profile = e_icp
                    client.tone_of_voice = e_tone
                    client.negative_constraints = e_neg
                    client.case_studies = e_cases
                    client.smtp_server = e_host
                    client.imap_server = e_imap
                    client.smtp_port = e_port
                    client.imap_port = e_iport
                    client.smtp_user = e_user
                    client.smtp_password = e_pass
                    client.daily_limit = e_limit

                    if e_file:
                        fname = save_uploaded_file(e_file)
                        client.attachment_filename = fname
                    
                    session.commit()
                    st.success("Dane zaktualizowane!")
                    time.sleep(1)
                    st.rerun()

        # --- TAB 2: KAMPANIE ---
        with tab_camp:
            st.markdown("#### Cele Zwiadowcze")
            st.info("Autobot użyje najnowszej aktywnej kampanii, gdy skończą mu się leady.")
            
            with st.form("new_camp"):
                target = st.text_area("Zdefiniuj nowy cel (np. E-commerce w Niemczech używający Magento)")
                if st.form_submit_button("Dodaj Cel"):
                    new_c = Campaign(client_id=client.id, name=f"Auto {int(time.time())}", status="ACTIVE", strategy_prompt=target)
                    session.add(new_c)
                    session.commit()
                    st.success("Cel dodany!")
                    st.rerun()

            st.markdown("Aktywne cele:")
            active = session.query(Campaign).filter(Campaign.client_id == client.id, Campaign.status == "ACTIVE").order_by(Campaign.id.desc()).all()
            for c in active:
                st.code(c.strategy_prompt)

        # --- TAB 3: DANE ---
        with tab_data:
            st.markdown("#### Surowe Dane Leadów")
            try:
                q = session.query(Lead.id, GlobalCompany.name, Lead.status, Lead.target_email, Lead.step_number, Lead.ai_confidence_score).join(GlobalCompany).join(Campaign).filter(Campaign.client_id == client.id)
                df = pd.read_sql(q.statement, session.connection())
                st.dataframe(df, use_container_width=True)            
            except Exception as e:
                st.warning("Brak danych lub błąd ładowania tabeli.")

finally:
    session.close()