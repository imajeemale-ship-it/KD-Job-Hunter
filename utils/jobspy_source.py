"""
JobSpy integration — Broad keyword-based job search across multiple platforms.
Uses python-jobspy to search Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google.
"""

import hashlib
import math


def _clean(val, fallback=""):
    """Sanitize a pandas value — convert NaN/None to fallback string."""
    if val is None:
        return fallback
    if isinstance(val, float) and math.isnan(val):
        return fallback
    s = str(val).strip()
    if s.lower() in ("nan", "none", ""):
        return fallback
    return s


def _company_from_row(row) -> str:
    """Support both current and older python-jobspy company column names."""
    company = _clean(row.get("company_name"))
    if not company:
        company = _clean(row.get("company"))
    return company or "Unknown"


def _passes_prefilter(title: str, description: str, search_config: dict) -> bool:
    """Cheap deterministic filtering before any LLM scoring call."""
    title_lower = title.lower()
    combined = f"{title} {description}".lower()

    excluded = [
        str(term).strip().lower()
        for term in search_config.get("exclude_title_terms", [])
        if str(term).strip()
    ]
    if any(term in title_lower for term in excluded):
        return False

    required_any = [
        str(term).strip().lower()
        for term in search_config.get("required_any_terms", [])
        if str(term).strip()
    ]
    if required_any and not any(term in combined for term in required_any):
        return False

    return True


def discover_jobspy_jobs(profile: dict) -> list:
    """
    Search for jobs using python-jobspy across multiple job boards.
    Each source is independently optional — if one fails, others continue.
    """
    from utils.discovery import Job

    search_config = profile.get("search", {})
    queries = search_config.get("queries", profile["preferences"].get("roles", []))
    locations = search_config.get("locations", profile["preferences"].get("locations", ["Remote"]))
    distance = search_config.get("distance_miles", 100)
    results_wanted = search_config.get("results_per_query", 50)

    # Always include Remote unless already configured.
    if not any("remote" in str(loc).lower() for loc in locations):
        locations = locations + ["Remote"]

    all_jobs = []

    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  ⚠ python-jobspy not installed. Run: pip install python-jobspy")
        return []

    sites = search_config.get(
        "jobspy_sites",
        ["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"],
    )

    for query in queries:
        for location in locations:
            print(f"  🔍 Searching: '{query}' in '{location}'...")
            try:
                results = scrape_jobs(
                    site_name=sites,
                    search_term=query,
                    location=location,
                    distance=distance,
                    results_wanted=results_wanted,
                    country_indeed="USA",
                    is_remote=profile["preferences"].get("remote_only", False),
                )

                if results is None or len(results) == 0:
                    continue

                accepted = 0
                for _, row in results.iterrows():
                    try:
                        title = _clean(row.get("title"), "Untitled")
                        company = _company_from_row(row)
                        job_location = _clean(row.get("location"), location)
                        job_url = _clean(row.get("job_url"))
                        description = _clean(row.get("description"))
                        site = _clean(row.get("site"), "jobspy")
                        date_posted = _clean(row.get("date_posted"))

                        if not job_url or not job_url.startswith("http"):
                            continue
                        if company == "Unknown" and not description:
                            continue
                        if title == "Untitled":
                            continue
                        if not _passes_prefilter(title, description, search_config):
                            continue

                        job_id = hashlib.md5(
                            (job_url or f"{title}_{company}").encode()
                        ).hexdigest()[:16]

                        salary_min = None
                        salary_max = None
                        try:
                            raw_min = row.get("min_amount")
                            raw_max = row.get("max_amount")
                            if raw_min is not None and not (
                                isinstance(raw_min, float) and math.isnan(raw_min)
                            ):
                                salary_min = int(raw_min)
                            if raw_max is not None and not (
                                isinstance(raw_max, float) and math.isnan(raw_max)
                            ):
                                salary_max = int(raw_max)
                        except (ValueError, TypeError):
                            pass

                        job = Job(
                            id=f"jobspy_{job_id}",
                            title=title,
                            company=company,
                            location=job_location,
                            url=job_url,
                            apply_url=job_url,
                            platform=f"jobspy_{site}",
                            description=description[:5000],
                            department="",
                            metadata={
                                "source": site,
                                "date_posted": date_posted,
                                "salary_min": salary_min,
                                "salary_max": salary_max,
                            },
                        )
                        all_jobs.append(job)
                        accepted += 1
                    except Exception:
                        continue

                print(
                    f"    Raw {len(results)} → kept {accepted} after prefilter "
                    f"for '{query}' in '{location}'"
                )

            except Exception as e:
                print(f"    ⚠ Search failed for '{query}' in '{location}': {e}")
                continue

    print(f"  📊 JobSpy total after prefilter: {len(all_jobs)} jobs found")
    return all_jobs
