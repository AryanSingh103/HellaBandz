"""Headers for our own API only; never forward this key to external providers."""
import os
from hellabandz.config import load_env


def api_headers() -> dict[str, str]:
    load_env()
    token = os.getenv("QS_API_TOKEN", "")
    return {"Authorization": "Bearer " + token} if token else {}
