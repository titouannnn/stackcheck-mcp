"""MCP server and tool definitions (thin layer over ``stack``).

Tool descriptions are loaded into every client's context: keep them short. Results are
returned as compact JSON text plus validated ``structuredContent``.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from stackcheck_mcp import __version__, stack
from stackcheck_mcp.models import Ecosystem, Explanation, RuntimeResult, StackResult, Targets

INSTRUCTIONS = (
    "Before pinning or upgrading dependencies, call check_stack once with the whole stack "
    "and use the versions it returns. Each issue explains an incompatibility with an "
    "indicative fix (suggestion/alternative); issues without severity are errors."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def compact(result: Any) -> CallToolResult:
    text = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result)


def build_server(app: stack.App, log_level: LogLevel = "WARNING") -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await app.aclose()

    server: MCPServer = MCPServer(
        "stackcheck-mcp",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        log_level=log_level,
    )

    @server.tool(annotations=READ_ONLY)
    async def check_stack(
        npm: Annotated[list[str], Field(default_factory=list, description="name or name@range")],
        pypi: Annotated[list[str], Field(default_factory=list, description="PEP 508, extras ok")],
        maven: Annotated[
            list[str], Field(default_factory=list, description="group:artifact[:version|range]")
        ],
        maven_bom: Annotated[str, Field(default="", description="BOM group:artifact[:version]")],
        runtimes: Annotated[list[str], Field(default_factory=list, description="endoflife slugs")],
        targets: Annotated[
            Targets,
            Field(
                default_factory=dict,
                description="cycle or version; default Node LTS, newest Python, Java LTS",
            ),
        ],
    ) -> Annotated[CallToolResult, StackResult]:
        """Latest stable versions plus incompatibilities between the packages and their shared
        deps (peer deps, requires_dist, engines, requires_python, wheels, BOM). Example:
        {"npm": ["react", "typescript", "typescript-eslint", "vite@^8"], "pypi": ["fastapi"]}
        """
        try:
            result = await stack.check_stack(
                app,
                npm_specs=npm,
                pypi_specs=pypi,
                maven_specs=maven,
                maven_bom=maven_bom,
                runtimes=runtimes,
                targets=targets,
            )
        except stack.InputError as exc:
            raise ToolError(str(exc)) from None
        return compact(result)

    @server.tool(annotations=READ_ONLY)
    async def get_runtime_info(
        products: Annotated[list[str], Field(description="endoflife.date slugs")],
    ) -> Annotated[CallToolResult, RuntimeResult]:
        """Latest, LTS (lts_eol: true if that LTS is end-of-life), next LTS, maintained cycles
        and EOL date of the recommended cycle, from endoflife.date. Example:
        {"products": ["nodejs", "python", "eclipse-temurin"]}
        """
        try:
            result = await stack.get_runtime_info(app, products)
        except stack.InputError as exc:
            raise ToolError(str(exc)) from None
        return compact(result)

    @server.tool(annotations=READ_ONLY)
    async def explain_issue(
        ecosystem: Ecosystem,
        package: Annotated[str, Field(description="npm name, PyPI name[extras], group:artifact")],
        version: Annotated[str, Field(default="", description="default: latest stable")],
    ) -> Annotated[CallToolResult, Explanation]:
        """Declared peer deps/engines (npm), requires_dist/requires_python (PyPI) or parent and
        dependencies (Maven) of one version, to dig into an issue. Example:
        {"ecosystem": "npm", "package": "typescript-eslint", "version": "8.72.1"}
        """
        try:
            result = await stack.explain(app, ecosystem, package, version)
        except stack.InputError as exc:
            raise ToolError(str(exc)) from None
        return compact(result)

    return server
