"""Command line entry point: ``stackcheck-mcp [--http] [--resolver-validation]``."""

import argparse
import ipaddress
import logging
import sys
from typing import cast

from stackcheck_mcp import __version__
from stackcheck_mcp.server import LogLevel, build_server
from stackcheck_mcp.stack import App, Settings

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stackcheck-mcp",
        description="MCP server: latest stable versions and cross-package compatibility.",
    )
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP, not stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback --host (no authentication: put a proxy in front)",
    )
    parser.add_argument(
        "--resolver-validation",
        action="store_true",
        help="also validate picks with real resolvers (uv pip compile, npm --package-lock-only)",
    )
    parser.add_argument(
        "--resolver-timeout", type=float, default=120.0, help="seconds per resolver run"
    )
    parser.add_argument(
        "--call-timeout",
        type=float,
        default=45.0,
        help="seconds of registry work per tool call before a partial answer",
    )
    parser.add_argument("--log-level", default="WARNING", type=str.upper, choices=LEVELS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.http and not args.allow_remote and not is_loopback(args.host):
        parser.error(
            f"--host {args.host} would expose an unauthenticated server beyond this machine; "
            "pass --allow-remote if an authenticating proxy sits in front of it"
        )
    return args


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:  # any other host name may resolve to a public address
        return False


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # stdout is the stdio transport: logs go to stderr only.
    logging.basicConfig(
        stream=sys.stderr, level=args.log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    settings = Settings(
        resolver_validation=args.resolver_validation,
        resolver_timeout=args.resolver_timeout,
        call_timeout=args.call_timeout,
    )
    server = build_server(App(settings), log_level=cast(LogLevel, args.log_level))
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
