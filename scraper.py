"""
Job scraper using SerpAPI's Google Jobs engine.
Uses next_page_token for pagination (start parameter is deprecated).
"""

import requests
from typing import Optional, Callable


SERP_API_URL = "https://serpapi.com/search.json"

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
    Fetch jobs from SerpAPI Google Jobs with next_page_token pagination.
    """
    query = title
    if seniority:
        query = f"{seniority} {query}"

    # Map days_back to SerpAPI chip
    chip = DAYS_BACK_MAP.get(days_back, "date_posted:week")
    if days_back not in DAYS_BACK_MAP:
        for threshold in sorted(DAYS_BACK_MAP.keys()):
            if threshold >= days_back:
                chip = DAYS_BACK_MAP[threshold]
                break
        else:
            chip = "date_posted:month"

    all_jobs = []
    api_calls = 0
    next_page_token = None
    max_pages = (max_results + 9) // 10  # ~10 results per page

    for page_num in range(max_pages):
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "chips": chip,
            "api_key": api_key,
            "hl": "en",  # Results in English
        }

        # Auto-detect country code from location string
        location_lower = location.lower()
        country_map = {
            "japan": "jp", "jp": "jp",
            "united states": "us", "usa": "us", "us": "us",
            "united kingdom": "gb", "uk": "gb",
            "canada": "ca", "germany": "de", "france": "fr",
            "australia": "au", "india": "in", "singapore": "sg",
            "netherlands": "nl", "ireland": "ie", "spain": "es",
            "italy": "it", "brazil": "br", "mexico": "mx",
            "south korea": "kr", "korea": "kr",
        }
        for key, code in country_map.items():
            if key in location_lower:
                params["gl"] = code
                break

        # Add pagination token for page 2+
        if next_page_token:
            params["next_page_token"] = next_page_token

        try:
            resp = requests.get(SERP_API_URL, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"  [ERROR] SerpAPI returned {resp.status_code}: {resp.text[:300]}")
                break
            data = resp.json()
            api_calls += 1
        except requests.RequestException as e:
            print(f"  [ERROR] SerpAPI request failed (page {page_num + 1}): {e}")
            break

        raw_jobs = data.get("jobs_results", [])

        if not raw_jobs:
            # Log what we got back to help debug
            print(f"  [DEBUG] Response keys: {list(data.keys())}")
            if "error" in data:
                print(f"  [DEBUG] API error: {data['error']}")
            print(f"  [INFO] No more results at page {page_num + 1}. API calls: {api_calls}")
            break

        page_jobs = [_build_job_dict(item, query, location) for item in raw_jobs]

        # Smart stop: if all jobs on this page are already seen, stop
        if is_seen_fn and page_jobs:
            new_on_page = sum(
                1 for j in page_jobs
                if not is_seen_fn(j["title"], j["company"], j["location"])
            )
            if new_on_page == 0:
                print(f"  [INFO] Page {page_num + 1}: all {len(page_jobs)} jobs already seen. "
                      f"Stopping. API calls: {api_calls}")
                break
            else:
                print(f"  [INFO] Page {page_num + 1}: {new_on_page}/{len(page_jobs)} new jobs")

        all_jobs.extend(page_jobs)

        if len(all_jobs) >= max_results:
            break

        # Get next page token from serpapi_pagination
        pagination = data.get("serpapi_pagination", {})
        next_page_token = pagination.get("next_page_token")
        if not next_page_token:
            print(f"  [INFO] No more pages available. API calls: {api_calls}")
            break

    return all_jobs[:max_results]