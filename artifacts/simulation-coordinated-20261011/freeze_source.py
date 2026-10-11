from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

out = Path('artifacts/simulation-coordinated-20261011')
target = out / 'source-frozen'
if target.exists():
    raise SystemExit('Refusing to overwrite frozen source')
files = []
for directory, pattern in [('hellabandz', '*.py'), ('hellabandz', '*.sql'), ('tests', '*.py'), ('apps/web/app', '*.ts'), ('apps/web/app', '*.tsx')]:
    files += [p for p in Path(directory).rglob(pattern) if '__pycache__' not in p.parts]
files += [Path(x) for x in ('pyproject.toml', 'uv.lock', 'docker-compose.yml')]
files += list(out.glob('*.py')) + [out / 'BENCHMARK_PLAN.md']
hashes = {}
for p in sorted(files):
    destination = target / p
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, destination)
    hashes[p.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
(out / 'source-frozen-sha256.json').write_text(json.dumps({'frozen_at_utc': datetime.now(timezone.utc).isoformat(), 'files': hashes}, indent=1))
print('Frozen', len(files), 'files')
