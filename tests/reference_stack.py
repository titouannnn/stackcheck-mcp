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
