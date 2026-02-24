"""
Job scraper using SerpAPI's Google Jobs engine.
Fetches jobs based on title, location, seniority, and date filters.

Supports pagination with smart stopping — stops fetching once it hits
a page where all jobs are already in the database, saving API calls.
"""

import requests
from typing import Optional, Callable


SERP_API_URL = "https://serpapi.com/search.json"

# SerpAPI date filter chips mapping
DAYS_BACK_MAP = {
    1: "date_posted:today",
    3: "date_posted:3days",
    7: "date_posted:week",
    14: "date_posted:2weeks",
    30: "date_posted:month",
}


def _build_job_dict(item: dict, query: str, location: str) -> dict:
    """Parse a single SerpAPI job result into our standard format."""
    apply_links = item.get("apply_options", [])
    link = apply_links[0].get("link", "") if apply_links else ""

    if not link:
        link = item.get("share_link", "")
    if not link:
        job_id = item.get("job_id", "")
        if job_id:
            link = f"https://www.google.com/search?q={query.replace(' ', '+')}&ibp=htl;jobs&htidocid={job_id}"

    return {
        "title": item.get("title", "Unknown Title"),
        "company": item.get("company_name", "Unknown Company"),
        "location": item.get("location", location),
        "description": item.get("description", ""),
        "link": link,
        "posted_at": item.get("detected_extensions", {}).get("posted_at", ""),
        "extensions": item.get("detected_extensions", {}),
    }


def scrape_jobs(
    api_key: str,
    title: str,
    location: str,
    seniority: Optional[str] = None,
    days_back: int = 7,
    max_results: int = 10,
    is_seen_fn: Optional[Callable[[str, str, str], bool]] = None,
) -> list[dict]:
    """
    Fetch jobs from SerpAPI Google Jobs with pagination and smart stopping.

    Args:
        api_key:      SerpAPI key
        title:        Job title to search
        location:     Job location
        seniority:    Optional seniority level filter
        days_back:    Only jobs posted in the last N days
        max_results:  Maximum total jobs to return
        is_seen_fn:   Optional callback(title, company, location) -> bool
                      If provided, pagination stops early when an entire page
                      consists of already-seen jobs, saving API calls.

    Returns:
        List of job dicts with keys: title, company, location, description, link, posted_at, extensions
    """
    # Build the query string
    query = title
    if seniority:
        query = f"{seniority} {query}"

    # Map days_back to the closest SerpAPI chip
    chip = DAYS_BACK_MAP.get(days_back, "date_posted:week")
    if days_back not in DAYS_BACK_MAP:
        for threshold in sorted(DAYS_BACK_MAP.keys()):
            if threshold >= days_back:
                chip = DAYS_BACK_MAP[threshold]
                break
        else:
            chip = "date_posted:month"

    all_jobs = []
    start = 0
    page_size = 10  # Google Jobs returns max 10 per page
    max_pages = (max_results + page_size - 1) // page_size  # Ceiling division
    api_calls = 0

    for page_num in range(max_pages):
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "chips": chip,
            "api_key": api_key,
            "start": start,
        }

        try:
            resp = requests.get(SERP_API_URL, params=params, timeout=30)
            # If 400 error, log the response body and try fallback
            if resp.status_code == 400:
                print(f"  [DEBUG] SerpAPI 400 response: {resp.text[:500]}")
                # Fallback: move location into query string
                params_retry = params.copy()
                loc = params_retry.pop("location", "")
                params_retry["q"] = f"{query} {loc}".strip()
                print(f"  [DEBUG] Retrying with q='{params_retry['q']}' (no location param)")
                resp = requests.get(SERP_API_URL, params=params_retry, timeout=30)
                if resp.status_code != 200:
                    print(f"  [DEBUG] Retry also failed ({resp.status_code}): {resp.text[:500]}")
            resp.raise_for_status()
            data = resp.json()
            api_calls += 1
        except requests.RequestException as e:
            print(f"  [ERROR] SerpAPI request failed (page {page_num + 1}): {e}")
            break

        raw_jobs = data.get("jobs_results", [])

        # No more results available
        if not raw_jobs:
            print(f"  [INFO] No more results at page {page_num + 1}. Total API calls: {api_calls}")
            break

        # Parse jobs from this page
        page_jobs = [_build_job_dict(item, query, location) for item in raw_jobs]

        # Smart stop: check if ALL jobs on this page are already seen
        if is_seen_fn and page_jobs:
            new_on_page = sum(
                1 for j in page_jobs
                if not is_seen_fn(j["title"], j["company"], j["location"])
            )
            if new_on_page == 0:
                print(f"  [INFO] Page {page_num + 1}: all {len(page_jobs)} jobs already seen. "
                      f"Stopping pagination. API calls used: {api_calls}")
                break
            else:
                print(f"  [INFO] Page {page_num + 1}: {new_on_page}/{len(page_jobs)} new jobs")

        all_jobs.extend(page_jobs)

        # Check if we've collected enough
        if len(all_jobs) >= max_results:
            break

        # Move to next page
        start += page_size

    # Trim to max_results
    return all_jobs[:max_results]