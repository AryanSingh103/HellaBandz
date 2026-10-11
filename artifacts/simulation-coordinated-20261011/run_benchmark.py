"""Frozen full managed-runner contract, with deterministic local execution transport.
The independent browser run exercises the production HTTP/Postgres transport.
"""
import json
import resource
import time
from pathlib import Path

from hellabandz.ledger import replay
from hellabandz.trading import live
from hellabandz.trading.execution import LocalLedger
from hellabandz.trading.registry import Registry
from hellabandz.trading.research import research
from hellabandz.trading.reporting import source_hash

OUT = Path('/out')
DATA = Path('/app/data/snapshots/alpaca-sip-1hour-2023-01-03-2026-09-30')
OUT.mkdir(exist_ok=True, parents=True)
reg = Registry(OUT / 'registry.json')
start = time.perf_counter()
found = research(DATA, reg, 600, use_llm=True,
    progress=lambda stage, role, n: print(stage, role, n, flush=True))
(OUT / 'research.json').write_text(json.dumps(found, indent=1))
assert found['status'] == 'complete', found['status']
messages = []
class Response:
    def __init__(self, value):
        self.value = value
    def json(self):
        return self.value
class Client:
    def __init__(self, ledger):
        self.ledger = ledger
    def get(self, path):
        assert path == '/v1/verify'
        rebuilt = replay(self.ledger.events, verify_chain=True)
        return Response({'match': rebuilt.digest() == self.ledger.state.digest(), 'chain_ok': True,
                         'events': len(self.ledger.events)})
    def post(self, path, json):
        messages.append(json)
        return Response({'ok': True})
class Transport(LocalLedger):
    def __init__(self, api, pid, headers, run):
        super().__init__()
        self.pid, self.c, self.latency_ns, self.calls = pid, Client(self), [], 0
live.ApiLedger = Transport
live.registry_path = lambda dataset: OUT / 'registry.json'
live.MANIFESTS = OUT
last = 0
def progress(stage, value=None, total=None):
    global last
    if value is not None and (value >= last + 100 or value == total):
        print(stage, value, total, flush=True)
        last = value
result = live.run_coordinated({'config': {'kind': 'coordinated', 'steps': 10000, 'speed': 0},
    'portfolio_id': 'full-sip-coordinated', 'run_id': 'frozen-sip'}, DATA, 'local', {}, progress, lambda: False)
(OUT / 'messages.json').write_text(json.dumps(messages, indent=1))
result.update(total_elapsed_s=round(time.perf_counter() - start, 3),
              peak_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
              source_sha256=source_hash(), transport='local deterministic ledger; browser benchmark separately verifies API/Postgres/worker')
(OUT / 'benchmark.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
