import requests


def session_with_retry() -> requests.Session:
    """A requests session with retry logic to HTTP requests."""
    session = requests.Session()
    retry = requests.adapters.Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=[
            "DELETE",
            "GET",
            "HEAD",
            "OPTIONS",
            "PUT",
            "TRACE",
            "POST"
        ]
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
