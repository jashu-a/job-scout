#!/usr/bin/env python3
"""
Job Scout - Main Pipeline
=========================
Scrapes jobs → deduplicates → AI matches against resume → generates tailored
resume + cover letter → uploads to Google Drive → sends to Telegram.

Usage:
    python main.py                     # Run with config.yaml defaults
    python main.py --days-back 3       # Override days_back
    python main.py --threshold 80      # Override match threshold
    python main.py --dry-run           # Scrape & match but don't send Telegram / upload Drive
    python main.py --no-drive          # Skip Google Drive upload
    python main.py --no-docs           # Skip tailored resume/cover letter generation
    python main.py --config my.yaml    # Use a custom config file
"""

import argparse
import sys
import time
import tempfile
import re
from pathlib import Path

import yaml

from db import get_connection, make_hash, is_seen, mark_seen, get_stats
from scraper import scrape_jobs
from matcher import match_resume_to_job, generate_tailored_resume, generate_cover_letter
from notifier import send_job_message, send_summary_message
from resume_parser import extract_resume_text


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        sys.exit(1)
    with open(path, "r") as f:
        return yaml.safe_load(f)


def validate_config(cfg: dict, skip_drive: bool = False):
    """Check that required fields are present and not placeholder values."""
    required = {
        "serpapi_key": "SerpAPI key",
        "openai_api_key": "OpenAI API key",
        "telegram_bot_token": "Telegram bot token",
        "telegram_chat_id": "Telegram chat ID",
    }
    errors = []
    for key, label in required.items():
        val = cfg.get(key, "")
        if not val or str(val).startswith("YOUR_"):
            errors.append(f"  - {label} ({key}) is not set in config.yaml")

    if not cfg.get("search_combos"):
        errors.append("  - No search_combos defined in config.yaml")

    resume_path = cfg.get("resume_path", "resume.pdf")
    if not Path(resume_path).exists():
        errors.append(f"  - Resume file not found: {resume_path}")

    # Google Drive validation (optional)
    if not skip_drive and cfg.get("gdrive_enabled", False):
        gdrive_creds = cfg.get("gdrive_credentials_path", "")
        if not gdrive_creds or str(gdrive_creds).startswith("YOUR_"):
            errors.append("  - Google Drive credentials path not set")
        elif not Path(gdrive_creds).exists():
            errors.append(f"  - Google Drive credentials file not found: {gdrive_creds}")

        gdrive_folder = cfg.get("gdrive_folder_id", "")
        if not gdrive_folder or str(gdrive_folder).startswith("YOUR_"):
            errors.append("  - Google Drive folder ID not set")

    if errors:
        print("[ERROR] Configuration issues found:")
        for e in errors:
            print(e)
        sys.exit(1)


def _sanitize(text: str) -> str:
    return re.sub(r'[^\w\s-]', '', text).strip().replace(' ', '_')[:50]


def _make_job_id(job: dict) -> str:
    """Create a short readable job ID from the job hash."""
    h = make_hash(job["title"], job["company"], job["location"])
    return h[:8]


def run_pipeline(cfg: dict, dry_run: bool = False, skip_drive: bool = False, skip_docs: bool = False):
    """Main pipeline: scrape → dedup → match → generate docs → upload → notify."""

    # Extract resume text
    resume_path = cfg.get("resume_path", "resume.docx")
    print(f"\n📄 Loading resume: {resume_path}")
    resume_text = extract_resume_text(resume_path)
    print(f"   Extracted {len(resume_text)} characters from resume.")
    print(f"   Original DOCX will be used as template for tailored versions.\n")

    # Config values
    serpapi_key = cfg["serpapi_key"]
    openai_key = cfg["openai_api_key"]
    openai_model = cfg.get("openai_model", "gpt-4o-mini")
    bot_token = cfg["telegram_bot_token"]
    chat_id = str(cfg["telegram_chat_id"])
    days_back = cfg.get("days_back", 7)
    threshold = cfg.get("match_threshold", 65)
    max_results = cfg.get("max_results_per_combo", 30)
    combos = cfg.get("search_combos", [])

    # Google Drive config
    gdrive_enabled = cfg.get("gdrive_enabled", False) and not skip_drive and not skip_docs
    gdrive_creds = cfg.get("gdrive_credentials_path", "")
    gdrive_folder_id = cfg.get("gdrive_folder_id", "")

    if gdrive_enabled:
        from drive_uploader import upload_to_drive
        print("☁️  Google Drive upload: ENABLED")
    else:
        print("☁️  Google Drive upload: DISABLED")

    generate_docs = not skip_docs
    if generate_docs:
        from doc_generator import create_tailored_resume, create_cover_letter
        print("📝 Tailored docs generation: ENABLED")
    else:
        print("📝 Tailored docs generation: DISABLED")

    # Stats
    total_scraped = 0
    skipped_dupes = 0
    new_jobs = 0
    matched_sent = 0
    docs_generated = 0
    drive_uploaded = 0

    conn = get_connection()

    # Create a temp dir for generated documents
    with tempfile.TemporaryDirectory(prefix="jobscout_") as tmp_dir:

        for i, combo in enumerate(combos, 1):
            title = combo["title"]
            location = combo["location"]
            seniority = combo.get("seniority", "")

            print(f"\n{'='*60}")
            print(f"🔍 [{i}/{len(combos)}] Searching: {title} | {location} | {seniority or 'Any level'}")
            print(f"{'='*60}")

            # Build dedup callback for smart pagination
            def _is_seen_check(job_title, job_company, job_location):
                return is_seen(conn, make_hash(job_title, job_company, job_location))

            jobs = scrape_jobs(
                api_key=serpapi_key,
                title=title,
                location=location,
                seniority=seniority,
                days_back=days_back,
                max_results=max_results,
                is_seen_fn=_is_seen_check,
            )

            total_scraped += len(jobs)
            print(f"   Found {len(jobs)} jobs from SerpAPI\n")

            for j, job in enumerate(jobs, 1):
                job_hash = make_hash(job["title"], job["company"], job["location"])

                # Dedup check
                if is_seen(conn, job_hash):
                    skipped_dupes += 1
                    print(f"   [{j}] ♻️  SKIP (duplicate): {job['title']} @ {job['company']}")
                    continue

                new_jobs += 1
                print(f"   [{j}] 🆕 NEW: {job['title']} @ {job['company']}")

                # ── Step 1: AI Matching ──
                print(f"       🤖 Scoring match...")
                match_result = match_resume_to_job(
                    api_key=openai_key,
                    resume_text=resume_text,
                    job_title=job["title"],
                    job_company=job["company"],
                    job_description=job["description"],
                    model=openai_model,
                )

                score = match_result.get("score", 0)
                reasoning = match_result.get("reasoning", "")
                key_matches = match_result.get("key_matches", [])
                key_gaps = match_result.get("key_gaps", [])
                recommendation = match_result.get("recommendation", "")
                seniority_fit = match_result.get("seniority_fit", "")

                is_match = score >= threshold

                if match_result.get("error"):
                    print(f"       ⚠️  AI Error: {match_result['error']}")

                print(f"       Score: {score}/100 | {recommendation} | Seniority: {seniority_fit}")
                print(f"       {'✅ MATCH' if is_match else '❌ Below threshold'}")

                # Store in DB (always, to avoid re-processing)
                mark_seen(conn, job_hash, job["title"], job["company"], job["location"],
                           job["link"], score, is_match)

                if not is_match:
                    continue

                # ── Step 2: Generate Tailored Docs (if matched) ──
                job_id = _make_job_id(job)
                resume_docx_path = None
                cover_letter_docx_path = None
                drive_link = None

                if generate_docs and not dry_run:
                    print(f"       📝 Generating tailored resume...")
                    resume_data = generate_tailored_resume(
                        api_key=openai_key,
                        resume_text=resume_text,
                        job_title=job["title"],
                        job_company=job["company"],
                        job_description=job["description"],
                        model=openai_model,
                    )

                    if not resume_data.get("_error"):
                        safe_company = _sanitize(job["company"])
                        resume_docx_path = str(Path(tmp_dir) / f"{safe_company}_{job_id}_resume.docx")
                        create_tailored_resume(
                            original_docx_path=resume_path,
                            resume_data=resume_data,
                            job_title=job["title"],
                            job_company=job["company"],
                            output_path=resume_docx_path,
                        )
                        print(f"       ✅ Resume created")

                        print(f"       📝 Generating cover letter...")
                        cl_data = generate_cover_letter(
                            api_key=openai_key,
                            resume_text=resume_text,
                            job_title=job["title"],
                            job_company=job["company"],
                            job_description=job["description"],
                            model=openai_model,
                        )

                        if not cl_data.get("_error"):
                            cover_letter_docx_path = str(Path(tmp_dir) / f"{safe_company}_{job_id}_cover_letter.docx")
                            create_cover_letter(
                                cl_data,
                                candidate_name=resume_data.get("candidate_name", "Candidate"),
                                contact_info=resume_data.get("contact_info", ""),
                                job_title=job["title"],
                                job_company=job["company"],
                                output_path=cover_letter_docx_path,
                            )
                            docs_generated += 1
                            print(f"       ✅ Cover letter created")
                        else:
                            print(f"       ⚠️  Cover letter generation failed: {cl_data['_error']}")
                    else:
                        print(f"       ⚠️  Resume generation failed: {resume_data['_error']}")

                # ── Step 3: Upload to Google Drive ──
                if gdrive_enabled and resume_docx_path and cover_letter_docx_path and not dry_run:
                    print(f"       ☁️  Uploading to Google Drive...")
                    drive_result = upload_to_drive(
                        credentials_path=gdrive_creds,
                        parent_folder_id=gdrive_folder_id,
                        company=job["company"],
                        job_id=job_id,
                        resume_path=resume_docx_path,
                        cover_letter_path=cover_letter_docx_path,
                    )

                    if not drive_result.get("error"):
                        drive_link = drive_result["folder_link"]
                        drive_uploaded += 1
                        print(f"       ✅ Uploaded → {drive_link}")
                    else:
                        print(f"       ⚠️  Drive upload failed: {drive_result['error']}")

                # ── Step 4: Send Telegram notification ──
                if dry_run:
                    print(f"       🏃 DRY RUN — would send to Telegram")
                else:
                    # Append Drive link to reasoning if available
                    full_reasoning = reasoning
                    if drive_link:
                        full_reasoning += f"\n\n📁 Tailored docs: {drive_link}"

                    success = send_job_message(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        title=job["title"],
                        company=job["company"],
                        location=job["location"],
                        link=job["link"],
                        score=score,
                        reasoning=full_reasoning,
                        key_matches=key_matches,
                        key_gaps=key_gaps,
                        posted_at=job.get("posted_at", ""),
                    )
                    if success:
                        matched_sent += 1
                        print(f"       📨 Sent to Telegram!")
                    else:
                        print(f"       ❌ Failed to send to Telegram")

    # Summary
    print(f"\n{'='*60}")
    print(f"📋 RUN SUMMARY")
    print(f"{'='*60}")
    print(f"   🔍 Total scraped:         {total_scraped}")
    print(f"   🆕 New jobs:               {new_jobs}")
    print(f"   ♻️  Duplicates skipped:     {skipped_dupes}")
    print(f"   🎯 Matched & sent:         {matched_sent}")
    print(f"   📝 Tailored docs created:  {docs_generated}")
    print(f"   ☁️  Uploaded to Drive:      {drive_uploaded}")

    db_stats = get_stats(conn)
    print(f"   📦 Total in database:      {db_stats['total_seen']}")
    print(f"{'='*60}\n")

    # Send summary to Telegram
    if not dry_run and matched_sent > 0:
        send_summary_message(bot_token, chat_id, total_scraped, new_jobs, matched_sent, skipped_dupes)

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Job Scout — Scrape, match, tailor, and notify"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--days-back", type=int, help="Override days_back from config")
    parser.add_argument("--threshold", type=int, help="Override match_threshold from config")
    parser.add_argument("--dry-run", action="store_true", help="Scrape & match but skip Telegram/Drive")
    parser.add_argument("--no-drive", action="store_true", help="Skip Google Drive upload")
    parser.add_argument("--no-docs", action="store_true", help="Skip tailored resume/cover letter generation")
    args = parser.parse_args()

    print("🚀 Job Scout starting...\n")

    cfg = load_config(args.config)

    # CLI overrides
    if args.days_back is not None:
        cfg["days_back"] = args.days_back
    if args.threshold is not None:
        cfg["match_threshold"] = args.threshold

    validate_config(cfg, skip_drive=args.no_drive or args.dry_run)

    print(f"⚙️  Settings: days_back={cfg['days_back']}, threshold={cfg['match_threshold']}, "
          f"combos={len(cfg['search_combos'])}")

    start = time.time()
    run_pipeline(cfg, dry_run=args.dry_run, skip_drive=args.no_drive, skip_docs=args.no_docs)
    elapsed = time.time() - start
    print(f"⏱️  Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
