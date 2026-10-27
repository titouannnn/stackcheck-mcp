"""The reference stack of docs/SPEC.md §9, shared by tests and the fixture recorder."""

NPM = [
    "react",
    "react-dom",
    "@types/react",
    "@types/react-dom",
    "typescript",
    "typescript-eslint",
    "eslint",
    "vite",
    "@vitejs/plugin-react",
    "vitest",
]
PYPI = [
    "fastapi",
    "pydantic",
    "uvicorn[standard]",
    "sqlalchemy",
    "pytest",
    "playwright",
    "ruff",
    "mypy",
]
RUNTIMES = ["nodejs", "python", "sqlite"]

REFERENCE = {"npm": NPM, "pypi": PYPI, "runtimes": RUNTIMES}

# Extra packages recorded for edge-case tests.
NPM_EXTRA = ["request", "@types/node"]
PYPI_EXTRA = ["pydantic-core", "greenlet", "httptools", "uvloop"]
PYPI_SCENARIOS = [
    PYPI + PYPI_EXTRA,
    ["fastapi", "pydantic<2.13"],
    ["pydantic", "pydantic-core<2.50"],  # impossible pin: pydantic requires ==2.50.1
    ["pydantic", "pydantic-core<2.47"],  # conflict with an older-pydantic alternative
]
MAVEN = [
    "org.springframework.boot:spring-boot-starter-web",
    "com.fasterxml.jackson.core:jackson-databind",
]
MAVEN_BOM = "org.springframework.boot:spring-boot-dependencies"
