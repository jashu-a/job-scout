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

from db import (get_connection, make_hash, is_seen, is_duplicate, mark_seen, get_stats,
                hash_resume, get_metadata, set_metadata, get_rescore_candidates, update_job_score,
                get_next_job_id)
from scraper import scrape_jobs, is_job_still_active
from matcher import match_resume_to_job, generate_tailored_resume, generate_cover_letter
from doc_generator import create_tailored_resume, create_cover_letter
from drive_uploader import upload_to_drive, download_db, upload_db
from notifier import send_job_message, send_summary_message, send_error_message
from resume_parser import extract_resume_text
import os


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
        import os
        # Check for OAuth env vars (new method) or credentials file (legacy)
        has_oauth = all([
            os.environ.get("GDRIVE_CLIENT_ID"),
            os.environ.get("GDRIVE_CLIENT_SECRET"),
            os.environ.get("GDRIVE_REFRESH_TOKEN"),
        ])
        gdrive_creds = cfg.get("gdrive_credentials_path", "")
        has_service_account = gdrive_creds and Path(gdrive_creds).exists()

        if not has_oauth and not has_service_account:
            errors.append("  - Google Drive credentials not found. Set GDRIVE_CLIENT_ID, "
                          "GDRIVE_CLIENT_SECRET, and GDRIVE_REFRESH_TOKEN env vars.")

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


def _docx_to_pdf(docx_path: str, pdf_path: str) -> bool:
    """Convert a .docx file to .pdf using python-docx2pdf or LibreOffice."""
    try:
        import subprocess
        # Use LibreOffice for reliable conversion (available on GitHub Actions ubuntu)
        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir",
             str(Path(pdf_path).parent), docx_path],
            capture_output=True, timeout=60,
        )
        # LibreOffice outputs to same dir with .pdf extension
        expected = Path(docx_path).with_suffix(".pdf")
        if expected.exists():
            if str(expected) != pdf_path:
                expected.rename(pdf_path)
            return True
        return False
    except Exception as e:
        print(f"       ⚠️  PDF conversion failed: {e}")
        return False


def _process_new_job(
    job: dict, conn, resume_text: str, resume_path: str,
    openai_key: str, openai_model: str, threshold: int,
    generate_docs: bool, gdrive_enabled: bool, gdrive_folder_id: str,
    bot_token: str, chat_id: str, dry_run: bool, tmp_dir: str,
    stats: dict,
):
    """
    Process a single new job: AI match → generate docs → upload → notify.
    Updates stats dict in-place. Returns True if job was matched and sent.
    """
    job_hash = make_hash(job["title"], job["company"], job["location"])

    # Check if the job posting is still active before spending OpenAI credits
    is_active, closed_reason = is_job_still_active(job.get("link", ""))
    if not is_active:
        print(f"       ⛔ Job no longer active: {closed_reason}")
        mark_seen(conn, job_hash, job["title"], job["company"], job.get("location", ""),
                   job.get("link", ""), 0, False)
        return False

    # AI Matching
    print(f"       🤖 Scoring match...")
    match_result = match_resume_to_job(
        api_key=openai_key,
        resume_text=resume_text,
        job_title=job["title"],
        job_company=job["company"],
        job_description=job.get("description", ""),
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

    # Store in DB
    mark_seen(conn, job_hash, job["title"], job["company"], job.get("location", ""),
               job.get("link", ""), score, is_match)

    if not is_match:
        return False

    # Check send limit
    if stats["matched_sent"] >= stats["max_sends"]:
        print(f"       ⏹️  Reached max Telegram sends ({stats['max_sends']})")
        return False

    # Assign sequential job ID
    job_id = get_next_job_id(conn)

    # Generate docs
    resume_pdf_path = None
    cover_letter_pdf_path = None
    drive_link = None

    if generate_docs and not dry_run:
        safe_company = _sanitize(job["company"]) or "Unknown"

        print(f"       📝 Generating tailored resume...")
        resume_data = generate_tailored_resume(
            api_key=openai_key,
            resume_text=resume_text,
            job_title=job["title"],
            job_company=job["company"],
            job_description=job.get("description", ""),
            model=openai_model,
        )

        candidate_name = _sanitize(resume_data.get("candidate_name", "Candidate")) if not resume_data.get("_error") else "Candidate"

        if not resume_data.get("_error"):
            # Create tailored DOCX first
            resume_docx_path = str(Path(tmp_dir) / f"resume_{job_id}.docx")
            is_pdf_resume = resume_path.lower().endswith(".pdf")

            if is_pdf_resume:
                # PDF resume: can't use as DOCX template, create fresh DOCX from AI output
                # doc_generator will handle creating a new document from scratch
                create_tailored_resume(
                    original_docx_path=None,
                    resume_data=resume_data,
                    job_title=job["title"],
                    job_company=job["company"],
                    output_path=resume_docx_path,
                )
            else:
                # DOCX resume: use as template for formatting
                create_tailored_resume(
                    original_docx_path=resume_path,
                    resume_data=resume_data,
                    job_title=job["title"],
                    job_company=job["company"],
                    output_path=resume_docx_path,
                )
            print(f"       ✅ Resume created")

            # Convert to PDF with proper naming: CandidateName_ID_CompanyName.pdf
            resume_pdf_name = f"{candidate_name}_{job_id}_{safe_company}_Resume.pdf"
            resume_pdf_path = str(Path(tmp_dir) / resume_pdf_name)
            if _docx_to_pdf(resume_docx_path, resume_pdf_path):
                print(f"       ✅ Converted to PDF: {resume_pdf_name}")
            else:
                # Fallback: upload DOCX if PDF conversion fails
                print(f"       ⚠️  PDF conversion failed, using DOCX")
                resume_pdf_path = resume_docx_path

            print(f"       📝 Generating cover letter...")
            cl_data = generate_cover_letter(
                api_key=openai_key,
                resume_text=resume_text,
                job_title=job["title"],
                job_company=job["company"],
                job_description=job.get("description", ""),
                model=openai_model,
            )

            if not cl_data.get("_error"):
                cover_docx_path = str(Path(tmp_dir) / f"cover_{job_id}.docx")
                create_cover_letter(
                    cl_data,
                    candidate_name=resume_data.get("candidate_name", "Candidate"),
                    contact_info=resume_data.get("contact_info", ""),
                    job_title=job["title"],
                    job_company=job["company"],
                    output_path=cover_docx_path,
                )
                stats["docs_generated"] += 1
                print(f"       ✅ Cover letter created")

                # Convert cover letter to PDF
                cover_pdf_name = f"{candidate_name}_{job_id}_{safe_company}_CoverLetter.pdf"
                cover_letter_pdf_path = str(Path(tmp_dir) / cover_pdf_name)
                if _docx_to_pdf(cover_docx_path, cover_letter_pdf_path):
                    print(f"       ✅ Converted to PDF: {cover_pdf_name}")
                else:
                    print(f"       ⚠️  PDF conversion failed, using DOCX")
                    cover_letter_pdf_path = cover_docx_path
            else:
                print(f"       ⚠️  Cover letter generation failed: {cl_data['_error']}")
        else:
            print(f"       ⚠️  Resume generation failed: {resume_data['_error']}")

    # Upload to Drive
    if gdrive_enabled and resume_pdf_path and cover_letter_pdf_path and not dry_run:
        print(f"       ☁️  Uploading to Google Drive...")
        drive_result = upload_to_drive(
            parent_folder_id=gdrive_folder_id,
            company=job["company"],
            job_id=str(job_id),
            resume_path=resume_pdf_path,
            cover_letter_path=cover_letter_pdf_path,
        )

        if not drive_result.get("error"):
            drive_link = drive_result["folder_link"]
            stats["drive_uploaded"] += 1
            print(f"       ✅ Uploaded → {drive_link}")
        else:
            print(f"       ⚠️  Drive upload failed: {drive_result['error']}")

    # Send Telegram notification
    if dry_run:
        print(f"       🏃 DRY RUN — would send to Telegram (Job #{job_id})")
        stats["matched_sent"] += 1
        return True

    full_reasoning = reasoning
    if drive_link:
        full_reasoning += f"\n\n📁 Tailored docs: {drive_link}"

    success = send_job_message(
        bot_token=bot_token,
        chat_id=chat_id,
        title=job["title"],
        company=job["company"],
        location=job.get("location", ""),
        link=job.get("link", ""),
        score=score,
        reasoning=full_reasoning,
        key_matches=key_matches,
        key_gaps=key_gaps,
        posted_at=job.get("posted_at", ""),
        job_id=job_id,
    )
    if success:
        stats["matched_sent"] += 1
        print(f"       📨 Sent to Telegram! (Job #{job_id})")
    else:
        print(f"       ❌ Failed to send to Telegram")

    return success


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
    threshold = cfg.get("match_threshold", 75)
    max_results = cfg.get("max_results_per_combo", 100)
    combos = cfg.get("search_combos", [])
    sources = cfg.get("sources", ["google_jobs", "linkedin", "tokyodev", "indeed"])
    max_sends = cfg.get("max_telegram_sends", 50)
    min_matches_per_run = cfg.get("min_matches_per_run", 10)
    expansion_max_sends = 10  # Hard cap: max 10 new jobs from expansion per run

    # Related titles for auto-expansion when results are low
    TITLE_EXPANSIONS = {
        # Software Engineering
        "software engineer": ["Full Stack Developer", "Application Developer", "Web Developer"],
        "backend developer": ["Backend Engineer", "API Developer", "Server-Side Engineer"],
        "backend engineer": ["Backend Developer", "API Developer", "Server-Side Engineer"],
        "frontend developer": ["Frontend Engineer", "UI Developer", "React Developer"],
        "frontend engineer": ["Frontend Developer", "UI Developer", "React Developer"],
        "full stack developer": ["Fullstack Engineer", "Software Engineer", "Web Developer"],
        "fullstack engineer": ["Full Stack Developer", "Software Engineer", "Web Developer"],
        # Infrastructure / DevOps
        "platform engineer": ["DevOps Engineer", "SRE", "Cloud Engineer", "Infrastructure Engineer"],
        "devops engineer": ["Platform Engineer", "SRE", "Cloud Engineer", "Infrastructure Engineer"],
        "sre": ["Site Reliability Engineer", "DevOps Engineer", "Platform Engineer"],
        "cloud engineer": ["DevOps Engineer", "Platform Engineer", "AWS Engineer"],
        "infrastructure engineer": ["Cloud Engineer", "Platform Engineer", "DevOps Engineer"],
        # Data Engineering
        "data engineer": ["Data Platform Engineer", "ETL Developer", "Analytics Engineer"],
        "analytics engineer": ["Data Engineer", "BI Engineer", "Data Analyst"],
        # Data Science / ML / AI
        "data scientist": ["ML Engineer", "Machine Learning Engineer", "AI Engineer", "Research Scientist"],
        "ml engineer": ["Machine Learning Engineer", "Data Scientist", "AI Engineer", "MLOps Engineer"],
        "machine learning engineer": ["ML Engineer", "AI Engineer", "Data Scientist", "Deep Learning Engineer"],
        "ai engineer": ["Machine Learning Engineer", "ML Engineer", "NLP Engineer", "AI Research Engineer"],
        "ai research engineer": ["Research Scientist", "Machine Learning Engineer", "AI Engineer"],
        "nlp engineer": ["AI Engineer", "Machine Learning Engineer", "Computational Linguist"],
        "mlops engineer": ["ML Engineer", "Platform Engineer", "Data Engineer"],
        "deep learning engineer": ["Machine Learning Engineer", "AI Engineer", "Computer Vision Engineer"],
        "computer vision engineer": ["Deep Learning Engineer", "AI Engineer", "Machine Learning Engineer"],
        # Cybersecurity
        "cybersecurity analyst": ["Security Analyst", "SOC Analyst", "Information Security Analyst", "Threat Analyst"],
        "security analyst": ["Cybersecurity Analyst", "SOC Analyst", "Threat Intelligence Analyst"],
        "security engineer": ["Cybersecurity Engineer", "Cloud Security Engineer", "Application Security Engineer"],
        "cybersecurity engineer": ["Security Engineer", "Penetration Tester", "Security Architect"],
        "penetration tester": ["Ethical Hacker", "Security Consultant", "Offensive Security Engineer"],
        "soc analyst": ["Security Analyst", "Cybersecurity Analyst", "Incident Response Analyst"],
        "information security analyst": ["Cybersecurity Analyst", "Security Analyst", "GRC Analyst"],
        "security architect": ["Cybersecurity Engineer", "Security Engineer", "Cloud Security Architect"],
        "cloud security engineer": ["Security Engineer", "DevSecOps Engineer", "Cloud Security Architect"],
        "devsecops engineer": ["DevOps Engineer", "Security Engineer", "Cloud Security Engineer"],
        # Project / Product Management
        "project manager": ["Program Manager", "Technical Project Manager", "Scrum Master", "Delivery Manager"],
        "program manager": ["Project Manager", "Technical Program Manager", "Portfolio Manager"],
        "technical project manager": ["Project Manager", "Engineering Manager", "Scrum Master"],
        "product manager": ["Product Owner", "Technical Product Manager", "Program Manager"],
        "product owner": ["Product Manager", "Scrum Master", "Business Analyst"],
        "scrum master": ["Agile Coach", "Project Manager", "Delivery Manager"],
        "engineering manager": ["Technical Lead", "Development Manager", "Team Lead"],
        "technical program manager": ["Program Manager", "Engineering Manager", "Technical Project Manager"],
    }

    # Google Drive config
    gdrive_enabled = cfg.get("gdrive_enabled", False) and not skip_drive and not skip_docs
    gdrive_creds = cfg.get("gdrive_credentials_path", "")
    gdrive_folder_id = cfg.get("gdrive_folder_id", "")
    db_remote_name = cfg.get("db_name", "jobs.db")

    if gdrive_enabled:
        print("☁️  Google Drive upload: ENABLED")

        # Download jobs.db from Drive for persistence between runs
        print(f"\n📥 Restoring database from Google Drive ({db_remote_name})...")
        download_db(gdrive_folder_id, remote_name=db_remote_name)
    else:
        print("☁️  Google Drive upload: DISABLED")

    generate_docs = not skip_docs
    if generate_docs:
        print("📝 Tailored docs generation: ENABLED")
    else:
        print("📝 Tailored docs generation: DISABLED")

    # Stats (dict so _process_new_job can update in-place)
    stats = {
        "total_scraped": 0,
        "skipped_dupes": 0,
        "new_jobs": 0,
        "matched_sent": 0,
        "docs_generated": 0,
        "drive_uploaded": 0,
        "max_sends": max_sends,
    }

    conn = get_connection()

    # Log DB state to verify cache persistence between runs
    db_stats = get_stats(conn)
    print(f"\n📦 Database state: {db_stats['total_seen']} jobs seen, {db_stats['total_matched']} matched")
    if db_stats['total_seen'] == 0:
        print(f"   ⚠️  Database is empty — this may be the first run or cache was not restored")
    print()

    # ══════════════════════════════════════════════════════════════════════
    # RESUME CHANGE DETECTION — auto-rescore if resume was updated
    # ══════════════════════════════════════════════════════════════════════
    current_resume_hash = hash_resume(resume_text)
    stored_resume_hash = get_metadata(conn, "resume_hash")

    if stored_resume_hash and stored_resume_hash != current_resume_hash and not dry_run:
        print(f"🔄 Resume change detected! Rescoring previously rejected jobs...")
        print(f"   Old hash: {stored_resume_hash} → New hash: {current_resume_hash}\n")

        candidates = get_rescore_candidates(conn, threshold)
        if candidates:
            print(f"   Found {len(candidates)} jobs below threshold to rescore\n")
            rescore_sent = 0
            rescore_max = 10  # Cap rescore notifications per run

            for idx, cand in enumerate(candidates, 1):
                if rescore_sent >= rescore_max:
                    print(f"\n   ✅ Rescore cap reached ({rescore_max} jobs sent) — stopping")
                    break

                print(f"   [{idx}/{len(candidates)}] Rescoring: {cand['title']} @ {cand['company']} (was {cand['match_score']:.0f})")

                # Fetch description from job page for better scoring
                description = ""
                if cand["link"]:
                    from scraper import _fetch_page_text
                    description = _fetch_page_text(cand["link"], max_chars=4000)

                match_result = match_resume_to_job(
                    api_key=openai_key,
                    resume_text=resume_text,
                    job_title=cand["title"],
                    job_company=cand["company"],
                    job_description=description or f"{cand['title']} at {cand['company']}",
                    model=openai_model,
                )

                new_score = match_result.get("score", 0)
                reasoning = match_result.get("reasoning", "")
                key_matches = match_result.get("key_matches", [])
                key_gaps = match_result.get("key_gaps", [])

                is_match = new_score >= threshold
                update_job_score(conn, cand["job_hash"], new_score, is_match)

                print(f"       New score: {new_score}/100 (was {cand['match_score']:.0f}) {'✅ NOW MATCHES!' if is_match else '❌ Still below'}")

                if is_match:
                    rescore_job_id = get_next_job_id(conn)
                    stats["matched_sent"] += 1
                    rescore_sent += 1

                    # Send to Telegram
                    full_reasoning = f"🔄 RESCORED (resume updated)\n{reasoning}"
                    send_job_message(
                        bot_token=bot_token, chat_id=chat_id,
                        title=cand["title"], company=cand["company"],
                        location=cand["location"], link=cand["link"],
                        score=new_score, reasoning=full_reasoning,
                        key_matches=key_matches, key_gaps=key_gaps,
                        job_id=rescore_job_id,
                    )
                    print(f"       📨 Sent to Telegram! (Job #{rescore_job_id})")

            print(f"\n   🔄 Rescore complete: {rescore_sent} newly matched jobs sent")
        else:
            print(f"   No below-threshold jobs to rescore")
    elif not stored_resume_hash:
        print(f"📄 First run with this resume — storing hash for future change detection")

    # Always update the stored resume hash
    set_metadata(conn, "resume_hash", current_resume_hash)

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
                sources=sources,
            )

            stats["total_scraped"] += len(jobs)
            print(f"   Found {len(jobs)} jobs\n")

            for j, job in enumerate(jobs, 1):
                job_hash = make_hash(job["title"], job["company"], job["location"])

                # Dedup check (both content hash and URL)
                if is_duplicate(conn, job_hash, job.get("link", "")):
                    stats["skipped_dupes"] += 1
                    print(f"   [{j}] ♻️  SKIP (duplicate): {job['title']} @ {job['company']}")
                    continue

                stats["new_jobs"] += 1
                desc_len = len(job.get("description", ""))
                print(f"   [{j}] 🆕 NEW: {job['title']} @ {job['company']} [{job.get('source', '?')}]")
                print(f"       📄 Description: {desc_len} chars")

                _process_new_job(
                    job=job, conn=conn, resume_text=resume_text, resume_path=resume_path,
                    openai_key=openai_key, openai_model=openai_model, threshold=threshold,
                    generate_docs=generate_docs, gdrive_enabled=gdrive_enabled,
                    gdrive_folder_id=gdrive_folder_id, bot_token=bot_token, chat_id=chat_id,
                    dry_run=dry_run, tmp_dir=tmp_dir, stats=stats,
                )

    # ══════════════════════════════════════════════════════════════════════
    # AUTO-EXPANSION: If not enough matches, widen the search
    # ══════════════════════════════════════════════════════════════════════
    if stats["matched_sent"] < min_matches_per_run and not dry_run:
        print(f"\n{'='*60}")
        print(f"📈 EXPANSION PASS — only {stats['matched_sent']} matches so far (target: {min_matches_per_run})")
        print(f"   Max {expansion_max_sends} new jobs from expansion")
        print(f"{'='*60}")

        expansion_sent = 0  # Track expansion sends separately

        already_searched = {c["title"].lower() for c in combos}
        expansion_combos = []

        for combo in combos:
            base_title = combo["title"].lower()
            related = TITLE_EXPANSIONS.get(base_title, [])
            for alt_title in related:
                if alt_title.lower() not in already_searched:
                    already_searched.add(alt_title.lower())
                    expansion_combos.append({
                        "title": alt_title,
                        "location": combo["location"],
                        "seniority": combo.get("seniority", ""),
                    })

        serpapi_only_sources = [s for s in sources if s in ("google_jobs", "indeed")]
        wider_date_combos = []
        if serpapi_only_sources and days_back < 14:
            for combo in combos:
                wider_date_combos.append({
                    "title": combo["title"],
                    "location": combo["location"],
                    "seniority": combo.get("seniority", ""),
                })

        if not expansion_combos and not wider_date_combos:
            print("   ⚠️  No additional title variations or date-sensitive sources to try")
        else:
            # Phase 1: New title variations (all sources, same days_back)
            if expansion_combos:
                print(f"\n   🔄 Phase 1: Trying {len(expansion_combos)} new title variations\n")

                for i, combo in enumerate(expansion_combos, 1):
                    if expansion_sent >= expansion_max_sends:
                        print(f"\n   ✅ Expansion cap reached ({expansion_max_sends} jobs sent) — stopping")
                        break

                    title = combo["title"]
                    location = combo["location"]
                    seniority = combo.get("seniority", "")

                    print(f"\n   {'─'*50}")
                    print(f"   📈 [Expand {i}/{len(expansion_combos)}] {title} | {location}")
                    print(f"   {'─'*50}")

                    def _is_seen_check(job_title, job_company, job_location):
                        return is_seen(conn, make_hash(job_title, job_company, job_location))

                    jobs = scrape_jobs(
                        api_key=serpapi_key, title=title, location=location,
                        seniority=seniority, days_back=days_back,
                        max_results=max_results, is_seen_fn=_is_seen_check, sources=sources,
                    )

                    stats["total_scraped"] += len(jobs)
                    print(f"   Found {len(jobs)} jobs\n")

                    for j, job in enumerate(jobs, 1):
                        if expansion_sent >= expansion_max_sends:
                            break

                        job_hash = make_hash(job["title"], job["company"], job["location"])
                        if is_duplicate(conn, job_hash, job.get("link", "")):
                            stats["skipped_dupes"] += 1
                            print(f"   [{j}] ♻️  SKIP (duplicate): {job['title']} @ {job['company']}")
                            continue

                        stats["new_jobs"] += 1
                        print(f"   [{j}] 🆕 NEW: {job['title']} @ {job['company']} [{job.get('source', '?')}]")
                        print(f"       📄 Description: {len(job.get('description', ''))} chars")

                        sent = _process_new_job(
                            job=job, conn=conn, resume_text=resume_text, resume_path=resume_path,
                            openai_key=openai_key, openai_model=openai_model, threshold=threshold,
                            generate_docs=generate_docs, gdrive_enabled=gdrive_enabled,
                            gdrive_folder_id=gdrive_folder_id, bot_token=bot_token, chat_id=chat_id,
                            dry_run=dry_run, tmp_dir=tmp_dir, stats=stats,
                        )
                        if sent:
                            expansion_sent += 1

            # Phase 2: Wider date range, SerpAPI sources only
            if expansion_sent < expansion_max_sends and wider_date_combos and serpapi_only_sources:
                print(f"\n   🔄 Phase 2: Retrying original titles with days_back=14 (SerpAPI sources only)\n")

                for i, combo in enumerate(wider_date_combos, 1):
                    if expansion_sent >= expansion_max_sends:
                        print(f"\n   ✅ Expansion cap reached ({expansion_max_sends} jobs sent) — stopping")
                        break

                    title = combo["title"]
                    location = combo["location"]
                    seniority = combo.get("seniority", "")

                    print(f"\n   {'─'*50}")
                    print(f"   📈 [Wider {i}/{len(wider_date_combos)}] {title} | {location} | 14 days")
                    print(f"   {'─'*50}")

                    def _is_seen_check(job_title, job_company, job_location):
                        return is_seen(conn, make_hash(job_title, job_company, job_location))

                    jobs = scrape_jobs(
                        api_key=serpapi_key, title=title, location=location,
                        seniority=seniority, days_back=14,
                        max_results=max_results, is_seen_fn=_is_seen_check,
                        sources=serpapi_only_sources,
                    )

                    stats["total_scraped"] += len(jobs)
                    print(f"   Found {len(jobs)} jobs\n")

                    for j, job in enumerate(jobs, 1):
                        if expansion_sent >= expansion_max_sends:
                            break

                        job_hash = make_hash(job["title"], job["company"], job["location"])
                        if is_duplicate(conn, job_hash, job.get("link", "")):
                            stats["skipped_dupes"] += 1
                            print(f"   [{j}] ♻️  SKIP (duplicate): {job['title']} @ {job['company']}")
                            continue

                        stats["new_jobs"] += 1
                        print(f"   [{j}] 🆕 NEW: {job['title']} @ {job['company']} [{job.get('source', '?')}]")
                        print(f"       📄 Description: {len(job.get('description', ''))} chars")

                        sent = _process_new_job(
                            job=job, conn=conn, resume_text=resume_text, resume_path=resume_path,
                            openai_key=openai_key, openai_model=openai_model, threshold=threshold,
                            generate_docs=generate_docs, gdrive_enabled=gdrive_enabled,
                            gdrive_folder_id=gdrive_folder_id, bot_token=bot_token, chat_id=chat_id,
                            dry_run=dry_run, tmp_dir=tmp_dir, stats=stats,
                        )
                        if sent:
                            expansion_sent += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"📋 RUN SUMMARY")
    print(f"{'='*60}")
    print(f"   🔍 Total scraped:         {stats['total_scraped']}")
    print(f"   🆕 New jobs:               {stats['new_jobs']}")
    print(f"   ♻️  Duplicates skipped:     {stats['skipped_dupes']}")
    print(f"   🎯 Matched & sent:         {stats['matched_sent']}")
    print(f"   📝 Tailored docs created:  {stats['docs_generated']}")
    print(f"   ☁️  Uploaded to Drive:      {stats['drive_uploaded']}")

    db_stats = get_stats(conn)
    print(f"   📦 Total in database:      {db_stats['total_seen']}")
    print(f"{'='*60}\n")

    # Send summary to Telegram
    if not dry_run and stats["matched_sent"] > 0:
        send_summary_message(bot_token, chat_id, stats["total_scraped"],
                             stats["new_jobs"], stats["matched_sent"], stats["skipped_dupes"])

    conn.close()

    # Upload jobs.db back to Drive for persistence
    if gdrive_enabled:
        print("\n📤 Saving database to Google Drive...")
        upload_db(gdrive_folder_id, remote_name=db_remote_name)


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

    bot_token = cfg.get("telegram_bot_token", "")
    chat_id = str(cfg.get("telegram_chat_id", ""))

    start = time.time()
    try:
        run_pipeline(cfg, dry_run=args.dry_run, skip_drive=args.no_drive, skip_docs=args.no_docs)
    except Exception as e:
        error_type = type(e).__name__
        error_detail = str(e)[:500]

        # Map common errors to user-friendly causes
        cause_map = {
            "invalid_grant": "Google Drive OAuth token expired. Regenerate refresh token and update GDRIVE_REFRESH_TOKEN secret.",
            "quota": "API quota exceeded. Check SerpAPI or OpenAI usage limits.",
            "rate_limit": "API rate limit hit. The run may have sent too many requests too quickly.",
            "timeout": "A request timed out. A job source may be unreachable.",
            "SSLError": "SSL certificate error. Likely a network/proxy issue.",
            "AuthenticationError": "OpenAI API key invalid or expired. Update OPENAI_API_KEY secret.",
            "Unauthorized": "API authentication failed. Check your API keys in GitHub Secrets.",
            "OperationalError": "Database error. The jobs.db file may be corrupted. Delete it from Google Drive to reset.",
        }

        possible_cause = ""
        for key, cause in cause_map.items():
            if key.lower() in error_detail.lower() or key.lower() in error_type.lower():
                possible_cause = cause
                break

        print(f"\n❌ PIPELINE ERROR: {error_type}: {error_detail}")
        if possible_cause:
            print(f"   💡 Possible cause: {possible_cause}")

        # Send error to Telegram (best-effort, don't crash if this also fails)
        if bot_token and chat_id:
            try:
                send_error_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    error_type=error_type,
                    error_detail=error_detail,
                    possible_cause=possible_cause,
                )
            except Exception:
                print("   ⚠️  Could not send error notification to Telegram")

        # Re-raise so GitHub Actions marks the run as failed
        raise

    elapsed = time.time() - start
    print(f"⏱️  Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()