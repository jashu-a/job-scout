"""
Multi-source job scraper.

Sources:
1. SerpAPI Google Jobs — best for US/global roles
2. LinkedIn via SerpAPI Google Search — searches site:linkedin.com/jobs
3. TokyoDev — direct scrape of their job listings (Japan-focused, English tech roles)
4. Indeed via SerpAPI Google Search — searches site:indeed.com or site:jp.indeed.com

Each source returns a standard job dict:
    { title, company, location, description, link, posted_at, source }
"""

import re
import requests
from typing import Optional, Callable
from bs4 import BeautifulSoup


SERP_API_URL = "https://serpapi.com/search.json"

DAYS_BACK_MAP = {
    1: "date_posted:today",
    3: "date_posted:3days",
    7: "date_posted:week",
    14: "date_posted:2weeks",
    30: "date_posted:month",
}

DAYS_BACK_GOOGLE_TBS = {
    1: "qdr:d",
    3: "qdr:d3",
    7: "qdr:w",
    14: "qdr:w2",
    30: "qdr:m",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: SerpAPI Google Jobs
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_google_jobs(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 10,
    is_seen_fn: Optional[Callable] = None,
) -> list[dict]:
    query = f"{seniority} {title}".strip() if seniority else title

    chip = DAYS_BACK_MAP.get(days_back, "date_posted:week")
    if days_back not in DAYS_BACK_MAP:
        for t in sorted(DAYS_BACK_MAP.keys()):
            if t >= days_back:
                chip = DAYS_BACK_MAP[t]
                break

    params = {
        "engine": "google_jobs",
        "q": query,
        "location": location,
        "chips": chip,
        "api_key": api_key,
        "hl": "en",
    }

    # Auto-detect country
    location_lower = location.lower()
    country_map = {
        "japan": "jp", "united states": "us", "usa": "us",
        "united kingdom": "gb", "uk": "gb", "canada": "ca",
        "germany": "de", "france": "fr", "australia": "au",
        "india": "in", "singapore": "sg", "remote": "us",
    }
    for key, code in country_map.items():
        if key in location_lower:
            params["gl"] = code
            break

    all_jobs = []
    next_page_token = None
    max_pages = (max_results + 9) // 10

    for page_num in range(max_pages):
        if next_page_token:
            params["next_page_token"] = next_page_token

        try:
            resp = requests.get(SERP_API_URL, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"  [GoogleJobs] Error {resp.status_code}: {resp.text[:200]}")
                break
            data = resp.json()
        except requests.RequestException as e:
            print(f"  [GoogleJobs] Request failed: {e}")
            break

        raw_jobs = data.get("jobs_results", [])
        if not raw_jobs:
            if "error" in data:
                print(f"  [GoogleJobs] No results: {data['error']}")
            break

        for item in raw_jobs:
            apply_links = item.get("apply_options", [])
            link = apply_links[0].get("link", "") if apply_links else ""
            if not link:
                link = item.get("share_link", "")

            all_jobs.append({
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "location": item.get("location", location),
                "description": item.get("description", ""),
                "link": link,
                "posted_at": item.get("detected_extensions", {}).get("posted_at", ""),
                "source": "Google Jobs",
            })

        if len(all_jobs) >= max_results:
            break

        pagination = data.get("serpapi_pagination", {})
        next_page_token = pagination.get("next_page_token")
        if not next_page_token:
            break

    return all_jobs[:max_results]


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: LinkedIn via SerpAPI Google Search
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_linkedin_via_google(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 10,
) -> list[dict]:
    query = f'site:linkedin.com/jobs/view {title} {location}'
    if seniority:
        query = f'site:linkedin.com/jobs/view {seniority} {title} {location}'

    tbs = DAYS_BACK_GOOGLE_TBS.get(days_back, "qdr:w")

    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": min(max_results, 10),
        "tbs": tbs,
        "hl": "en",
    }

    jobs = []
    try:
        resp = requests.get(SERP_API_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [LinkedIn] Error {resp.status_code}: {resp.text[:200]}")
            return jobs
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [LinkedIn] Request failed: {e}")
        return jobs

    for item in data.get("organic_results", [])[:max_results]:
        link = item.get("link", "")
        if "linkedin.com/jobs" not in link:
            continue

        raw_title = item.get("title", "")
        snippet = item.get("snippet", "")

        # Parse "Title - Company - Location" pattern from LinkedIn titles
        parts = raw_title.split(" - ")
        job_title = parts[0].strip() if parts else raw_title
        company = parts[1].strip() if len(parts) > 1 else ""
        job_location = parts[2].strip() if len(parts) > 2 else location

        # Clean up common LinkedIn suffixes
        job_title = re.sub(r'\s*\|.*$', '', job_title)

        jobs.append({
            "title": job_title,
            "company": company,
            "location": job_location,
            "description": snippet,
            "link": link,
            "posted_at": item.get("date", ""),
            "source": "LinkedIn",
        })

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 3: TokyoDev (direct scrape)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_tokyodev(title: str, max_results: int = 20) -> list[dict]:
    """Scrape TokyoDev job listings page."""
    url = "https://www.tokyodev.com/jobs"
    jobs = []

    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; JobScout/1.0)"
        })
        if resp.status_code != 200:
            print(f"  [TokyoDev] Error {resp.status_code}")
            return jobs
        
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # TokyoDev uses job card elements
        job_cards = soup.select("a[href*='/jobs/']")
        
        seen_links = set()
        title_lower = title.lower()
        
        for card in job_cards:
            link = card.get("href", "")
            if not link or link in seen_links:
                continue
            if not link.startswith("http"):
                link = f"https://www.tokyodev.com{link}"
            seen_links.add(link)
            
            # Get text content from the card
            card_text = card.get_text(separator=" | ", strip=True)
            
            # Skip navigation links and non-job links
            if len(card_text) < 10 or "/jobs/" not in link:
                continue
            
            # Basic keyword matching against the search title
            if title_lower and not any(
                kw in card_text.lower()
                for kw in title_lower.split()
            ):
                continue
            
            # Parse what we can from the card text
            parts = [p.strip() for p in card_text.split("|") if p.strip()]
            job_title = parts[0] if parts else card_text
            company = parts[1] if len(parts) > 1 else ""
            
            jobs.append({
                "title": job_title[:100],
                "company": company[:100],
                "location": "Tokyo, Japan",
                "description": card_text,
                "link": link,
                "posted_at": "",
                "source": "TokyoDev",
            })
            
            if len(jobs) >= max_results:
                break

    except Exception as e:
        print(f"  [TokyoDev] Scraping failed: {e}")

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 4: Indeed via SerpAPI Google Search
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_indeed_via_google(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 10,
) -> list[dict]:
    # Use jp.indeed.com for Japan, indeed.com for others
    if "japan" in location.lower() or "tokyo" in location.lower():
        site = "jp.indeed.com"
    else:
        site = "indeed.com"

    query = f'site:{site} {title} {location}'
    if seniority:
        query = f'site:{site} {seniority} {title} {location}'

    tbs = DAYS_BACK_GOOGLE_TBS.get(days_back, "qdr:w")

    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": min(max_results, 10),
        "tbs": tbs,
        "hl": "en",
    }

    jobs = []
    try:
        resp = requests.get(SERP_API_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [Indeed] Error {resp.status_code}: {resp.text[:200]}")
            return jobs
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [Indeed] Request failed: {e}")
        return jobs

    for item in data.get("organic_results", [])[:max_results]:
        link = item.get("link", "")
        if "indeed.com" not in link:
            continue

        raw_title = item.get("title", "")
        snippet = item.get("snippet", "")

        # Parse title - Indeed titles often have "Job Title - Company Name | Indeed"
        clean_title = re.sub(r'\s*[\|\-]\s*Indeed.*$', '', raw_title, flags=re.IGNORECASE)
        parts = clean_title.split(" - ", 1)
        job_title = parts[0].strip()
        company = parts[1].strip() if len(parts) > 1 else ""

        jobs.append({
            "title": job_title,
            "company": company,
            "location": location,
            "description": snippet,
            "link": link,
            "posted_at": item.get("date", ""),
            "source": "Indeed",
        })

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# UNIFIED SCRAPER — calls all enabled sources, auto-disables dead ones
# ═══════════════════════════════════════════════════════════════════════════════

# Track which regions return 0 from Google Jobs across combos in a single run
_google_jobs_dead_regions: set = set()


def scrape_jobs(
    api_key: str,
    title: str,
    location: str,
    seniority: str = "",
    days_back: int = 7,
    max_results: int = 30,
    is_seen_fn: Optional[Callable] = None,
    sources: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scrape jobs from multiple sources.

    Smart behavior:
    - If Google Jobs returns 0 results for a location, it is auto-disabled
      for all remaining combos using that same location in this run.
    - SerpAPI credits are only spent on sources that actually return results.

    Args:
        sources: List of enabled sources. Default: all.
                 Options: "google_jobs", "linkedin", "tokyodev", "indeed"
    """
    global _google_jobs_dead_regions

    if sources is None:
        sources = ["google_jobs", "linkedin", "tokyodev", "indeed"]

    all_jobs = []
    active_sources = [s for s in sources]  # copy
    per_source_limit = max(max_results // len(active_sources), 5)

    # Normalize location for region tracking
    region_key = location.strip().lower()

    # Auto-skip Google Jobs for regions that already returned 0
    if "google_jobs" in active_sources and region_key in _google_jobs_dead_regions:
        print(f"  ⏭️  Skipping Google Jobs (already returned 0 for '{location}')")
        active_sources.remove("google_jobs")

    if "google_jobs" in active_sources:
        print(f"  📡 Fetching from Google Jobs...")
        gj = scrape_google_jobs(api_key, title, location, seniority, days_back, per_source_limit, is_seen_fn)
        print(f"  [GoogleJobs] {len(gj)} results")
        if len(gj) == 0:
            _google_jobs_dead_regions.add(region_key)
            print(f"  ⚠️  Google Jobs returned 0 — disabling for '{location}' for remaining combos")
        all_jobs.extend(gj)

    if "linkedin" in active_sources:
        print(f"  📡 Fetching from LinkedIn...")
        li = scrape_linkedin_via_google(api_key, title, location, seniority, days_back, per_source_limit)
        print(f"  [LinkedIn] {len(li)} results")
        all_jobs.extend(li)

    if "tokyodev" in active_sources:
        # Only scrape TokyoDev for Japan-related searches
        if any(kw in location.lower() for kw in ["japan", "tokyo", "remote"]):
            print(f"  📡 Fetching from TokyoDev...")
            td = scrape_tokyodev(title, per_source_limit)
            print(f"  [TokyoDev] {len(td)} results")
            all_jobs.extend(td)

    if "indeed" in active_sources:
        print(f"  📡 Fetching from Indeed...")
        ind = scrape_indeed_via_google(api_key, title, location, seniority, days_back, per_source_limit)
        print(f"  [Indeed] {len(ind)} results")
        all_jobs.extend(ind)

    return all_jobs[:max_results]