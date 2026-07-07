"""Runtime MCP server registry (the `/mcp` chat command's storage).

Deployment configs (`mcp_servers:` yaml or `.mcp.json`) define MCP servers at
deploy time; this registry lets users connect additional servers from the chat
UI at runtime, Claude-style, without redeploying. Entries live in the
`mcp_runtime_servers` Postgres table and are merged into the config-defined
set by `get_effective_mcp_servers` (config wins on name collision, so a chat
user can never shadow an operator-managed server).

Runtime entries are restricted to HTTP transports (`streamable_http`, `sse`).
stdio servers stay config-only: their packages are pip-installed into the
chatbot image at build time, and accepting arbitrary commands from the chat UI
would be remote code execution in the container.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

from src.utils.logging import get_logger
from src.utils.mcp_json import _normalize_server

logger = get_logger(__name__)

__all__ = [
    "McpServerValidationError",
    "McpRuntimeServer",
    "McpRuntimeServerRegistry",
]

_ALLOWED_RUNTIME_TRANSPORTS = ("streamable_http", "sse")
# Same shape Claude Code accepts for server names; \Z so trailing newlines fail.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_MAX_NAME_CHARS = 64
_MAX_URL_CHARS = 2048
_MAX_HEADERS = 16
_MAX_HEADER_CHARS = 1024

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mcp_runtime_servers (
    name        TEXT PRIMARY KEY,
    config      JSONB NOT NULL,
    added_by    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class McpServerValidationError(ValueError):
    """Raised when a runtime MCP server definition is rejected."""


@dataclass
class McpRuntimeServer:
    name: str
    config: Dict[str, Any]
    added_by: Optional[str] = None
    created_at: Optional[str] = None


def validate_runtime_server(name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Claude-style entry and enforce the runtime-add restrictions.

    Returns the normalized internal config (transport form). Raises
    McpServerValidationError with a user-presentable message otherwise.
    """
    if not name or len(name) > _MAX_NAME_CHARS or not _NAME_RE.match(name):
        raise McpServerValidationError(
            "Server name must use letters, numbers, hyphens, or underscores "
            f"(max {_MAX_NAME_CHARS} characters)"
        )
    try:
        cfg = _normalize_server(name, entry)
    except ValueError as exc:
        raise McpServerValidationError(str(exc)) from exc

    transport = cfg.get("transport")
    if transport not in _ALLOWED_RUNTIME_TRANSPORTS:
        raise McpServerValidationError(
            "Only HTTP servers can be connected at runtime ('http' or 'sse'); "
            "stdio servers are installed into the image and must be defined in "
            "the deployment config"
        )

    url = str(cfg.get("url") or "")
    parsed = urlparse(url)
    if len(url) > _MAX_URL_CHARS or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise McpServerValidationError("Server URL must be a valid http(s) URL")

    headers = cfg.get("headers")
    if headers is not None:
        if not isinstance(headers, dict) or len(headers) > _MAX_HEADERS:
            raise McpServerValidationError(
                f"headers must be an object with at most {_MAX_HEADERS} entries"
            )
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str) \
                    or len(key) > _MAX_HEADER_CHARS or len(value) > _MAX_HEADER_CHARS:
                raise McpServerValidationError("headers must map short strings to short strings")

    skill = cfg.get("skill")
    if skill is not None and (not isinstance(skill, str) or len(skill) > _MAX_NAME_CHARS):
        raise McpServerValidationError("skill must be a short skill name from the deployment's skills_dir")

    # Whitelist what a runtime entry may carry: everything else in the internal
    # schema (env, command, path, host_file_mounts, build_context, image, …) is
    # deploy-time machinery a chat user has no business setting.
    allowed_keys = {"transport", "url", "headers", "skill"}
    unknown = sorted(set(cfg) - allowed_keys)
    if unknown:
        raise McpServerValidationError(
            f"Unsupported field(s) for a runtime server: {', '.join(unknown)}"
        )
    return cfg


class McpRuntimeServerRegistry:
    """CRUD over the `mcp_runtime_servers` table."""

    def __init__(self, pg_config: Optional[Dict[str, Any]] = None, *, connection_pool=None):
        self._pool = connection_pool
        self._pg_config = pg_config
        self._schema_ready = False

    def _get_connection(self):
        if self._pool:
            return self._pool.get_connection_direct()
        elif self._pg_config:
            return psycopg2.connect(**self._pg_config)
        else:
            raise ValueError("No connection pool or pg_config provided")

    def _release_connection(self, conn) -> None:
        if self._pool:
            self._pool.release_connection(conn)
        else:
            conn.close()

    def _ensure_schema(self, conn) -> None:
        if self._schema_ready:
            return
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
        self._schema_ready = True

    def list(self) -> Dict[str, McpRuntimeServer]:
        conn = self._get_connection()
        try:
            self._ensure_schema(conn)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT name, config, added_by, created_at FROM mcp_runtime_servers ORDER BY created_at"
                )
                rows = cur.fetchall()
            return {
                row["name"]: McpRuntimeServer(
                    name=row["name"],
                    config=row["config"],
                    added_by=row["added_by"],
                    created_at=str(row["created_at"]) if row["created_at"] else None,
                )
                for row in rows
            }
        finally:
            self._release_connection(conn)

    def configs(self) -> Dict[str, Dict[str, Any]]:
        """Just the name -> internal-config mapping, for the MCP client merge."""
        return {name: server.config for name, server in self.list().items()}

    def add(self, name: str, entry: Dict[str, Any], *, added_by: Optional[str] = None) -> Dict[str, Any]:
        cfg = validate_runtime_server(name, entry)
        conn = self._get_connection()
        try:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_runtime_servers (name, config, added_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (name) DO UPDATE
                       SET config = EXCLUDED.config, added_by = EXCLUDED.added_by
                    """,
                    (name, json.dumps(cfg), added_by),
                )
            conn.commit()
            return cfg
        finally:
            self._release_connection(conn)

    def remove(self, name: str) -> bool:
        conn = self._get_connection()
        try:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mcp_runtime_servers WHERE name = %s", (name,))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        finally:
            self._release_connection(conn)
