"""Commit tournament manifest hashes to Solana Devnet and verify them (I-10).

The commitment is a single SPL Memo instruction carrying
`quantswarm:v1:<run_id>:<sha256(canonical manifest)>`. Verification recomputes
the hash from the local manifest file and compares it with the memo stored
on-chain, so editing any byte of the manifest makes verification fail.

    uv run python -m quantswarm.audit.solana commit data/manifests/<run>.json
    uv run python -m quantswarm.audit.solana verify data/manifests/<run>.json

Devnet only; no real funds. The fee payer keypair lives in data/solana/ (gitignored).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

from quantswarm.config import load_env
from quantswarm.ledger import canonical, sha256_hex

load_env()
ROOT = Path(__file__).resolve().parents[2]
RPC = os.environ.get("SOLANA_RPC", "https://api.devnet.solana.com")
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
KEYPAIR = ROOT / "data/solana/devnet-keypair.json"
DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"


def require_devnet():
    if rpc("getGenesisHash", []) != DEVNET_GENESIS:
        raise RuntimeError("Refusing non-Devnet RPC: test SOL only")


def manifest_hash_file(path: Path) -> tuple[str, dict]:
    m = json.loads(path.read_text())
    return sha256_hex(canonical(m)), m


def memo_for(run_id: str, h: str) -> str:
    return f"quantswarm:v1:{run_id}:{h}"


def rpc(method: str, params: list) -> dict:
    r = httpx.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"{method}: {j['error']}")
    return j["result"]


def load_keypair():
    from solders.keypair import Keypair

    if KEYPAIR.exists():
        return Keypair.from_bytes(bytes(json.loads(KEYPAIR.read_text())))
    kp = Keypair()
    KEYPAIR.parent.mkdir(parents=True, exist_ok=True)
    KEYPAIR.write_text(json.dumps(list(bytes(kp))))
    return kp


def ensure_funded(kp, min_lamports: int = 1_000_000) -> int:
    bal = rpc("getBalance", [str(kp.pubkey())])["value"]
    if bal >= min_lamports:
        return bal
    try:
        sig = rpc("requestAirdrop", [str(kp.pubkey()), 100_000_000])
        for _ in range(30):
            st = rpc("getSignatureStatuses", [[sig]])["value"][0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                break
            time.sleep(1)
    except Exception as e:
        raise RuntimeError(f"Devnet airdrop failed ({e}). Fund {kp.pubkey()} with test SOL at https://faucet.solana.com "
                         "and re-run.")
    return rpc("getBalance", [str(kp.pubkey())])["value"]


def send_memo(memo: str) -> str:
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    require_devnet()
    kp = load_keypair()
    ensure_funded(kp)
    bh = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    ix = Instruction(Pubkey.from_string(MEMO_PROGRAM), memo.encode(), [AccountMeta(kp.pubkey(), True, True)])
    msg = Message.new_with_blockhash([ix], kp.pubkey(), Hash.from_string(bh))
    tx = Transaction([kp], msg, Hash.from_string(bh))
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
    for _ in range(60):
        st = rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err"):
                raise RuntimeError(f"transaction failed: {st['err']}")
            return sig
        time.sleep(1)
    raise TimeoutError(f"transaction {sig} not confirmed")


def onchain_memo(sig: str) -> str | None:
    for _ in range(20):
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                          "commitment": "confirmed"}])
        if tx:
            if tx.get("meta", {}).get("err"):
                return None
            for ix in tx["transaction"]["message"]["instructions"]:
                if ix.get("programId") == MEMO_PROGRAM:
                    return ix.get("parsed")
            return None
        time.sleep(1)
    return None


def explorer(sig: str) -> str:
    return f"https://explorer.solana.com/tx/{sig}?cluster=devnet"


def commit(path: Path, api: str | None = None) -> dict:
    h, m = manifest_hash_file(path)
    memo = memo_for(m["run_id"], h)
    record_path = path.with_suffix(".solana.json")
    previous = json.loads(record_path.read_text()) if record_path.exists() else {}
    # Reuse a confirmed matching receipt if publishing to the API previously failed.
    sig = previous.get("signature") if previous.get("manifest_sha256") == h else None
    if not sig or onchain_memo(sig) != memo:
        sig = send_memo(memo)
    if onchain_memo(sig) != memo:
        raise RuntimeError("confirmed transaction does not contain the expected manifest memo")
    rec = {"run_id": m["run_id"], "manifest_sha256": h, "memo": memo, "signature": sig, "cluster": "devnet",
           "explorer": explorer(sig)}
    record_path.write_text(json.dumps(rec, indent=1))
    if api:
        from quantswarm.api.client import api_headers
        response = httpx.post(f"{api}/v1/tournaments", json={"run_id": m["run_id"], "manifest": m, "manifest_hash": h,
                                                           "solana_sig": sig}, timeout=90, headers=api_headers())
        response.raise_for_status()
    return rec


def verify(path: Path, sig: str | None = None, offline: bool = False) -> dict:
    h, m = manifest_hash_file(path)
    rec_path = path.with_suffix(".solana.json")
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    sig = sig or rec.get("signature")
    expected = memo_for(m["run_id"], h)
    out = {"run_id": m["run_id"], "recomputed_sha256": h, "signature": sig}
    if offline or not sig:
        out["committed_sha256"] = rec.get("manifest_sha256")
        out["ok"] = rec.get("manifest_sha256") == h
        out["source"] = "local commitment record"
        return out
    memo = onchain_memo(sig)
    out.update({"onchain_memo": memo, "ok": memo == expected, "source": "solana devnet", "explorer": explorer(sig)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("commit")
    c.add_argument("manifest")
    c.add_argument("--api", default=os.environ.get("QS_API"))
    v = sub.add_parser("verify")
    v.add_argument("manifest")
    v.add_argument("--sig")
    v.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.cmd == "commit":
        print(json.dumps(commit(Path(a.manifest), a.api), indent=1))
    else:
        r = verify(Path(a.manifest), a.sig, a.offline)
        print(json.dumps(r, indent=1))
        sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
