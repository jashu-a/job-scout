# 🎯 Job Scout

Automated job hunting pipeline: **Scrape → Deduplicate → AI Match → Tailor Resume & Cover Letter → Google Drive → Telegram Notify**

## How It Works

1. Scrapes jobs from Google Jobs (via SerpAPI) based on your title/location/seniority combos
2. Deduplicates against a local SQLite database (never sends the same job twice)
3. Uses OpenAI to compare each job description against your resume (weighted scoring across hard skills, experience level, domain relevance, education, and soft signals)
4. For matched jobs: generates a **tailored resume** and **cover letter** adapted to the specific JD
5. Uploads both documents to **Google Drive** in a `<Company>_<JobID>` folder
6. Sends matching jobs to your **Telegram** with match score, reasoning, and Drive folder link

## Quick Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get your API keys

| Service | Where to get it | Cost |
|---------|----------------|------|
| **SerpAPI** | [serpapi.com](https://serpapi.com) → Sign up → Dashboard → API Key | Free: 250 searches/month |
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | ~$0.01-0.03 per job match |
| **Telegram Bot** | Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` | Free |
| **Telegram Chat ID** | Message [@userinfobot](https://t.me/userinfobot) → it replies with your ID | Free |
| **Google Drive** | [console.cloud.google.com](https://console.cloud.google.com) → see below | Free |

#### Google Drive Setup (Optional)

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a project
2. Enable **Google Drive API** (APIs & Services → Library → search "Drive")
3. Create a **Service Account** (APIs & Services → Credentials → Create → Service Account)
4. Download the JSON key file for the service account
5. In Google Drive, create a folder for your job applications
6. **Share that folder** with the service account email (it looks like `name@project.iam.gserviceaccount.com`)
7. Copy the folder ID from the URL: `https://drive.google.com/drive/folders/<THIS_IS_THE_ID>`
8. Set `gdrive_enabled: true`, the credentials path, and folder ID in `config.yaml`

### 3. Configure

Edit `config.yaml`:
- Paste in your API keys
- Define your search combos (title + location + seniority)
- Place your `resume.pdf` in the same directory
- Adjust `days_back`, `match_threshold`, etc.

### 4. Run

```bash
# Standard run
python main.py

# Override settings via CLI
python main.py --days-back 3 --threshold 80

# Dry run (scrape & match, but don't send Telegram / upload Drive)
python main.py --dry-run

# Skip Google Drive upload (still generates docs locally)
python main.py --no-drive

# Skip tailored docs entirely (just match & notify)
python main.py --no-docs

# Use a different config file
python main.py --config my_config.yaml
```

## Configuration Reference

```yaml
# config.yaml
serpapi_key: "abc123"           # SerpAPI key
openai_api_key: "sk-..."       # OpenAI key
telegram_bot_token: "123:ABC"  # Bot token from @BotFather
telegram_chat_id: "987654321"  # Your chat ID

resume_path: "resume.pdf"      # Path to your resume

search_combos:
  - title: "Software Engineer"
    location: "New York, NY"
    seniority: "Mid level"      # Optional: Internship, Entry level, Mid level, Senior level, Director, Executive

days_back: 7                    # Jobs posted within last N days (1, 3, 7, 14, 30)
match_threshold: 65             # Minimum AI match score (0-100) to notify
max_results_per_combo: 30       # Max jobs fetched per search combo (paginated)
openai_model: "gpt-4o-mini"    # OpenAI model (gpt-4o-mini is cheapest)

# Google Drive (optional)
gdrive_enabled: false
gdrive_credentials_path: "service_account.json"
gdrive_folder_id: "1ABCxyz..."  # From the Drive folder URL
```

## Scheduling (Optional)

To run automatically, add a cron job:

```bash
# Run every morning at 8am
crontab -e
0 8 * * * cd /path/to/job-scout && python main.py >> scout.log 2>&1
```

Or on Windows with Task Scheduler, or use a simple loop:

```bash
# Run every 6 hours
while true; do python main.py; sleep 21600; done
```

## Project Structure

```
job-scout/
├── config.yaml          # All settings (API keys, search combos, thresholds)
├── resume.pdf           # Your resume (you provide this)
├── main.py              # CLI entry point & pipeline orchestrator
├── scraper.py           # SerpAPI Google Jobs scraper (with pagination + smart stop)
├── matcher.py           # OpenAI matching, tailored resume, and cover letter generation
├── doc_generator.py     # Creates formatted .docx files for resume & cover letter
├── drive_uploader.py    # Google Drive folder creation & file upload
├── notifier.py          # Telegram message sender
├── resume_parser.py     # PDF text extraction
├── db.py                # SQLite dedup store
├── jobs.db              # Auto-created database (after first run)
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

## Cost Estimate

For a typical run with 3 search combos × 10 new jobs each:
- **SerpAPI**: 3-9 searches depending on pagination (free tier: 250/month)
- **OpenAI** (gpt-4o-mini): ~30 scoring calls + ~20 doc generation calls ≈ $0.05-0.10 per run
- **Telegram**: Free
- **Google Drive**: Free

Running daily for a month: ~$2-3 in OpenAI costs, well within SerpAPI free tier.

## Tips

- Start with `--dry-run` to test your setup without spamming Telegram
- Use a low `match_threshold` (e.g., 50) at first to see what comes through, then raise it
- The SQLite database (`jobs.db`) persists between runs — delete it to reset dedup history
- Add more search combos in `config.yaml` to cast a wider net
