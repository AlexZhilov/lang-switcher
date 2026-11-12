#!/usr/bin/env python3
"""Query PyPI's public version advisories; send only package names/versions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def check(requirement):
    name, version = requirement.split('==')
    url = f'https://pypi.org/pypi/{quote(name)}/{quote(version)}/json'
    request = Request(url, headers={'User-Agent': 'lang-switcher-security-review'})
    with urlopen(request, timeout=20) as response:
        data = json.load(response)
    findings = [item for item in data.get('vulnerabilities', []) if not item.get('withdrawn')]
    return {'name': name, 'version': version, 'vulnerabilities': findings, 'url': url}


def main():
    requirements = [line.strip() for line in (ROOT / 'requirements.txt').read_text().splitlines()
                    if line.strip() and not line.startswith('#')]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check, requirements))
    (ROOT / '.build').mkdir(exist_ok=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'packages': results}
    (ROOT / '.build/dependency-audit.json').write_text(json.dumps(report, indent=2))
    for result in results:
        print(f'{result["name"]}=={result["version"]}: {len(result["vulnerabilities"])} known advisories')
    return 1 if any(result['vulnerabilities'] for result in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
