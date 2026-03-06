"""
Telegram notifier.
Sends matched job listings as formatted messages to a Telegram chat.
"""

import requests
import time


TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_job_message(
    bot_token: str,
    chat_id: str,
    title: str,
    company: str,
    location: str,
    link: str,
    score: int,
    reasoning: str,
    key_matches: list[str],
    key_gaps: list[str],
    posted_at: str = "",
) -> bool:
    """Send a single job match as a formatted Telegram message."""

    matches_str = ", ".join(key_matches) if key_matches else "N/A"
    gaps_str = ", ".join(key_gaps) if key_gaps else "None noted"

    message = (
        f"🎯 <b>Job Match Found!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}\n"
    )

    if posted_at:
        message += f"🕐 Posted: {posted_at}\n"

    message += (
        f"\n📊 <b>Match Score: {score}/100</b>\n"
        f"💬 {reasoning}\n"
        f"\n✅ <b>Matches:</b> {matches_str}\n"
        f"⚠️ <b>Gaps:</b> {gaps_str}\n"
    )

    if link:
        message += f"\n🔗 <a href=\"{link}\">Apply Here</a>\n"

    message += f"━━━━━━━━━━━━━━━━━━━"

    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        # Rate limit: Telegram allows ~30 msgs/sec, but let's be safe
        time.sleep(1)
        return True
    except requests.RequestException as e:
        print(f"  [ERROR] Telegram send failed: {e}")
        return False


def send_summary_message(
    bot_token: str,
    chat_id: str,
    total_scraped: int,
    new_jobs: int,
    matched_jobs: int,
    skipped_duplicates: int,
) -> bool:
    """Send a run summary to Telegram."""

    message = (
        f"📋 <b>Job Scout Run Complete</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Total scraped: {total_scraped}\n"
        f"🆕 New jobs: {new_jobs}\n"
        f"🎯 Matched & sent: {matched_jobs}\n"
        f"♻️ Duplicates skipped: {skipped_duplicates}\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [ERROR] Telegram summary failed: {e}")
        return False


def send_error_message(
    bot_token: str,
    chat_id: str,
    error_type: str,
    error_detail: str,
    possible_cause: str = "",
) -> bool:
    """Send an error/warning notification to Telegram."""

    message = (
        f"⚠️ <b>Job Scout Error</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"❌ <b>{error_type}</b>\n"
        f"📝 {error_detail}\n"
    )

    if possible_cause:
        message += f"\n💡 <b>Possible cause:</b> {possible_cause}\n"

    message += f"━━━━━━━━━━━━━━━━━━━"

    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [ERROR] Telegram error notification failed: {e}")
        return False