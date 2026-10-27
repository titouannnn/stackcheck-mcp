"""Re-record the offline test fixtures from the live registries.

Fixtures are a snapshot of the reduced on-disk cache (``tests/fixtures/cache``), written
by running the real fetchers, so tests exercise the same code path as production. Each
entry records its ``fetched_at`` date. Usage::

    uv run python scripts/record_fixtures.py          # refresh everything
"""

import asyncio
import shutil
import sys
from pathlib import Path

from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.http import HttpClient
from stackcheck_mcp.maven import check_maven
from stackcheck_mcp.npm import check_npm
from stackcheck_mcp.pypi import check_pypi
from stackcheck_mcp.runtimes import fetch_product_list, runtime_infos, utc_today

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "cache"
sys.path.insert(0, str(ROOT))

from tests import reference_stack as ref  # noqa: E402

RUNTIMES = ["nodejs", "python", "eclipse-temurin", "sqlite", "spring-boot"]


async def record() -> None:
    http = HttpClient()
    try:
        fetcher = Fetcher(http, Cache(FIXTURES, ttl=10**9))
        today = utc_today()
        await fetch_product_list(fetcher)
        infos, errors = await runtime_infos(fetcher, RUNTIMES, today)
        if errors:
            sys.exit(f"runtime errors: {errors}")
        print(f"runtimes: {sorted(infos)}")
        npm = await check_npm(fetcher, ref.NPM + ref.NPM_EXTRA, None)
        if npm.errors:
            sys.exit(f"npm errors: {npm.errors}")
        print(f"npm: {npm.picks}")
        for specs in ref.PYPI_SCENARIOS:
            pypi = await check_pypi(fetcher, specs, "3.15.2")
            if pypi.errors:
                sys.exit(f"pypi errors: {pypi.errors}")
            print(f"pypi: {pypi.picks} issues={[i['kind'] for i in pypi.issues]}")
        for java in ("25.0.5+8", "11.0.29+7"):
            mvn = await check_maven(fetcher, ref.MAVEN, ref.MAVEN_BOM, java)
            if mvn.errors:
                sys.exit(f"maven errors: {mvn.errors}")
            print(f"maven: {mvn.picks} issues={mvn.issues}")
    finally:
        await http.aclose()


def main() -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    asyncio.run(record())
    size = sum(p.stat().st_size for p in FIXTURES.rglob("*.json"))
    count = sum(1 for _ in FIXTURES.rglob("*.json"))
    print(f"{count} fixture files, {size / 1024:.0f} KiB in {FIXTURES.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
