"""Create/reuse a local Devnet wallet and pin its public identity; never print key bytes."""
from pathlib import Path
from hellabandz.audit.solana import load_keypair

path = Path(__file__).resolve().parents[1] / ".env"
lines = path.read_text().splitlines() if path.exists() else []
public = str(load_keypair().pubkey())
configured = next((line.partition("=")[2].strip() for line in lines if line.startswith("QS_AUDIT_SIGNER=")), "")
if configured and configured != public:
    raise SystemExit("Pinned QS_AUDIT_SIGNER differs from local wallet; resolve explicitly before signing")
lines = [line for line in lines if not line.startswith("QS_AUDIT_SIGNER=")]
lines.append("QS_AUDIT_SIGNER=" + public)
path.write_text("\n".join(lines) + "\n")
print("Pinned Devnet public wallet:", public)
print("Use free test SOL from https://faucet.solana.com; never send mainnet funds.")
