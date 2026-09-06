"""V1 autonomous submission allowlist."""
from urllib.parse import urlsplit


def is_greenhouse_live_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https"
                and parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
                and parsed.username is None and parsed.password is None
                and parsed.port in {None, 443})
    except (ValueError, TypeError):
        return False
