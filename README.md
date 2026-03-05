# 🎯 Job Scout

Automated job hunting pipeline that runs daily via GitHub Actions:

**Scrape 6 sources → Deduplicate → AI Match → Tailor Resume & Cover Letter → Google Drive → Telegram Notify**

Built for software engineers targeting Japan, but works for any region.

---

## How It Works

```
┌──────────────────────────────────────────────────────────┐
│                    GitHub Actions (daily cron)           │
│                                                          │
│  1. Download jobs.db from Google Drive                   │
│  2. Scrape 6 job sources across your search combos       │
│  3. Deduplicate (hash + URL matching via SQLite)         │
│  4. AI-score each new job against your resume (0-100)    │
│  5. For matches above threshold:                         │
│     → Generate tailored resume (.docx)                   │
│     → Generate cover letter (.docx)                      │
│     → Upload both to Google Drive subfolder              │
│     → Send Telegram notification with score & link       │
│  6. Upload updated jobs.db back to Google Drive          │
└──────────────────────────────────────────────────────────┘
```

## Job Sources

| Source | Method | SerpAPI Credits | Notes |
|--------|--------|-----------------|-------|
| **LinkedIn** | Direct scrape (Japan) / SerpAPI (other) | 0 for Japan | Fetches full job descriptions |
| **TokyoDev** | Direct scrape | 0 | Curated English tech jobs in Japan |
| **JapanDev** | Direct scrape | 0 | 283+ curated jobs, top companies (Mercari, PayPay, Treasure Data, etc.) |
| **GaijinPot** | Direct scrape | 0 | English-friendly IT jobs in Japan (may fail from some IPs) |
| **Indeed** | Direct scrape (Japan) / SerpAPI (other) | 0 for Japan | Skipped for Japan (403 block) |
| **Google Jobs** | SerpAPI | 1 per page | Auto-disabled for regions that return 0 results |

For Japan-focused searches, **all scraping is free** — zero SerpAPI credits used.

## Smart Location Handling

You don't need precise location formatting. The scraper normalizes whatever you type:

| You type | Normalized to |
|----------|--------------|
| `Tokyo` | `Tokyo, Japan` |
| `Japan` | `Japan` |
| `jp` | `Japan` |
| `NYC` | `New York, NY` |
| `sf` | `San Francisco, CA` |
| `london` | `London, United Kingdom` |
| `Remote` | `United States` |

---

## Setup

### Prerequisites

- Python 3.11+
- GitHub account (for Actions automation)
- API keys (see below)

### 1. Fork/clone and install

```bash
git clone https://github.com/YOUR_USERNAME/job-scout.git
cd job-scout
pip install -r requirements.txt
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

Google Drive stores your tailored documents AND the dedup database (`jobs.db`) that persists between runs.

1. Go to [Google Cloud Console](https://console.cloud.google.com) → Create a project
2. Enable the **Google Drive API** (APIs & Services → Library → search "Drive")
3. Create an **OAuth 2.0 Client ID** (APIs & Services → Credentials → Create → OAuth Client ID → Desktop App)
4. Download the client credentials JSON
5. In Google Drive, create a folder for job applications. Copy the folder ID from the URL:
   ```
   https://drive.google.com/drive/folders/<THIS_IS_THE_FOLDER_ID>
   ```
6. Generate a refresh token locally:

   ```python
   from google_auth_oauthlib.flow import InstalledAppFlow

   flow = InstalledAppFlow.from_client_secrets_file(
       "client_secret.json",
       scopes=["https://www.googleapis.com/auth/drive.file"]
   )
   creds = flow.run_local_server(port=0)
   print("Refresh token:", creds.refresh_token)
   ```

   > **Corporate proxy / SSL issues?** If you get SSL errors during token generation, temporarily disable verification:
   > ```python
   > import urllib3, requests
   > urllib3.disable_warnings()
   > old_send = requests.Session.send
   > def patched_send(self, *args, **kwargs):
   >     kwargs['verify'] = False
   >     return old_send(self, *args, **kwargs)
   > requests.Session.send = patched_send
   > ```

7. You now have three values needed for GitHub Secrets:
   - `GDRIVE_CLIENT_ID` — from the downloaded JSON (`client_id` field)
   - `GDRIVE_CLIENT_SECRET` — from the downloaded JSON (`client_secret` field)
   - `GDRIVE_REFRESH_TOKEN` — from step 6

### 4. Configure GitHub Secrets

Go to your repo → Settings → Secrets and variables → Actions → New repository secret.

Add these secrets:

| Secret | Value |
|--------|-------|
| `SERPAPI_KEY` | Your SerpAPI key |
| `OPENAI_API_KEY` | Your OpenAI key |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID |
| `RESUME_BASE64` | `base64 -w0 resume.docx` (your resume, base64-encoded) |
| `GDRIVE_CLIENT_ID` | OAuth client ID |
| `GDRIVE_CLIENT_SECRET` | OAuth client secret |
| `GDRIVE_REFRESH_TOKEN` | OAuth refresh token |
| `GDRIVE_FOLDER_ID` | Google Drive folder ID |

To encode your resume:

```bash
# macOS
base64 -i resume.docx | pbcopy

# Linux
base64 -w0 resume.docx | xclip -selection clipboard
```

### 5. Customize search combos

Edit the workflow file (`.github/workflows/scout.yml`) to set your search combos:

```yaml
'search_combos': [
    {'title': 'Software Engineer', 'location': 'Tokyo, Japan', 'seniority': 'Mid level'},
    {'title': 'Backend Developer', 'location': 'Tokyo, Japan', 'seniority': 'Mid level'},
    {'title': 'Platform Engineer', 'location': 'Tokyo, Japan', 'seniority': 'Mid level'},
],
'sources': ['linkedin', 'tokyodev', 'japandev', 'gaijinpot', 'indeed', 'google_jobs'],
```

Available seniority values: `Internship`, `Entry level`, `Mid level`, `Senior level`, `Director`, `Executive` (or leave empty).

### 6. Deploy

Push to GitHub and the workflow runs automatically on schedule. You can also trigger manually from the Actions tab.

```bash
git add .
git commit -m "Configure Job Scout"
git push
```

---

## Running Locally

For local testing without GitHub Actions:

```bash
# Copy and edit config
cp config.example.yaml config.yaml
# Fill in your API keys, search combos, etc.

# Set OAuth env vars
export GDRIVE_CLIENT_ID="..."
export GDRIVE_CLIENT_SECRET="..."
export GDRIVE_REFRESH_TOKEN="..."

# Standard run
python main.py

# Dry run (scrape & match, skip Telegram/Drive)
python main.py --dry-run

# Override settings
python main.py --days-back 3 --threshold 80

# Skip Drive upload (still generates docs locally)
python main.py --no-drive

# Skip doc generation entirely (just match & notify)
python main.py --no-docs
```

---

## GitHub Actions Workflow

The workflow (`.github/workflows/scout.yml`) is pre-configured to:

- Run daily at **9:00 AM JST** (midnight UTC)
- Support manual triggers with configurable `days_back`, `threshold`, and `dry_run` options
- Timeout after 60 minutes

### Manual Trigger

Go to Actions → Job Scout → Run workflow. You can override:

- **days_back**: How far back to search (1, 3, 7, 14, 30 days). Default: 3
- **threshold**: Minimum AI match score. Default: 65
- **dry_run**: Scrape and score without sending notifications or uploading

---

## Deduplication

Job Scout uses a dual deduplication strategy stored in SQLite (`jobs.db`):

1. **Content hash** — `SHA256(title + company + location)` catches exact matches
2. **URL hash** — Normalized job URL catches the same job with slightly different metadata

URL normalization handles cross-source duplicates:
- `jp.linkedin.com/jobs/view/devops-engineer-at-company-4373401636` → `linkedin.com/jobs/view/4373401636`
- `jp.indeed.com/viewjob?jk=abc123&from=web` → `indeed.com/viewjob?jk=abc123`
- TokyoDev, JapanDev, GaijinPot URLs → stripped of query params

The database is **persisted via Google Drive** — downloaded at the start of each run and uploaded after completion. This eliminates the unreliability of GitHub Actions cache.

---

## AI Matching

Each job is scored 0-100 by OpenAI (default: `gpt-4o-mini`) across five dimensions:

- **Hard skills match** (30%) — programming languages, frameworks, tools
- **Experience level** (25%) — seniority alignment
- **Domain relevance** (20%) — industry/product type fit
- **Education** (10%) — degree/certification alignment
- **Soft signals** (15%) — remote policy, company culture, growth

Jobs scoring above the threshold get:

1. **Tailored resume** — Key skills and summary rewritten to match the job description, formatted as a `.docx` using your original resume as a template
2. **Cover letter** — 3-4 paragraphs addressing the specific company and role, formatted as a `.docx`

Both documents are uploaded to a Google Drive subfolder named `CompanyName_JobID/`.

---

## Telegram Notifications

Each matched job sends a message with:

```
🎯 Job Title @ Company (Score: 85/100)
📍 Tokyo, Japan | 🔗 Apply Link
💡 Why it matched: Strong Python/AWS overlap, mid-level seniority fit
⚠️ Gaps: No Kubernetes experience mentioned
📁 Drive: [link to resume & cover letter folder]
```

A summary message is sent at the end of each run with totals.

---

## Project Structure

```
job-scout/
├── .github/
│   └── workflows/
│       └── scout.yml          # GitHub Actions workflow (daily cron + manual)
├── main.py                    # Pipeline orchestrator & CLI
├── scraper.py                 # Multi-source scraper (6 sources)
├── matcher.py                 # AI scoring + tailored resume/cover letter generation
├── doc_generator.py           # DOCX creation for resume & cover letter
├── drive_uploader.py          # Google Drive OAuth2 upload + jobs.db persistence
├── notifier.py                # Telegram message sender
├── resume_parser.py           # Resume text extraction (PDF/DOCX)
├── db.py                      # SQLite dedup (hash + URL based)
├── config.example.yaml        # Config template (copy to config.yaml)
├── requirements.txt           # Python dependencies
├── .gitignore
└── README.md
```

## Cost Estimate

For a typical daily run with 3 Japan-focused search combos:

| Service | Usage | Cost |
|---------|-------|------|
| **SerpAPI** | 0 credits (Japan = all direct scraping) | Free |
| **OpenAI** (gpt-4o-mini) | ~15-25 scoring + ~10-15 doc generation calls | ~$0.03-0.08 |
| **Telegram** | Unlimited | Free |
| **Google Drive** | 15 GB free storage | Free |
| **GitHub Actions** | ~20-30 min/run × 30 days = ~600-900 min | Free (2,000 min/month for private repos, unlimited for public) |

Running daily for a month: **~$1-2.50 total** in OpenAI costs. Everything else is free.

---

## Troubleshooting

**JapanDev returns 0 results** — Their HTML structure may have changed. Check if `h2 a[href*='/jobs/']` still matches job cards on their site.

**GaijinPot unreachable** — Their server blocks GitHub Actions IPs. Expected behavior; the scraper fails gracefully. Works fine when running locally.

**Indeed returns 403** — Indeed actively blocks scraping from cloud IPs. For Japan searches, Indeed is auto-skipped. For other regions, it falls back to SerpAPI Google Search.

**Google Jobs returns 0** — The Google Jobs engine can be inconsistent for Japan. Once a region returns 0, it's auto-disabled for the rest of the run to save credits.

**Duplicate notifications** — Make sure `jobs.db` persistence is working. Check logs for `📦 Downloaded jobs.db from Drive` at the start of the run. If it says "starting fresh" every time, verify your Drive OAuth credentials.

**Resume/cover letters look identical** — Check that job descriptions are being fetched (look for `📄 Description: XXXX chars` in logs). Descriptions under 100 chars produce generic results.

**SSL errors during OAuth setup** — Common on corporate networks. Use the SSL bypass snippet in the Google Drive Setup section above.