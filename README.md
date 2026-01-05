# 🎯 Headhunter Bot - Autonomous AI Sales Agent

> **Enterprise-grade autonomous agent system for B2B sales outreach automation**  
> Sourcing → Research → Personalization → Sending - fully autonomous with LLM intelligence

![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![Status](https://img.shields.io/badge/Status-Production%20Ready-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 🌟 What It Does

### Problem It Solves
- Sales teams waste **50-70%** of time on repetitive tasks
- Manual company research = **30-60 min per lead**
- Email outreach is inconsistent and ineffective
- Follow-ups are manual and error-prone

### Solution
**1 Sales Rep + Headhunter Bot = 5 Sales Reps (in throughput)**

- ✅ **Autonomous Sourcing** - Finds 20-50 qualified leads daily without human input
- ✅ **Deep Research** - AI extracts tech stack, decision makers, pain points automatically
- ✅ **Hyper-Personalized Emails** - Each message tailored to company profile using Gemini 2.0 Flash
- ✅ **Inbox Monitoring** - Replies automatically tracked and classified (Positive/Negative/Neutral)
- ✅ **Drip Campaigns** - Intelligent follow-up scheduling
- ✅ **Warmup Safety** - Gradual ramp-up to avoid spam filters (Day 1: 2 emails → gradual to 50)

---

## 🏗️ Architecture

### System Overview

```
┌──────────────────────────────────────────────────────────────┐
│            NEXUS CORE ENGINE (Async Dispatcher)              │
│  • Max 20 concurrent workers (Semaphore)                     │
│  • Auto-recovery on crash (Watchdog loop)                    │
│  • Background backups (every 6 hours)                        │
└────────────────────┬─────────────────────────────────────────┘
                     │
        ┌────────────┼──────────────┬──────────────┐
        ▼            ▼              ▼              ▼
    ┌────────┐  ┌─────────┐   ┌─────────┐   ┌──────────┐
    │ SCOUT  │  │RESEARCH │   │ WRITER  │   │  SENDER  │
    │ Agent  │  │ Agent   │   │ Agent   │   │  Agent   │
    └────────┘  └─────────┘   └─────────┘   └──────────┘
        │            │             │              │
        └────────────┼─────────────┼──────────────┘
                     ▼             ▼
         ┌────────────────────────────────────┐
         │  INBOX MONITOR (Reply Classifier)  │
         │  + Drip Campaign Scheduler         │
         └────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
    ┌──────────┐           ┌───────────┐
    │PostgreSQL│           │  Backups  │
    │Database  │           │  (JSON)   │
    └──────────┘           └───────────┘
```

### The 5 Agents

| Agent | Role | Input | Output | Tech |
|-------|------|-------|--------|------|
| **SCOUT** | Find leads | Strategy prompt | 20-50 qualified leads/day | Apify + Gemini AI |
| **RESEARCHER** | Company analysis | Lead domain | Tech stack, decision makers, pain points | Firecrawl + Gemini |
| **WRITER** | Email generation | Company profile + Lead data | Personalized subject + body | LangChain + Gemini |
| **SENDER** | Execute delivery | Draft email | SENT or DRAFT status | SMTP/IMAP |
| **INBOX** | Monitor replies | IMAP connection | Sentiment + classification | IMAP + Gemini |

---

## 📊 Database Schema

### Core Tables

**`clients`** - Agency DNA
```python
Client(
    name="Your Agency",
    industry="Software House",
    tone_of_voice="Professional, Direct",
    ideal_customer_profile="Fintech, Series A, EU",
    daily_limit=50,
    warmup_enabled=True
)
```

**`campaigns`** - Lead generation strategies
```python
Campaign(
    client_id=1,
    strategy_prompt="Find UK fintech companies with Series A funding",
    status="ACTIVE"
)
```

**`leads`** - Pipeline state machine
```
NEW → ANALYZED → DRAFTED → SENT → REPLIED
```

**`global_companies`** - Knowledge graph (JSONB)
```python
GlobalCompany(
    domain="techcorp.com",
    tech_stack=["React", "AWS", "Stripe"],
    decision_makers=[{"name": "John", "role": "CTO"}],
    pain_points=["Slow site", "No mobile"],
    quality_score=85
)
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- PostgreSQL 14+
- Redis 7+ (optional)

### Installation

```bash
# Clone
git clone https://github.com/KamilKowalczuk/headhunter_bot
cd headhunter_bot

# Python environment (using uv)
uv sync

# Configure
cp .env.example .env
# Fill: DATABASE_URL, GEMINI_API_KEY, APIFY_API_TOKEN

# Initialize DB
python init_db.py

# Run
python main.py
```

**Expected output:**
```
⚡ NEXUS ENGINE: HARDENED CORE v2.1
System startup. Max Workers: 20
```

### Docker
```bash
docker-compose up -d
```

---

## 📖 Usage

### Add Client (Agency)

```python
from app.database import Client, Session, engine

session = Session(engine)
client = Client(
    name="TechHunt Agency",
    industry="Software Outsourcing",
    value_proposition="Build MVPs in 3 months",
    ideal_customer_profile="SaaS, Series A-B",
    tone_of_voice="Professional, No corporate BS",
    
    # SMTP Config (Gmail)
    sender_name="John Doe",
    smtp_user="john@techunt.com",
    smtp_password="app_password_here",  # NOT Gmail password!
    smtp_server="smtp.googlemail.com",
    imap_server="imap.gmail.com",
    
    daily_limit=50,
    warmup_enabled=True,
    warmup_start_limit=2,
    warmup_increment=2,
    status="ACTIVE"
)

session.add(client)
session.commit()
print(f"✅ Client {client.id} created")
```

### Create Campaign

```python
from app.database import Campaign

campaign = Campaign(
    client_id=1,
    name="UK FinTechs Q1 2026",
    status="ACTIVE",
    strategy_prompt="Find UK fintech companies with <100 employees who raised funding in last 18 months",
    target_region="UK, London"
)

session.add(campaign)
session.commit()
print(f"✅ Campaign {campaign.id} ready")
# System will now:
# 1. Generate 2-3 search queries from strategy_prompt
# 2. Execute daily searches via Apify
# 3. AI filters candidates (removes B2C, spam, competitors)
# 4. Creates leads and researches them
# 5. Generates personalized emails
# 6. Sends or saves as draft (based on sending_mode)
```

---

## 🧠 AI Integration

### Models Used

| Component | Model | Why |
|-----------|-------|-----|
| Sourcing Filter | Gemini 2.0 Flash | Cost-effective, low latency, structured output |
| Research | Gemini 2.0 Flash | Fast web understanding, JSON extraction |
| Email Generation | Gemini 2.0 Flash | Best personalization + tone matching |
| Sentiment Analysis | Gemini 2.0 Flash | Real-time classification |

**Estimated cost:** €0.02-0.05 per lead (research + generation)

### Safety Features

- **Gatekeeper Filtering** - Rejects B2C, competitors, spam automatically
- **Hallucination Killer** - `ai_confidence_score` (0-100) for each lead
- **Manual Review Mode** - Save as DRAFT before sending (default)
- **Negative Constraints** - Client-specific blacklist in prompt

---

## ⚙️ Configuration

### Per-Client Warmup

```python
client = Client(
    warmup_enabled=True,
    warmup_start_limit=2,      # Day 1: 2 emails
    warmup_increment=2,        # +2 each day (exponential ramp-up)
    warmup_started_at=None     # Set automatically on first send
)

# Example timeline:
# Day 1:  2 emails
# Day 2:  4 emails
# Day 3:  6 emails
# ...
# Day 25: 50 emails (stabilizes at daily_limit)
```

### Per-Campaign Strategy

```python
campaign = Campaign(
    strategy_prompt="""
    Find software agencies:
    - Location: EU (UK, France, Germany preferred)
    - Size: 10-100 people
    - Signs: Recently hired, new website, active on LinkedIn
    """,
    target_region="EU"
)
```

---

## 📊 Monitoring

### Real-time Console
```
[bold green]🚀 TechHunt:[/bold green] WYSYŁAM (AUTO) to FinTechCorp...
[blue]🔬 TechHunt:[/blue] Analizuję fintech.com...
[cyan]✍️ TechHunt:[/cyan] Piszę maila...
[bold red]🕵️ TechHunt:[/bold red] Sprawdzam strategię...
```

### Logs
- `engine.log` - Full system logs (rotated, 5MB max)
- Structured JSON logging for production

### Health Check (Optional API)
```bash
curl http://localhost:8000/health
```

---

## 🛠️ Troubleshooting

### Scout not finding leads?
```bash
# Check Apify quota
python debug_firecrawl.py

# Verify strategy
psql $DATABASE_URL -c "SELECT * FROM campaigns WHERE status='ACTIVE';"

# Check cooldown
psql $DATABASE_URL -c "SELECT * FROM search_history ORDER BY searched_at DESC LIMIT 5;"
```

### Emails not sending?
```bash
# Test SMTP
python -c "from app.agents.sender import test_smtp; test_smtp()"

# Check daily limit
psql $DATABASE_URL -c "SELECT COUNT(*) FROM leads WHERE status='SENT' AND DATE(sent_at)=CURRENT_DATE;"
```

---

## 🚨 Known Limitations (Roadmap)

### Current
- ❌ No multi-instance deployment (single server)
- ❌ Redis declared but not utilized
- ❌ No database indexes (impacts performance at scale)
- ❌ Limited retry logic on API failures
- ⚠️ No web UI (planned)

### Improvements Needed
- [ ] Add database indexes (Quick Win: 30 min, Impact: -90% query time)
- [ ] Implement Redis cache (Quick Win: 2h, Impact: -50% DB load)
- [ ] Add retry logic with exponential backoff (2h, Impact: +30% success rate)
- [ ] FastAPI endpoints for programmatic access (4h)
- [ ] Streamlit dashboard (3h)
- [ ] Payment integration (Stripe) for SaaS

---

## 📈 Performance

### Typical Metrics (1 client, 50 daily limit)

| Metric | Value | Notes |
|--------|-------|-------|
| Leads sourced/day | 20-50 | Depends on strategy |
| Research time/lead | 5-15s | Firecrawl + AI |
| Email generation | 2-3s | LLM inference |
| Success rate | 95%+ | With proper config |
| Memory usage | ~250MB | Base + agents |

### Scaling Potential
- **100 clients:** PostgreSQL pool 20 (default) ✅
- **500+ clients:** Add Redis cache + increase pool ✅
- **1000+ clients:** Kubernetes deployment needed 🔄

---

## 💼 Business Model

### For Your Agency
- **Cost:** €200-500/month (hosting) + your dev time
- **Benefit:** Automate 80% of outreach
- **ROI:** 10x (vs manual work)

### As SaaS Product
- **Price:** €299-999/month per client
- **MRR Potential:** €10k-50k (at scale)
- **Market:** 50,000+ agencies globally

### Consulting Services
- **Price:** €5k-15k per implementation
- **Effort:** 1-2 weeks per client
- **Annual Potential:** €50k+

---

## 📦 Dependencies

### AI & LLM
```toml
langchain = "1.2.0"
langchain-google-genai = "4.1.2"
langgraph = "1.0.5"
pydantic = "2.12.5"
```

### Data & Infrastructure
```toml
sqlalchemy = "2.0.45"
psycopg2-binary = "2.9.11"
redis = "7.1.0"
```

### Web Scraping
```toml
apify-client = "2.3.0"
firecrawl-py = "4.12.0"
```

### UI & Monitoring
```toml
streamlit = "1.52.2"
rich = "14.2.0"
matplotlib = "3.10.8"
```

---

## 🤝 Contributing

Issues or ideas?

1. Fork repo
2. Create feature branch (`git checkout -b feature/amazing`)
3. Commit (`git commit -m "Add amazing feature"`)
4. Push (`git push origin feature/amazing`)
5. Open PR

**Areas seeking contributions:**
- [ ] Redis integration
- [ ] FastAPI endpoints
- [ ] React dashboard
- [ ] Kubernetes deployment
- [ ] Tests (unit + integration)

---

## 📜 License

MIT License - See LICENSE file

---

## 💡 Key Insights

### Why This Architecture Works

1. **Async-First** - Non-blocking I/O = max throughput
2. **Modular Agents** - Each agent owns one responsibility
3. **AI-Native** - LLM at every decision point (not just output)
4. **Enterprise-Ready** - Error handling, logging, recovery built-in
5. **Cost-Optimized** - Gemini 2.0 Flash for best cost/perf

### Philosophy

> "One autonomous agent > five manual salespeople"

The system doesn't try to be human. It's optimized for what it does best: consistent, tireless outreach with AI-powered personalization.

---

## 🎯 Next Steps

### Quick Wins (This Week)
1. Add database indexes → -90% query time
2. Implement retry logic → +30% success rate
3. Setup health checks → Better visibility

### Growth (This Month)
1. Multi-tenancy support
2. FastAPI + Streamlit UI
3. Stripe integration (SaaS)

### Scale (Next Quarter)
1. 100 active clients
2. €10k/month MRR
3. Production SLA (99.9% uptime)

---

## 📞 Support

**Questions?**
- Open issue on GitHub
- Check troubleshooting section above
- Review logs in `engine.log`

**Found a bug?**
- Report with `engine.log` excerpt
- Include client ID + campaign ID
- Describe steps to reproduce

---

## ⭐ If you found this useful, please star the repo!

Built with ❤️ for sales teams tired of manual outreach.

---

**Status:** Production-Ready (v2.1)  
**Last Updated:** January 2026  
**Maintainer:** [@KamilKowalczuk](https://github.com/KamilKowalczuk)
