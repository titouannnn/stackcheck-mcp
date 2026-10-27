"""Output contract of the MCP tools.

Every field that can be empty is ``NotRequired`` and is omitted rather than sent as
``null``/``[]``/``{}``. Changing anything here means updating the README schema and the
tests in the same commit.
"""

from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

from pydantic import ConfigDict, with_config

Ecosystem = Literal["npm", "pypi", "maven"]

IssueKind = Literal[
    "peer",  # npm peerDependencies between requested packages
    "requires",  # PyPI requires_dist between requested packages
    "engines",  # npm engines.node vs the Node target
    "python",  # requires_python vs the Python target
    "wheels",  # no wheel for the Python target
    "bom",  # Maven version differs from the BOM-managed one
    "java",  # Spring Boot Java baseline vs the Java target
    "deprecated",
    "yanked",
    "unsatisfiable",  # the user's own constraint matches no stable version
    "resolver",  # a real resolver rejected the set (--resolver-validation)
]

ValidationStatus = Literal["skipped", "ok", "failed", "unavailable", "timeout"]


class Issue(TypedDict):
    kind: IssueKind
    ecosystem: Ecosystem
    package: str
    required_by: NotRequired[str]
    range: NotRequired[str]
    target: NotRequired[str]
    suggestion: NotRequired[dict[str, str]]
    alternative: NotRequired[dict[str, str]]
    severity: NotRequired[Literal["warn"]]  # absent means error
    message: NotRequired[str]


NextLts = TypedDict("NextLts", {"version": str, "from": str})


class RuntimeInfo(TypedDict):
    latest: NotRequired[str]
    lts: NotRequired[str]
    next_lts: NotRequired[NextLts]
    maintained: NotRequired[list[str]]
    eol: NotRequired[str]


@with_config(ConfigDict(extra="allow"))  # unknown keys are reported, not dropped
class Targets(TypedDict, total=False):
    node: str
    python: str
    java: str


class StackResult(TypedDict):
    targets: NotRequired[dict[str, str]]
    npm: NotRequired[dict[str, str]]
    pypi: NotRequired[dict[str, str]]
    maven: NotRequired[dict[str, str]]
    runtimes: NotRequired[dict[str, RuntimeInfo]]
    issues: NotRequired[list[Issue]]
    errors: NotRequired[dict[str, dict[str, str]]]
    validation: NotRequired[dict[str, ValidationStatus]]
    stale: NotRequired[bool]


class RuntimeResult(TypedDict):
    runtimes: NotRequired[dict[str, RuntimeInfo]]
    errors: NotRequired[dict[str, str]]
    stale: NotRequired[bool]


class Explanation(TypedDict):
    ecosystem: Ecosystem
    package: str
    version: NotRequired[str]
    peerDependencies: NotRequired[dict[str, str]]
    optionalPeers: NotRequired[list[str]]
    engines: NotRequired[dict[str, str]]
    deprecated: NotRequired[str]
    requires_dist: NotRequired[list[str]]
    requires_python: NotRequired[str]
    yanked: NotRequired[str]
    parent: NotRequired[str]
    dependencies: NotRequired[list[str]]
    error: NotRequired[str]
    stale: NotRequired[bool]


@dataclass
class EcosystemResult:
    """Internal: what one ecosystem contributes to a ``check_stack`` answer."""

    picks: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # Inputs for --resolver-validation: exact pins and the user's original specs.
    pins: list[str] = field(default_factory=list)
    specs: list[str] = field(default_factory=list)
