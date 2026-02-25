"""
Multi-source job scraper.

Sources:
1. SerpAPI Google Jobs — US/global roles (skipped for dead regions)
2. LinkedIn — direct scrape via SerpAPI Google Search (global) or requests (Japan)
3. TokyoDev — direct HTML scrape (Japan English tech roles)
4. Indeed — direct scrape via SerpAPI Google Search (global) or requests (Japan)

Smart behaviors:
- Auto-disables Google Jobs for regions returning 0 results
- For Japan, uses direct scraping (no SerpAPI) for LinkedIn & Indeed
- Fetches full job descriptions when possible for better AI matching
"""

import re
import requests
from typing import Optional, Callable
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin


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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Regions where Google Jobs returned 0 (persists within a single run)
_google_jobs_dead_regions: set = set()


def _is_japan_search(location: str) -> bool:
    """Check if a search targets Japan."""
    return any(kw in location.lower() for kw in ["japan", "tokyo", "osaka", "kyoto", "yokohama", "nagoya", "fukuoka"])


def _fetch_page_text(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and extract visible text for job description enrichment."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove script/style
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:max_chars]
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: SerpAPI Google Jobs (US/global only)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_google_jobs(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 10,
    is_seen_fn: Optional[Callable] = None,
) -> list[dict]:
    query = f"{seniority} {title}".strip() if seniority else title

    chip = DAYS_BACK_MAP.get(days_back, "date_posted:week")

    params = {
        "engine": "google_jobs",
        "q": query,
        "location": location,
        "chips": chip,
        "api_key": api_key,
        "hl": "en",
    }

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
                "description": item.get("description", "")[:4000],
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
# SOURCE 2: LinkedIn
# For Japan: direct scrape (no SerpAPI)
# For other regions: SerpAPI Google Search
# ═══════════════════════════════════════════════════════════════════════════════

def _scrape_linkedin_direct(title: str, location: str, max_results: int = 15) -> list[dict]:
    """Scrape LinkedIn job search directly (no SerpAPI needed)."""
    jobs = []
    query = quote_plus(f"{title}")
    loc = quote_plus(location)
    url = f"https://www.linkedin.com/jobs/search?keywords={query}&location={loc}&f_TPR=r604800&position=1&pageNum=0"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [LinkedIn-Direct] HTTP {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # LinkedIn public job search uses these selectors
        cards = soup.select("div.base-card, li.result-card, div.job-search-card")

        if not cards:
            # Try alternative selectors
            cards = soup.select("[data-entity-urn]")

        for card in cards[:max_results]:
            title_el = card.select_one("h3, .base-search-card__title, .job-search-card__title")
            company_el = card.select_one("h4, .base-search-card__subtitle, .job-search-card__subtitle")
            location_el = card.select_one(".job-search-card__location, .base-search-card__metadata")
            link_el = card.select_one("a[href*='/jobs/view/'], a.base-card__full-link")

            job_title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            job_location = location_el.get_text(strip=True) if location_el else location
            link = link_el.get("href", "") if link_el else ""

            if not job_title:
                continue

            # Clean LinkedIn tracking params from URL
            if "?" in link:
                link = link.split("?")[0]

            # Try to fetch full description from the job page
            description = ""
            if link:
                description = _fetch_page_text(link, max_chars=4000)

            jobs.append({
                "title": job_title,
                "company": company,
                "location": job_location,
                "description": description or f"{job_title} at {company} in {job_location}",
                "link": link,
                "posted_at": "",
                "source": "LinkedIn",
            })

    except Exception as e:
        print(f"  [LinkedIn-Direct] Scraping failed: {e}")

    return jobs


def _scrape_linkedin_via_serpapi(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 10,
) -> list[dict]:
    """Scrape LinkedIn via SerpAPI Google Search."""
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
            print(f"  [LinkedIn-SerpAPI] Error {resp.status_code}: {resp.text[:200]}")
            return jobs
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [LinkedIn-SerpAPI] Request failed: {e}")
        return jobs

    for item in data.get("organic_results", [])[:max_results]:
        link = item.get("link", "")
        if "linkedin.com/jobs" not in link:
            continue

        raw_title = item.get("title", "")
        snippet = item.get("snippet", "")

        parts = raw_title.split(" - ")
        job_title = parts[0].strip() if parts else raw_title
        company = parts[1].strip() if len(parts) > 1 else ""
        job_location = parts[2].strip() if len(parts) > 2 else location

        job_title = re.sub(r'\s*\|.*$', '', job_title)

        # Fetch full job description for better AI matching
        description = _fetch_page_text(link, max_chars=4000) if link else ""

        jobs.append({
            "title": job_title,
            "company": company,
            "location": job_location,
            "description": description or snippet,
            "link": link,
            "posted_at": item.get("date", ""),
            "source": "LinkedIn",
        })

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 3: TokyoDev (direct scrape — Japan only)
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_tokyodev(title: str, max_results: int = 20) -> list[dict]:
    """Scrape TokyoDev job listings."""
    url = "https://www.tokyodev.com/jobs"
    jobs = []

    try:
        resp = requests.get(url, timeout=30, headers=HEADERS)
        if resp.status_code != 200:
            print(f"  [TokyoDev] Error {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        job_cards = soup.select("a[href*='/jobs/']")

        seen_links = set()
        title_lower = title.lower()
        title_keywords = [kw for kw in title_lower.split() if len(kw) > 2]

        for card in job_cards:
            link = card.get("href", "")
            if not link or link in seen_links:
                continue
            if not link.startswith("http"):
                link = f"https://www.tokyodev.com{link}"
            seen_links.add(link)

            card_text = card.get_text(separator=" | ", strip=True)

            if len(card_text) < 10:
                continue

            # Match against any keyword from the search title
            if title_keywords and not any(kw in card_text.lower() for kw in title_keywords):
                continue

            parts = [p.strip() for p in card_text.split("|") if p.strip()]
            job_title = parts[0] if parts else card_text
            company = parts[1] if len(parts) > 1 else ""

            # Fetch full description from job page
            description = _fetch_page_text(link, max_chars=4000)

            jobs.append({
                "title": job_title[:100],
                "company": company[:100],
                "location": "Tokyo, Japan",
                "description": description or card_text,
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
# SOURCE 4: Indeed
# For Japan: direct scrape of jp.indeed.com (no SerpAPI)
# For other regions: SerpAPI Google Search
# ═══════════════════════════════════════════════════════════════════════════════

def _scrape_indeed_direct(title: str, location: str, max_results: int = 15) -> list[dict]:
    """Scrape Indeed Japan directly (no SerpAPI)."""
    jobs = []

    if _is_japan_search(location):
        base_url = "https://jp.indeed.com"
    else:
        base_url = "https://www.indeed.com"

    query = quote_plus(title)
    loc = quote_plus(location)
    url = f"{base_url}/jobs?q={query}&l={loc}&fromage=7&limit={max_results}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [Indeed-Direct] HTTP {resp.status_code}")
            return jobs

        soup = BeautifulSoup(resp.text, "html.parser")

        # Indeed job cards
        cards = soup.select("div.job_seen_beacon, div.jobsearch-ResultsList > div, .result")

        for card in cards[:max_results]:
            title_el = card.select_one("h2 a, .jobTitle a, a.jcs-JobTitle")
            company_el = card.select_one("[data-testid='company-name'], .companyName, .company")
            location_el = card.select_one("[data-testid='text-location'], .companyLocation, .location")

            job_title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            job_location = location_el.get_text(strip=True) if location_el else location

            if not job_title:
                continue

            # Build link
            link = ""
            if title_el:
                href = title_el.get("href", "")
                if href:
                    link = urljoin(base_url, href)

            # Try to get description from job page
            description = ""
            if link:
                description = _fetch_page_text(link, max_chars=4000)

            jobs.append({
                "title": job_title,
                "company": company,
                "location": job_location,
                "description": description or f"{job_title} at {company}",
                "link": link,
                "posted_at": "",
                "source": "Indeed",
            })

    except Exception as e:
        print(f"  [Indeed-Direct] Scraping failed: {e}")

    return jobs


def _scrape_indeed_via_serpapi(
    api_key: str, title: str, location: str,
    seniority: str = "", days_back: int = 7, max_results: int = 10,
) -> list[dict]:
    """Scrape Indeed via SerpAPI Google Search."""
    site = "jp.indeed.com" if _is_japan_search(location) else "indeed.com"

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
            print(f"  [Indeed-SerpAPI] Error {resp.status_code}: {resp.text[:200]}")
            return jobs
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [Indeed-SerpAPI] Request failed: {e}")
        return jobs

    for item in data.get("organic_results", [])[:max_results]:
        link = item.get("link", "")
        if "indeed.com" not in link:
            continue

        raw_title = item.get("title", "")
        snippet = item.get("snippet", "")

        clean_title = re.sub(r'\s*[\|\-]\s*Indeed.*$', '', raw_title, flags=re.IGNORECASE)
        parts = clean_title.split(" - ", 1)
        job_title = parts[0].strip()
        company = parts[1].strip() if len(parts) > 1 else ""

        # Fetch full description
        description = _fetch_page_text(link, max_chars=4000) if link else ""

        jobs.append({
            "title": job_title,
            "company": company,
            "location": location,
            "description": description or snippet,
            "link": link,
            "posted_at": item.get("date", ""),
            "source": "Indeed",
        })

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# UNIFIED SCRAPER
# ═══════════════════════════════════════════════════════════════════════════════

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

    Smart behaviors:
    - Google Jobs auto-disabled for dead regions
    - Japan searches use direct scraping (saves SerpAPI credits)
    - Full descriptions fetched for better AI matching
    """
    global _google_jobs_dead_regions

    if sources is None:
        sources = ["google_jobs", "linkedin", "tokyodev", "indeed"]

    all_jobs = []
    japan_search = _is_japan_search(location)
    region_key = location.strip().lower()

    # Per-source limits — generous to hit 20-25 new jobs per run
    per_source_limit = max(max_results // max(len(sources) - 1, 1), 10)

    # ── Google Jobs ──
    if "google_jobs" in sources:
        if region_key in _google_jobs_dead_regions:
            print(f"  ⏭️  Skipping Google Jobs (returned 0 for '{location}' earlier)")
        else:
            print(f"  📡 Fetching from Google Jobs...")
            gj = scrape_google_jobs(api_key, title, location, seniority, days_back, per_source_limit, is_seen_fn)
            print(f"  [GoogleJobs] {len(gj)} results")
            if len(gj) == 0:
                _google_jobs_dead_regions.add(region_key)
                print(f"  ⚠️  Google Jobs returned 0 — disabled for '{location}'")
            all_jobs.extend(gj)

    # ── LinkedIn ──
    if "linkedin" in sources:
        if japan_search:
            print(f"  📡 Fetching from LinkedIn (direct scrape — no SerpAPI)...")
            li = _scrape_linkedin_direct(title, location, per_source_limit)
        else:
            print(f"  📡 Fetching from LinkedIn (via SerpAPI)...")
            li = _scrape_linkedin_via_serpapi(api_key, title, location, seniority, days_back, per_source_limit)
        print(f"  [LinkedIn] {len(li)} results")
        all_jobs.extend(li)

    # ── TokyoDev ──
    if "tokyodev" in sources:
        if japan_search or "remote" in location.lower():
            print(f"  📡 Fetching from TokyoDev (direct scrape — no SerpAPI)...")
            td = scrape_tokyodev(title, per_source_limit)
            print(f"  [TokyoDev] {len(td)} results")
            all_jobs.extend(td)

    # ── Indeed ──
    if "indeed" in sources:
        if japan_search:
            print(f"  📡 Fetching from Indeed Japan (direct scrape — no SerpAPI)...")
            ind = _scrape_indeed_direct(title, location, per_source_limit)
        else:
            print(f"  📡 Fetching from Indeed (via SerpAPI)...")
            ind = _scrape_indeed_via_serpapi(api_key, title, location, seniority, days_back, per_source_limit)
        print(f"  [Indeed] {len(ind)} results")
        all_jobs.extend(ind)

    print(f"  📊 Total from all sources: {len(all_jobs)}")
    return all_jobs[:max_results]