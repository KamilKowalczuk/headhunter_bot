import streamlit as st
import pandas as pd
import time
import sys
import os
import signal
import subprocess
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, exc

# --- IMPORTY BACKENDU ---
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from app.database import engine, Client, Campaign, Lead, GlobalCompany
    from app.agents.strategy import generate_strategy
    from app.agents.scout import run_scout_async 
    from app.agents.researcher import analyze_lead
    from app.agents.writer import generate_email
    from app.scheduler import process_followups, save_draft_via_imap
    from app.agents.inbox import check_inbox
    from app.agents.reporter import create_pdf_report
    # Import logiki warm-up
    from app.warmup import calculate_daily_limit
except ImportError as e:
    st.error(f"❌ BŁĄD IMPORTÓW: Nie można załadować modułów backendu.\nDetale: {e}")
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
    
    /* Boxy statusu silnika */
    .engine-status-box {
        padding: 10px;
        border-radius: 8px;
        text-align: center;
        font-weight: bold;
        margin-bottom: 10px;
        border: 1px solid;
    }
    .status-online { background-color: #dcfce7; color: #166534; border-color: #22c55e; }
    .status-offline { background-color: #fee2e2; color: #991b1b; border-color: #ef4444; }

    /* Animacja kropki LIVE */
    @keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
    .live-dot { color: #ef4444; animation: blink 1.5s infinite; font-weight: bold; }
    
    [data-testid="metric-container"] {
        background-color: #FFFFFF;
        border: 1px solid #E5E7EB;
        padding: 16px !important;
        border-radius: 12px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    }
    .stButton button { border-radius: 8px; font-weight: 600; padding: 0.5rem 1rem; }

    /* STYL KONSOLI LOGÓW */
    .console-logs {
        background-color: #1e1e1e;
        color: #d4d4d4;
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 13px;
        line-height: 1.4;
        padding: 15px;
        border-radius: 8px;
        height: 400px; /* Stała wysokość */
        overflow-y: scroll; /* Suwak */
        white-space: pre-wrap; /* Zawijanie wierszy */
        border: 1px solid #333;
    }
</style>
""", unsafe_allow_html=True)

# --- ŚCIEŻKI I PLIKI STERUJĄCE ---
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
FILES_DIR = os.path.join(ROOT_DIR, 'files')
PID_FILE = os.path.join(ROOT_DIR, 'engine.pid')
LOG_FILE = os.path.join(ROOT_DIR, 'engine.log')

os.makedirs(FILES_DIR, exist_ok=True)

# --- ENGINE MANAGER ---
def is_engine_running():
    """Sprawdza czy proces main.py żyje."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False
    return False

def start_engine():
    """Uruchamia main.py z flagą -u (unbuffered logs)."""
    if is_engine_running(): return
    
    if os.path.exists(LOG_FILE): open(LOG_FILE, 'w').close()

    with open(LOG_FILE, "a") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            cwd=ROOT_DIR,
            stdout=log,
            stderr=log
        )
    
    with open(PID_FILE, 'w') as f:
        f.write(str(process.pid))

def stop_engine():
    """Zabija proces silnika."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
        except Exception as e:
            print(f"Błąd zatrzymywania: {e}")
        finally:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)

def get_engine_logs(lines=200):
    """Czyta logi, odwraca kolejność (najnowsze na górze)."""
    if not os.path.exists(LOG_FILE): return "Brak logów. Uruchom silnik."
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            recent = all_lines[-lines:]
            recent.reverse() 
            return "".join(recent)
    except: return "Błąd odczytu."

def get_db():
    return Session(engine)

def save_uploaded_file(uploaded_file):
    if uploaded_file is not None:
        file_path = os.path.join(FILES_DIR, uploaded_file.name)
        with open(file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        return uploaded_file.name
    return None

session = get_db()

try:
    # ==============================================================================
    # SIDEBAR: CENTRUM DOWODZENIA
    # ==============================================================================
    with st.sidebar:
        st.image("https://cdn-icons-png.flaticon.com/512/12565/12565256.png", width=60)
        st.title("TITAN OS")
        
        # --- SEKCJA: SYSTEM ENGINE ---
        st.markdown("### 🖥️ SILNIK SYSTEMU")
        engine_status = is_engine_running()
        
        if engine_status:
            st.markdown('<div class="engine-status-box status-online">🟢 ONLINE</div>', unsafe_allow_html=True)
            if st.button("ZATRZYMAJ", use_container_width=True):
                stop_engine()
                time.sleep(1)
                st.rerun()
        else:
            st.markdown('<div class="engine-status-box status-offline">🔴 OFFLINE</div>', unsafe_allow_html=True)
            if st.button("URUCHOM", use_container_width=True):
                start_engine()
                time.sleep(1)
                st.rerun()
        
        st.markdown("---")

        # --- WYBÓR KLIENTA ---
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
                # Wyświetlamy tryb w Sidebarze
                mode_icon = "💼" if client.mode == "JOB_HUNT" else "💰"
                st.markdown(f"### {mode_icon} {client.status}")
                st.caption(f"Tryb: {client.mode}") 
                
                # Wyświetlamy metodę wysyłki w sidebarze
                send_icon = "🚀" if getattr(client, 'sending_mode', 'DRAFT') == "AUTO" else "📝"
                st.caption(f"Wysyłka: {getattr(client, 'sending_mode', 'DRAFT')} {send_icon}")

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

    # ==============================================================================
    # VIEW: ONBOARDING
    # ==============================================================================
    if selected_option == "➕ DODAJ FIRMĘ":
        st.title("📝 Onboarding Nowej Firmy")
        with st.form("new_client_form"):
            c1, c2, c3 = st.columns(3)
            with c1: name = st.text_input("Nazwa Firmy (ID)")
            with c2: industry = st.text_input("Branża")
            with c3: sender = st.text_input("Nadawca")
            
            c_uvp, c_icp = st.columns(2)
            with c_uvp: uvp = st.text_area("Value Proposition")
            with c_icp: icp = st.text_area("Ideal Customer Profile")
            
            mode_sel = st.selectbox("Tryb Agenta", ["SALES", "JOB_HUNT"], index=0)

            t1, t2, t3 = st.columns(3)
            with t1: smtp_host = st.text_input("SMTP Host", "smtp.gmail.com")
            with t2: smtp_port = st.number_input("SMTP Port", 465)
            with t3: smtp_user = st.text_input("Email User")
            
            pass_input = st.text_input("Hasło", type="password")
            
            st.markdown("#### 🎨 Branding")
            html_foot = st.text_area("Stopka HTML")

            if st.form_submit_button("🚀 Utwórz", type="primary"):
                if not name:
                    st.error("Nazwa wymagana")
                else:
                    nc = Client(
                        name=name, industry=industry, sender_name=sender,
                        value_proposition=uvp, ideal_customer_profile=icp,
                        mode=mode_sel,
                        sending_mode="DRAFT", # Domyślnie bezpiecznie
                        smtp_server=smtp_host, smtp_port=smtp_port, smtp_user=smtp_user, 
                        smtp_password=pass_input, html_footer=html_foot, status="ACTIVE"
                    )
                    session.add(nc)
                    session.commit()
                    st.success("Zapisano.")
                    time.sleep(1)
                    st.rerun()

    # ==============================================================================
    # VIEW: DASHBOARD KLIENTA
    # ==============================================================================
    elif client:
        col_head, col_live = st.columns([0.8, 0.2])
        with col_head:
            st.title(f"{client.name}")
            mode_desc = "Sprzedaż B2B" if client.mode == "SALES" else "Poszukiwanie Pracy"
            st.markdown(f"**Branża:** {client.industry} | **Nadawca:** {client.sender_name} | **Cel:** {mode_desc}")
        with col_live:
            st.write("")
            st.write("")
            live_mode = st.toggle("📡 TRYB LIVE", value=False, help="Płynne odświeżanie danych")

        # 1. PLACEHOLDERY DLA DANYCH DYNAMICZNYCH
        metrics_placeholder = st.empty()
        
        st.markdown("---")
        log_label = "📜 PODGLĄD ZDARZEŃ SILNIKA"
        if live_mode: log_label += " <span class='live-dot'>● REC</span>"
        
        with st.expander(log_label, expanded=True):
            if not live_mode:
                if st.button("🔄 Odśwież Logi", key="refresh_logs_main"):
                    st.rerun()
            logs_placeholder = st.empty()

        # FUNKCJA AKTUALIZUJĄCA DANE
        def update_dashboard_data():
            with engine.connect() as conn:
                tmp_session = Session(bind=conn)
                
                # Pobieramy świeże dane klienta (dla warmupa)
                fresh_client = tmp_session.query(Client).filter(Client.id == client.id).first()
                
                c_new = tmp_session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "NEW").count()
                c_ready = tmp_session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "ANALYZED").count()
                c_draft = tmp_session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "DRAFTED").count()
                
                today = datetime.now().date()
                sent_today = tmp_session.query(Lead).join(Campaign).filter(
                    Campaign.client_id == client.id, 
                    Lead.status == "SENT",
                    func.date(Lead.sent_at) == today
                ).count()
                
                # --- WARMUP CALC ---
                eff_limit = 50
                is_warmup = False
                if fresh_client:
                    eff_limit = calculate_daily_limit(fresh_client)
                    target = fresh_client.daily_limit or 50
                    is_warmup = fresh_client.warmup_enabled and eff_limit < target

                limit_display = f"{sent_today}/{eff_limit}"
                if is_warmup: limit_display += " 🔥"
                
                tmp_session.close()

                with metrics_placeholder.container():
                    k1, k2, k3, k4 = st.columns(4)
                    k1.metric("W kolejce (New)", c_new)
                    k2.metric("Do napisania", c_ready)
                    k3.metric("Do wysłania", c_draft)
                    k4.metric("Dziś wysłano", limit_display, delta=eff_limit-sent_today, delta_color="normal")

            logs = get_engine_logs(200)
            logs_placeholder.markdown(f'<div class="console-logs">{logs}</div>', unsafe_allow_html=True)

        # PANEL AKCJI RĘCZNYCH
        st.markdown("### 🛠️ Sterowanie Manualne")
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        
        with col_m1:
            if st.button("1. Szukaj (Scout)", use_container_width=True):
                camp = session.query(Campaign).filter(Campaign.client_id == client.id, Campaign.status == "ACTIVE").first()
                if camp:
                    strategy = generate_strategy(client, camp.strategy_prompt, camp.id)
                    if strategy and strategy.search_queries:
                        asyncio.run(run_scout_async(session, camp.id, strategy))
                        st.success("Scout zakończył.")
        
        with col_m2:
            if st.button(f"2. Analizuj", use_container_width=True):
                with st.status("Analiza..."):
                    leads = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "NEW").limit(5).all()
                    for l in leads: analyze_lead(session, l.id)
                    st.success("Gotowe.")

        with col_m3:
            if st.button(f"3. Pisz Maile", use_container_width=True):
                with st.status("Pisanie..."):
                    leads = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "ANALYZED").limit(5).all()
                    for l in leads: generate_email(session, l.id)
                    st.success("Gotowe.")

        with col_m4:
            if st.button(f"4. Wyślij", use_container_width=True):
                with st.status("Wysyłka..."):
                    process_followups(session, client)
                    leads = session.query(Lead).join(Campaign).filter(Campaign.client_id == client.id, Lead.status == "DRAFTED").limit(5).all()
                    for l in leads: 
                        save_draft_via_imap(l, client)
                        l.status = "SENT"
                        l.sent_at = func.now()
                        session.commit()
                    st.success("Wysłano.")

        st.markdown("---")
        # ZAKŁADKI
        tab_conf, tab_camp, tab_rep, tab_data = st.tabs(["⚙️ KONFIGURACJA", "🚀 KAMPANIE", "📊 RAPORTY", "📂 BAZA DANYCH"])

        # --- TAB 1: PEŁNA KONFIGURACJA ---
        with tab_conf:
            st.markdown("#### Edycja DNA i Ustawień")
            with st.form("edit_client_full"):
                c1, c2, c3 = st.columns(3)
                with c1: e_name = st.text_input("Nazwa Firmy", client.name)
                with c2: e_ind = st.text_input("Branża", client.industry)
                with c3: e_sender = st.text_input("Nadawca", client.sender_name)

                # --- PRZEŁĄCZNIKI (MODE & SENDING) ---
                c_mode1, c_mode2 = st.columns(2)
                
                with c_mode1:
                    # Tryb Strategiczny
                    curr_mode_index = 0 if client.mode == "SALES" else 1
                    e_mode = st.radio("Cel Agenta:", ["SALES", "JOB_HUNT"], index=curr_mode_index, horizontal=True)
                
                with c_mode2:
                    # Tryb Techniczny (Wysyłka)
                    # Używamy getattr na wypadek gdyby w bazie jeszcze nie było wartości (dla starych klientów)
                    curr_send = getattr(client, 'sending_mode', 'DRAFT')
                    send_idx = 0 if curr_send == "DRAFT" else 1
                    e_send = st.radio("Metoda Wysyłki:", ["DRAFT", "AUTO"], index=send_idx, horizontal=True)
                    
                    if e_send == "AUTO":
                        st.warning("⚠️ AUTO wysyła maile natychmiast! Upewnij się, że Warm-up działa.")
                # -------------------------------------

                ec_uvp, ec_icp = st.columns(2)
                with ec_uvp: e_uvp = st.text_area("Value Proposition / Twoje BIO", client.value_proposition, height=120)
                with ec_icp: e_icp = st.text_area("Ideal Customer / Pracodawca", client.ideal_customer_profile, height=120)

                ec_tone, ec_neg = st.columns(2)
                with ec_tone: e_tone = st.text_input("Tone of Voice", client.tone_of_voice)
                with ec_neg: e_neg = st.text_area("Negative Constraints", client.negative_constraints, height=70)
                e_cases = st.text_area("Case Studies / Projekty", client.case_studies, height=100)

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
                
                e_limit = st.number_input("Limit Dzienny (Docelowy)", value=client.daily_limit or 50)
                curr_file = client.attachment_filename or "Brak pliku"
                st.info(f"Obecny załącznik: {curr_file}")
                e_file = st.file_uploader("Zmień załącznik", type=['pdf', 'docx'])

                # --- SEKCJA WARM-UP ---
                st.markdown("---")
                st.markdown("#### 🔥 Strategia Rozgrzewki (Warm-up)")
                
                c_warm1, c_warm2 = st.columns([0.2, 0.8])
                with c_warm1:
                    e_warm_enable = st.checkbox("Włącz Warm-up", value=client.warmup_enabled)
                with c_warm2:
                    if e_warm_enable:
                        st.info(f"Start: {client.warmup_started_at.strftime('%Y-%m-%d') if client.warmup_started_at else 'Dziś'}")

                wc1, wc2 = st.columns(2)
                with wc1:
                    e_warm_start = st.number_input("Start (ile maili 1. dnia)", value=client.warmup_start_limit or 2, disabled=not e_warm_enable)
                with wc2:
                    e_warm_inc = st.number_input("Przyrost (ile więcej co dzień)", value=client.warmup_increment or 2, disabled=not e_warm_enable)

                if e_warm_enable:
                    final_lim = e_limit
                    days_to_max = max(0, int((final_lim - e_warm_start) / e_warm_inc))
                    st.caption(f"📈 Pełną moc ({final_lim}/dzień) osiągniesz za ok. {days_to_max} dni.")
                # ----------------------

                st.markdown("---")
                st.markdown("#### Stopka HTML")
                e_footer = st.text_area("Kod HTML", value=client.html_footer, height=200)

                if st.form_submit_button("💾 Zapisz Zmiany", type="primary"):
                    client.name = e_name
                    client.industry = e_ind
                    client.sender_name = e_sender
                    client.mode = e_mode
                    client.sending_mode = e_send # Zapisujemy tryb wysyłki
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
                    client.html_footer = e_footer
                    
                    if e_file:
                        fname = save_uploaded_file(e_file)
                        client.attachment_filename = fname
                    
                    # LOGIKA ZAPISU WARM-UP
                    if e_warm_enable and not client.warmup_enabled:
                        client.warmup_started_at = datetime.now()
                    client.warmup_enabled = e_warm_enable
                    client.warmup_start_limit = e_warm_start
                    client.warmup_increment = e_warm_inc

                    session.commit()
                    st.success("Zapisano!")
                    st.rerun()

        # --- TAB 2: KAMPANIE ---
        with tab_camp:
            st.markdown("#### Cele Zwiadowcze")
            if client.mode == "JOB_HUNT":
                st.info("💡 W trybie JOB_HUNT wpisz np.: 'Software House Python Kraków', 'AI Startups Remote'.")
            else:
                st.info("💡 W trybie SALES wpisz np.: 'Sklepy meblowe Warszawa', 'Biura księgowe'.")

            with st.form("new_camp"):
                target = st.text_area("Cel")
                if st.form_submit_button("Dodaj Cel"):
                    nc = Campaign(client_id=client.id, name="Auto", status="ACTIVE", strategy_prompt=target)
                    session.add(nc)
                    session.commit()
                    st.success("Dodano")
                    st.rerun()
            active = session.query(Campaign).filter(Campaign.client_id == client.id, Campaign.status == "ACTIVE").order_by(Campaign.id.desc()).all()
            
            # --- SEKCJA KASOWANIA CELÓW (TAB 2) ---
            st.markdown("---")
            if not active:
                st.caption("Brak aktywnych celów.")
                
            for c in active:
                col_text, col_btn = st.columns([0.85, 0.15])
                with col_text:
                    st.code(c.strategy_prompt, language="text")
                with col_btn:
                    if st.button("🗑️ Usuń", key=f"del_camp_{c.id}", use_container_width=True):
                        c.status = "ARCHIVED" # Soft delete
                        session.commit()
                        st.success("Usunięto.")
                        time.sleep(0.5)
                        st.rerun()
            # -------------------------------------

        # --- TAB 3: RAPORTOWANIE ---
        with tab_rep:
            st.markdown("#### 📄 Centrum Raportowania Enterprise")
            c_rep1, c_rep2, c_rep3 = st.columns(3)
            with c_rep1: d_start = st.date_input("Od dnia", value=datetime.now() - timedelta(days=30))
            with c_rep2: d_end = st.date_input("Do dnia", value=datetime.now())
            with c_rep3:
                st.write("") 
                st.write("") 
                gen_btn = st.button("🖨️ Wygeneruj PDF", type="primary", use_container_width=True)

            if gen_btn:
                with st.spinner("Generowanie..."):
                    try:
                        pdf_path = create_pdf_report(session, client.id)
                        if pdf_path and os.path.exists(pdf_path):
                            st.success(f"Gotowe!")
                            with open(pdf_path, "rb") as pdf_file:
                                st.download_button("📥 POBIERZ", pdf_file, file_name=os.path.basename(pdf_path), mime="application/pdf", use_container_width=True)
                    except Exception as e: st.error(f"Błąd: {e}")

        # --- TAB 4: DANE ---
        with tab_data:
            st.markdown("#### Surowe Dane Leadów")
            try:
                q = session.query(Lead.id, GlobalCompany.name, Lead.status, Lead.target_email).join(GlobalCompany).join(Campaign).filter(Campaign.client_id == client.id)
                df = pd.read_sql(q.statement, session.connection())
                st.dataframe(df, use_container_width=True)            
            except Exception as e: st.warning("Brak danych.")

        # =================================================================
        # 3. PĘTLA ODŚWIEŻANIA
        # =================================================================
        if live_mode:
            while True:
                update_dashboard_data()
                time.sleep(1)
        else:
            update_dashboard_data()

finally:
    session.close()