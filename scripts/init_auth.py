"""Generate a local operator key without printing it or replacing an existing key."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
path = root / ".env"
text = path.read_text() if path.exists() else (root / ".env.example").read_text()
lines = text.splitlines()
if not any(line.startswith("QS_API_TOKEN=") and len(line.partition("=")[2].strip()) >= 32 for line in lines):
    lines = [line for line in lines if not line.startswith("QS_API_TOKEN=")]
    lines.append("QS_API_TOKEN=" + secrets.token_urlsafe(48))
if not any(line.startswith("QS_AUTH_REQUIRED=") for line in lines):
    lines.append("QS_AUTH_REQUIRED=1")
path.write_text("\n".join(lines) + "\n")
print("Operator access key configured in ignored .env (QS_API_TOKEN); never put it in NEXT_PUBLIC settings.")
