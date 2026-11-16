"""Compare check_stack's latest-stable answers with registry truth for top packages.

The expected versions are computed here, independently of the server's code:

- npm: ``dist-tags.latest`` of the abbreviated document; when it is not a plain
  ``X.Y.Z``, the highest plain version.
- PyPI: the JSON API's per-version file lists (never filenames): the highest final
  release with at least one non-yanked file, else the highest one with files.
- Maven (a curated list of popular artifacts: Central publishes no download ranking): the
  most recently *published* stable version, from the timestamps of Central's search
  index. Ours may legitimately differ (a backport of an older line published last); it
  is ``suspicious`` when ours was published more than two years before that version,
  which is how build-date versions such as commons-collections ``20040616`` show up.
  The search index lags Central (by months in 2026): a pick it does not know yet is
  ``unindexed``, not an error.

A batch can race a publish (the server and the oracle fetch minutes apart): recheck any
mismatch before calling it a bug.

    uv run python scripts/sweep.py --npm 5000 --pypi 5000 --maven 70 --out sweep

Writes ``<out>/<ecosystem>.jsonl`` and ``<out>/summary.json``. Uses its own
cache directory (``<out>/cache``) unless STACKCHECK_CACHE_DIR is set.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

TOP_PYPI = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
TOP_NPM = "https://cdn.jsdelivr.net/npm/npm-high-impact@1.13.0/lib/top-download.js"
ABBREVIATED = {"Accept": "application/vnd.npm.install-v1+json"}
PLAIN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
CENTRAL_SEARCH = "https://search.maven.org/solrsearch/select"
MAVEN_PRERELEASE = frozenset(
    {"alpha", "beta", "rc", "cr", "milestone", "snapshot", "preview", "ea"}
)
TWO_YEARS_MS = 2 * 365 * 86400 * 1000
TOP_MAVEN = [
    "org.springframework.boot:spring-boot-starter-web",
    "org.springframework.boot:spring-boot-dependencies",
    "org.springframework:spring-core",
    "org.springframework:spring-context",
    "com.fasterxml.jackson.core:jackson-databind",
    "com.fasterxml.jackson.core:jackson-core",
    "com.google.guava:guava",
    "com.google.code.gson:gson",
    "com.google.protobuf:protobuf-java",
    "org.slf4j:slf4j-api",
    "ch.qos.logback:logback-classic",
    "org.apache.logging.log4j:log4j-core",
    "log4j:log4j",
    "junit:junit",
    "org.junit.jupiter:junit-jupiter",
    "org.mockito:mockito-core",
    "org.assertj:assertj-core",
    "org.hamcrest:hamcrest",
    "commons-collections:commons-collections",
    "org.apache.commons:commons-collections4",
    "commons-lang:commons-lang",
    "org.apache.commons:commons-lang3",
    "commons-io:commons-io",
    "commons-codec:commons-codec",
    "commons-logging:commons-logging",
    "commons-beanutils:commons-beanutils",
    "org.apache.commons:commons-text",
    "org.apache.httpcomponents:httpclient",
    "org.apache.httpcomponents.client5:httpclient5",
    "com.squareup.okhttp3:okhttp",
    "io.netty:netty-all",
    "io.netty:netty-handler",
    "org.hibernate.orm:hibernate-core",
    "org.hibernate:hibernate-core",
    "org.hibernate.validator:hibernate-validator",
    "jakarta.persistence:jakarta.persistence-api",
    "jakarta.servlet:jakarta.servlet-api",
    "javax.servlet:javax.servlet-api",
    "org.projectlombok:lombok",
    "org.postgresql:postgresql",
    "com.mysql:mysql-connector-j",
    "com.h2database:h2",
    "org.xerial:sqlite-jdbc",
    "com.zaxxer:HikariCP",
    "org.flywaydb:flyway-core",
    "org.liquibase:liquibase-core",
    "io.projectreactor:reactor-core",
    "io.reactivex.rxjava3:rxjava",
    "org.jetbrains.kotlin:kotlin-stdlib",
    "org.jetbrains.kotlinx:kotlinx-coroutines-core",
    "org.scala-lang:scala-library",
    "org.apache.kafka:kafka-clients",
    "software.amazon.awssdk:s3",
    "com.amazonaws:aws-java-sdk-s3",
    "io.micrometer:micrometer-core",
    "io.opentelemetry:opentelemetry-api",
    "org.eclipse.jetty:jetty-server",
    "org.apache.tomcat.embed:tomcat-embed-core",
    "io.grpc:grpc-netty",
    "org.yaml:snakeyaml",
    "org.apache.poi:poi",
    "org.jsoup:jsoup",
    "org.bouncycastle:bcprov-jdk18on",
    "io.jsonwebtoken:jjwt-api",
    "org.testcontainers:testcontainers",
    "org.seleniumhq.selenium:selenium-java",
    "io.rest-assured:rest-assured",
    "org.apache.maven.plugins:maven-compiler-plugin",
    "org.apache.maven.plugins:maven-surefire-plugin",
    "javax.annotation:javax.annotation-api",
]


def triple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


async def get(client: httpx2.AsyncClient, url: str, **kwargs: Any) -> httpx2.Response:
    """GET with exponential backoff; a 429 waits at least its Retry-After."""
    for attempt in range(6):
        delay = 2.0**attempt
        try:
            response = await client.get(url, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            if (after := response.headers.get("retry-after", "")).isdigit():
                delay = max(delay, float(after))
        except httpx2.TransportError:
            if attempt == 5:
                raise
        await asyncio.sleep(delay)
    return response


async def npm_truth(client: httpx2.AsyncClient, name: str) -> dict[str, Any]:
    # The abbreviated document (what `npm install` reads); `/<name>/latest` is throttled.
    url = f"https://registry.npmjs.org/{quote(name, safe='@')}"
    response = await get(client, url, headers=ABBREVIATED)
    if response.status_code == 404:
        return {"missing": True}
    response.raise_for_status()
    doc = response.json()
    if "versions" not in doc:  # unpublished: only the name and a timestamp remain
        return {"missing": True}
    latest = str(doc["dist-tags"]["latest"])
    expected: str | None = latest
    if not PLAIN.match(latest):
        # SPEC §3: a pre-release `latest` caps nothing (whatwg-url-without-unicode: 8.0.0-3).
        plain = (v for v in doc["versions"] if PLAIN.match(v))
        expected = max(plain, key=triple, default=None)
    deprecated = bool(expected and doc["versions"][expected].get("deprecated"))
    return {"expected": expected, "latest": latest, "deprecated": deprecated}


async def pypi_truth(client: httpx2.AsyncClient, name: str) -> dict[str, Any]:
    response = await get(client, f"https://pypi.org/pypi/{quote(name)}/json")
    if response.status_code == 404:
        return {"missing": True}
    response.raise_for_status()
    data = response.json()
    best: Version | None = None
    best_with_files: Version | None = None
    for raw, files in data["releases"].items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        if not files:
            continue
        if best_with_files is None or version > best_with_files:
            best_with_files = version
        if any(not f.get("yanked") for f in files) and (best is None or version > best):
            best = version
    # SPEC §3: an all-yanked release is still the answer when nothing else exists.
    expected = best or best_with_files
    return {
        "expected": str(expected) if expected else None,
        "yanked_latest": best_with_files is not None and best_with_files != best,
        "info_version": data["info"]["version"],
    }


def maven_stable(version: str) -> bool:
    """SPEC §3, implemented here independently of the server."""
    tokens = re.findall(r"[a-z]+|\d+", version.lower())
    for token, following in zip(tokens, [*tokens[1:], ""], strict=True):
        if token in MAVEN_PRERELEASE or (token in ("a", "b", "m") and following.isdigit()):
            return False
    return True


async def maven_truth(client: httpx2.AsyncClient, ga: str) -> dict[str, Any]:
    group, artifact = ga.split(":")
    published: dict[str, int] = {}
    start = 0
    while True:
        params = {
            "q": f'g:"{group}" AND a:"{artifact}"',
            "core": "gav",
            "rows": "200",
            "start": str(start),
            "wt": "json",
        }
        response = await get(client, CENTRAL_SEARCH, params=params)
        response.raise_for_status()
        found = response.json()["response"]
        published |= {doc["v"]: int(doc["timestamp"]) for doc in found["docs"]}
        start += 200
        if start >= int(found["numFound"]) or not found["docs"]:
            break
    stable = {v: ts for v, ts in published.items() if maven_stable(v)}
    if not stable:
        return {"missing": True}
    return {"expected": max(stable, key=lambda v: stable[v]), "published": stable}


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def issue_package(ecosystem: str, label: str) -> str:
    """Package of an issue label: ``name@v`` (npm), ``name[extras]==v``, ``g:a:v``."""
    if ecosystem == "npm":
        return label.rsplit("@", 1)[0] if label.rfind("@") > 0 else label
    if ecosystem == "maven":
        return label.rsplit(":", 1)[0] if label.count(":") == 2 else label
    return label.split("==")[0].split("[")[0]


async def server_answers(
    ecosystem: str, names: list[str], batch: int
) -> tuple[dict[str, dict[str, Any]], list[float]]:
    from stackcheck_mcp import stack

    app = stack.App(stack.Settings(resolver_validation=False))
    answers: dict[str, dict[str, Any]] = {}
    durations: list[float] = []
    try:
        for group in chunks(names, batch):
            started = time.perf_counter()
            out = await stack.check_stack(
                app,
                npm_specs=group if ecosystem == "npm" else [],
                pypi_specs=group if ecosystem == "pypi" else [],
                maven_specs=group if ecosystem == "maven" else [],
                maven_bom="",
                runtimes=[],
                targets={},
            )
            durations.append(time.perf_counter() - started)
            picks = out.get(ecosystem, {})
            errors = out.get("errors", {}).get(ecosystem, {})
            issues = out.get("issues", [])
            for name in group:
                key = canonicalize_name(name) if ecosystem == "pypi" else name
                version = picks.get(key)
                kinds = sorted(
                    {
                        i["kind"]
                        for i in issues
                        if i.get("ecosystem") == ecosystem
                        and issue_package(ecosystem, i["package"]) == key
                    }
                )
                answers[name] = {"ours": version, "error": errors.get(key), "kinds": kinds}
            done = len(answers)
            print(f"  {ecosystem}: {done}/{len(names)} ({durations[-1]:.1f}s)", file=sys.stderr)
    finally:
        await app.aclose()
    return answers, durations


async def truths(ecosystem: str, names: list[str]) -> dict[str, dict[str, Any]]:
    # Central's search index throttles hard: fewer parallel requests there.
    limit = asyncio.Semaphore(4 if ecosystem == "maven" else 16)
    oracle = {"npm": npm_truth, "pypi": pypi_truth, "maven": maven_truth}[ecosystem]

    async with httpx2.AsyncClient(timeout=60, follow_redirects=True) as client:

        async def one(name: str) -> tuple[str, dict[str, Any]]:
            async with limit:
                try:
                    return name, await oracle(client, name)
                except Exception as exc:  # recorded per package, never fatal
                    return name, {"oracle_error": f"{type(exc).__name__}: {exc}"}

        return dict(await asyncio.gather(*(one(n) for n in names)))


def classify(truth: dict[str, Any], answer: dict[str, Any]) -> str:
    if "oracle_error" in truth:
        return "oracle_error"
    ours, expected = answer["ours"], truth.get("expected")
    if truth.get("missing") or expected is None:
        return "agree_none" if ours is None else "extra"
    if ours is None:
        return "ours_error"
    if ours == expected:
        return "match"
    if "published" in truth:  # Maven: the oracle is the newest publication, not the highest
        ours_at, newest_at = truth["published"].get(ours), truth["published"][expected]
        if ours_at is None:
            return "unindexed"  # the search index lags Central by months: newer than it
        return "suspicious" if newest_at - ours_at > TWO_YEARS_MS else "backport"
    return "mismatch"


async def sweep(ecosystem: str, names: list[str], batch: int, out: Path) -> dict[str, Any]:
    started = time.perf_counter()
    # One after the other, so the oracle's traffic neither throttles nor slows the server.
    answers, durations = await server_answers(ecosystem, names, batch)
    truth_map = await truths(ecosystem, names)
    counts: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    with (out / f"{ecosystem}.jsonl").open("w", encoding="utf-8") as handle:
        for rank, name in enumerate(names, 1):
            truth, answer = truth_map[name], answers[name]
            status = classify(truth, answer)
            counts[status] += 1
            if truth.get("deprecated") and "deprecated" not in answer["kinds"]:
                flags["deprecated_not_flagged"] += 1
            if truth.get("yanked_latest") and "yanked" not in answer["kinds"]:
                flags["yanked_not_flagged"] += 1
            truth = {k: v for k, v in truth.items() if k != "published"}  # small records
            record = {"rank": rank, "name": name, "status": status, **truth, **answer}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {
        "packages": len(names),
        "status": dict(counts),
        "flags": dict(flags),
        "batch_size": batch,
        "batch_seconds": {
            "max": round(max(durations), 2),
            "mean": round(sum(durations) / len(durations), 2),
        },
        "total_seconds": round(time.perf_counter() - started, 1),
    }


async def top_lists(npm: int, pypi: int) -> tuple[list[str], list[str]]:
    async with httpx2.AsyncClient(timeout=60, follow_redirects=True) as client:
        npm_names: list[str] = []
        pypi_names: list[str] = []
        if npm:
            text = (await get(client, TOP_NPM)).text
            npm_names = re.findall(r"'([^']+)'", text)[:npm]
        if pypi:
            rows = (await get(client, TOP_PYPI)).json()["rows"]
            pypi_names = [row["project"] for row in rows][:pypi]
    return npm_names, pypi_names


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--npm", type=int, default=1000)
    parser.add_argument("--pypi", type=int, default=1000)
    parser.add_argument("--maven", type=int, default=len(TOP_MAVEN), help="curated list size")
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("sweep"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("STACKCHECK_CACHE_DIR", str((args.out / "cache").resolve()))
    npm_names, pypi_names = await top_lists(args.npm, args.pypi)
    summary: dict[str, Any] = {"date": time.strftime("%Y-%m-%d")}
    lists = (("npm", npm_names), ("pypi", pypi_names), ("maven", TOP_MAVEN[: args.maven]))
    for ecosystem, names in lists:
        if names:
            summary[ecosystem] = await sweep(ecosystem, names, args.batch, args.out)
            print(json.dumps({ecosystem: summary[ecosystem]}), file=sys.stderr)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
