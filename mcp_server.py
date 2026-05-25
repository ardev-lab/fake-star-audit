#!/usr/bin/env python3
"""
MCP (Model Context Protocol) server for fake-star-audit — OPTIONAL.

The core tool (audit.py) is zero-dependency and runs standalone. This wrapper
is only needed if you want to call the audit from an MCP client (Claude Desktop,
Cursor, etc.). It requires the `mcp` package:  pip install mcp

It runs over stdio: the MCP client launches it as a subprocess on the user's own
machine. It does not open a network server, read environment variables, or write
files of its own — it simply exposes audit.py's logic as the `audit_repo` tool.

Run (usually launched by the MCP client, not by hand):
    python3 mcp_server.py
"""

import re

from mcp.server.fastmcp import FastMCP

from audit import audit, AuditError, REPO_RE

mcp = FastMCP("fake-star-audit")


@mcp.tool()
def audit_repo(repo: str, timeout: int = 10) -> dict:
    """Audit a GitHub repository's stargazers for signs of fake-star injection.

    Inspects two windows of stargazers (oldest ~100 + newest 30) across five
    transparent, deterministic axes (burst, suffix-farm, sequential-id cluster,
    same-second cluster, inter-star gap regularity) plus extended signals, and
    returns a LOW / MEDIUM / HIGH risk verdict with per-axis evidence. Uses the
    anonymous GitHub API only (no token). Heuristic, not proof — read the evidence.

    Args:
        repo: GitHub repository as "owner/name" (e.g. "facebook/react").
        timeout: Per-request HTTP timeout in seconds (default 10).

    Returns:
        A dict with keys: risk_verdict, repo_metadata, axes (per-axis flag +
        evidence), extended_signals, evidence_summary, warnings. On failure,
        a dict with keys: error, message.
    """
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        return {"error": "invalid_input",
                "message": "repo must be 'owner/name' (alphanumerics, '.', '_', '-')"}
    try:
        return audit(repo, timeout=int(timeout))
    except AuditError as e:
        return {"error": e.code, "message": e.message}
    except Exception as e:  # never crash the MCP server on an unexpected error
        return {"error": "internal_error", "message": str(e)}


def main():
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
