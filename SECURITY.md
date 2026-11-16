# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on
[titouannnn/stackcheck-mcp](https://github.com/titouannnn/stackcheck-mcp/security/advisories/new).
Do not open a public issue. You should get an answer within a week.

Only the latest release is supported.

## Threat model

stackcheck-mcp reads public registries (npm, PyPI, Maven Central, endoflife.date) and answers
an MCP client. Things it does to stay safe, worth knowing when you assess a report:

- **No credentials.** It never sends any, and the opt-in resolvers are pointed at the public
  registries (`--registry`, `--default-index`, `--no-config`) whatever your `.npmrc` or
  `uv.toml` say.
- **Input validation.** Package names are validated before any URL or file path is built.
  Requests are limited to 100 entries per list, and each call to 45 s of registry work.
- **Subprocesses (only with `--resolver-validation`).** `uv` and `npm` run without a shell,
  with `--ignore-scripts`, in a temporary directory, with a timeout that kills the whole
  process tree. User input reaches them through stdin or a generated `package.json`, never
  the command line.
- **HTTP transport.** It has no authentication. It binds to loopback by default, where the
  MCP SDK applies DNS rebinding protection. A non-loopback `--host` requires
  `--allow-remote`, which you should only use behind an authenticating proxy.
- **Cache.** Only reduced JSON is stored, written atomically under the cache directory.
  Pruning only deletes `*.json` files in the server's own namespace folders.
- **stdout** carries the stdio transport. Logs go to stderr.
