"""Run the reference stack (docs/SPEC.md §9) through ``check_stack``, live, and time it.

Cold = empty cache directory; warm = same directory, fresh process state (new client).
The call goes through the MCP layer (in-process client) like a real agent's would.

    uv run python scripts/reference_run.py [--resolver-validation]
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stackcheck_mcp.cache import Cache  # noqa: E402
from stackcheck_mcp.server import build_server  # noqa: E402
from stackcheck_mcp.stack import App, Settings  # noqa: E402
from tests.reference_stack import REFERENCE  # noqa: E402


async def timed_call(cache_dir: Path, validate: bool) -> tuple[float, str]:
    app = App(settings=Settings(resolver_validation=validate), cache=Cache(cache_dir))
    async with Client(build_server(app)) as client:
        start = time.perf_counter()
        result = await client.call_tool("check_stack", REFERENCE)
        elapsed = time.perf_counter() - start
    text = result.content[0].text  # type: ignore[union-attr]
    return elapsed, text


async def main() -> None:
    validate = "--resolver-validation" in sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        cold, text = await timed_call(Path(tmp), validate)
        warm_runs = [(await timed_call(Path(tmp), validate))[0] for _ in range(5)]
    print(json.dumps(json.loads(text), indent=2))
    print(f"\nresponse: {len(text)} bytes of compact JSON", file=sys.stderr)
    print(f"cold: {cold * 1000:.0f} ms", file=sys.stderr)
    print(f"warm: {', '.join(f'{w * 1000:.0f}' for w in warm_runs)} ms", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
