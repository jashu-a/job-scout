# 🎯 Job Scout

Automated job hunting pipeline that runs daily via GitHub Actions:

**Scrape 6 sources → Deduplicate → AI Match → ATS-Optimized Resume & Cover Letter → Google Drive → Telegram Notify**

Works for any role and region. Built-in auto-expansion when results are low, automatic resume change detection with rescoring, and error notifications via Telegram.

---

## How It Works

```
┌──────────────────────────────────────────────────────────┐
│                    GitHub Actions (daily cron)            │
│                                                          │
│  1. Download jobs.db from Google Drive                   │
│  2. Detect resume changes → auto-rescore old rejects     │
│  3. Scrape 6 job sources across your search combos       │
│  4. Deduplicate (hash + URL matching via SQLite)         │
│  5. AI-score each new job against your resume (0-100)    │
│  6. For matches above threshold:                         │
│     → Generate ATS-optimized resume (.docx)              │
│     → Generate tailored cover letter (.docx)             │
│     → Upload both to Google Drive subfolder              │
│     → Send Telegram notification with score & link       │
│  7. If < 10 matches: auto-expand with related titles     │
│  8. Upload updated jobs.db back to Google Drive          │
│  9. On any error: send alert to Telegram                 │
└──────────────────────────────────────────────────────────┘
```

## Key Features

- **6 job sources** — LinkedIn, TokyoDev, JapanDev, GaijinPot, Indeed, Google Jobs
- **Zero SerpAPI credits for Japan** — all Japan sources use direct scraping
- **ATS-optimized resumes** — keywords extracted from JD, woven into resume to beat applicant tracking systems
- **Auto-expansion** — if < 10 matches, automatically tries related job titles (capped at 10 expansion sends)
- **Resume change detection** — update your resume and previously rejected jobs get automatically rescored
- **Smart dedup** — hash + URL based, persisted in Google Drive across runs
- **Error alerts** — pipeline errors sent to Telegram with suggested fixes
- **Multi-user support** — share codebase across users with separate configs via `user_config.yaml`

## Job Sources

| Source | Method | SerpAPI Credits | Notes |
|--------|--------|-----------------|-------|
| **LinkedIn** | Direct scrape (Japan) / SerpAPI (other) | 0 for Japan | Fetches full job descriptions |
| **TokyoDev** | Direct scrape | 0 | Curated English tech jobs in Japan |
| **JapanDev** | Direct scrape | 0 | 283+ curated jobs at top Japan companies |
| **GaijinPot** | Direct scrape | 0 | English-friendly IT jobs in Japan |
| **Indeed** | SerpAPI (non-Japan only) | 1 per call | Skipped for Japan (403 block) |
| **Google Jobs** | SerpAPI | 1 per page | Auto-disabled for regions returning 0 |

## Smart Location Handling

The scraper normalizes whatever you type:

| You type | Normalized to |
|----------|--------------|
| `Tokyo` | `Tokyo, Japan` |
| `Japan` / `jp` | `Japan` |
| `NYC` | `New York, NY` |
| `sf` | `San Francisco, CA` |
| `london` | `London, United Kingdom` |
| `US` / `usa` | `United States` |
| `Remote` | `United States` |

---

## Setup (Full — for new installations)

### Prerequisites

- GitHub account (for Actions automation)
- API keys (see below)

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/job-scout.git
cd job-scout
```

### 2. Get your API keys

| Service | Where to get it | Cost |
|---------|----------------|------|
| **SerpAPI** | [serpapi.com](https://serpapi.com) | Free: 250 searches/month |
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | ~$0.01-0.03 per job |
| **Telegram Bot** | Message [@BotFather](https://t.me/BotFather) → `/newbot` | Free |
| **Telegram Chat ID** | Message [@userinfobot](https://t.me/userinfobot) | Free |
| **Google Drive** | [console.cloud.google.com](https://console.cloud.google.com) → see below | Free |

### 3. Google Drive Setup (OAuth2)

1. Go to [Google Cloud Console](https://console.cloud.google.com) → Create a project
2. Enable the **Google Drive API** (APIs & Services → Library → search "Drive")
3. Create an **OAuth 2.0 Client ID** (APIs & Services → Credentials → Create → OAuth Client ID → Desktop App)
4. Download the client credentials JSON
5. **Publish your app**: APIs & Services → OAuth consent screen → Publish App (prevents token expiry)
6. In Google Drive, create a folder for job applications. Copy the folder ID from the URL:
   ```
   https://drive.google.com/drive/folders/<THIS_IS_THE_FOLDER_ID>
   ```
7. Generate a refresh token locally:
   ```python
   from google_auth_oauthlib.flow import InstalledAppFlow
   flow = InstalledAppFlow.from_client_secrets_file(
       "client_secret.json",
       scopes=["https://www.googleapis.com/auth/drive.file"]
   )
   creds = flow.run_local_server(port=0)
   print("Refresh token:", creds.refresh_token)
   ```

### 4. Add GitHub Secrets

Go to your repo → Settings → Secrets and variables → Actions → New repository secret.

| Secret | Value |
|--------|-------|
| `SERPAPI_KEY` | Your SerpAPI key |
| `OPENAI_API_KEY` | Your OpenAI key |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID (or group chat ID) |
| `RESUME_BASE64` | Your resume, base64-encoded (see below) |
| `GDRIVE_CLIENT_ID` | OAuth client ID |
| `GDRIVE_CLIENT_SECRET` | OAuth client secret |
| `GDRIVE_REFRESH_TOKEN` | OAuth refresh token |
| `GDRIVE_FOLDER_ID` | Google Drive folder ID |

Encode your resume:
```bash
# macOS
base64 -i resume.docx | pbcopy

# Linux
base64 -w0 resume.docx | xclip -selection clipboard
```

### 5. Edit `user_config.yaml`

```yaml
search_combos:
  - title: "Your Job Title"
    location: "Your Location"
    seniority: "Mid level"

sources:
  - linkedin
  - indeed
  - google_jobs

days_back: 5
match_threshold: 65
db_name: "jobs.db"
```

### 6. Push and run

```bash
git add .
git commit -m "Configure Job Scout"
git push
```

Trigger manually: repo → Actions → Job Scout → Run workflow.

---

## Auto-Expansion

When a run finds fewer than 10 matches, Job Scout automatically widens the search:

**Phase 1** — Tries related job titles across all sources:

| Your title | Also searches |
|-----------|---------------|
| Software Engineer | Full Stack Developer, Application Developer, Web Developer |
| Backend Developer | Backend Engineer, API Developer, Server-Side Engineer |
| Platform Engineer | DevOps Engineer, SRE, Cloud Engineer, Infrastructure Engineer |
| Cybersecurity Analyst | Security Analyst, SOC Analyst, Information Security Analyst, Threat Analyst |
| Project Manager | Program Manager, Technical Project Manager, Scrum Master, Delivery Manager |
| Data Scientist | ML Engineer, Machine Learning Engineer, AI Engineer, Research Scientist |
| *...and 40+ more mappings* | |

**Phase 2** — Retries original titles with `days_back=14` using SerpAPI sources only (won't re-scrape static sites).

Expansion stops as soon as **10 new jobs** are sent via Telegram.

---

## Resume Change Detection

Job Scout hashes your resume and stores the hash in the database. When it detects a change:

1. Pulls all previously rejected jobs (scored below threshold)
2. Re-scores them against your updated resume
3. Sends newly qualifying jobs to Telegram tagged with 🔄 RESCORED
4. Capped at 10 rescore notifications per run

This happens automatically — just update `RESUME_BASE64` in GitHub Secrets and the next run detects the change.

---

## ATS Optimization

Tailored resumes are optimized to beat Applicant Tracking Systems:

- **Exact keyword matching** — JD terms woven in verbatim (ATS does literal string matching)
- **Keyword density** — important terms appear 2-3 times across sections
- **Full form + acronym** — e.g. "Amazon Web Services (AWS)" to catch both patterns
- **Action verbs** — strong verbs matching JD language
- **No fabrication** — only rephrases and reorders existing experience using JD terminology

---

## Multi-User Support

Multiple users can share the same codebase with separate configs:

- `user_config.yaml` — personal search combos, sources, thresholds, `db_name`
- `.github/workflows/scout.yml` — personal schedule
- GitHub Secrets — personal API keys and resume
- Both files protected from upstream merges via `.gitattributes`

To sync code updates without overwriting personal config:
```bash
git pull upstream main --no-rebase
git push
```

---

## Project Structure

```
job-scout/
├── .github/
│   └── workflows/
│       └── scout.yml          # GitHub Actions workflow (schedule + manual)
├── main.py                    # Pipeline orchestrator, expansion, rescore
├── scraper.py                 # Multi-source scraper (6 sources)
├── matcher.py                 # AI scoring + ATS-optimized resume/cover letter
├── doc_generator.py           # DOCX creation for resume & cover letter
├── drive_uploader.py          # Google Drive OAuth2 upload + DB persistence
├── notifier.py                # Telegram notifications + error alerts
├── resume_parser.py           # Resume text extraction (PDF/DOCX)
├── db.py                      # SQLite dedup + metadata (resume hash)
├── user_config.yaml           # Personal search config (per-user)
├── user_config.example.yaml   # Template for new users
├── config.example.yaml        # Full config template
├── .gitattributes             # Merge protection for personal files
├── requirements.txt           # Python dependencies
└── README.md
```

## Cost Estimate

| Service | Usage per daily run | Monthly cost |
|---------|-------------------|-------------|
| **SerpAPI** | 0 credits (Japan) / 3-9 (US) | Free (250/month) |
| **OpenAI** (gpt-4o-mini) | ~15-30 calls | ~$0.03-0.10 |
| **Telegram** | Unlimited | Free |
| **Google Drive** | ~1 MB/run | Free (15 GB) |
| **GitHub Actions** | ~20-30 min | Free (2,000 min/month private) |

**Monthly total: ~$1-3 in OpenAI costs.** Everything else is free.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| **Duplicate notifications** | Check logs for `📦 Downloaded jobs.db from Drive`. If "starting fresh" every time, verify Drive OAuth credentials. |
| **Token expired** | Regenerate refresh token, update `GDRIVE_REFRESH_TOKEN` secret. Make sure app is "In production" in Google Cloud Console. |
| **Indeed 403** | Expected for Japan. For US, it uses SerpAPI fallback. |
| **JapanDev 0 results** | Their HTML may have changed. Check `h2 a[href*='/jobs/']` selector. |
| **GaijinPot unreachable** | GitHub IPs blocked. Works locally. Fails gracefully. |
| **Identical resumes** | Check description lengths in logs. Short descriptions produce generic results. |
| **Fabricated experience** | Prompt guardrails prevent this. If it still happens, report the job title. |