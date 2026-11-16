"""Helper test runner to query stackcheck-mcp directly from python.

Usage:
    uv run python scripts/test_runner.py '{"npm": ["next", "react@19", "react-dom@19"]}'
"""

import asyncio
import json
import sys

from stackcheck_mcp import stack


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: test_runner.py '<json_spec>'", file=sys.stderr)
        sys.exit(1)

    spec = json.loads(sys.argv[1])
    app = stack.App(
        settings=stack.Settings(resolver_validation=spec.get("resolver_validation", False))
    )
    try:
        if "products" in spec:
            res = await stack.get_runtime_info(app, spec["products"])
        elif "explain" in spec:
            exp = spec["explain"]
            res = await stack.explain(app, exp["ecosystem"], exp["package"], exp.get("version", ""))
        else:
            res = await stack.check_stack(
                app,
                npm_specs=spec.get("npm", []),
                pypi_specs=spec.get("pypi", []),
                maven_specs=spec.get("maven", []),
                maven_bom=spec.get("maven_bom", ""),
                runtimes=spec.get("runtimes", []),
                targets=spec.get("targets", {}),
            )
        print(json.dumps(res, indent=2, ensure_ascii=False))
    finally:
        await app.aclose()


if __name__ == "__main__":
    asyncio.run(main())
