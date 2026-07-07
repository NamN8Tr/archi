from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import List, Any, Tuple, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain.tools import BaseTool

from src.utils.config_access import get_mcp_servers_config, get_full_config
from src.utils.logging import get_logger
from src.utils.mcp_json import expand_env_placeholders
from src.archi.pipelines.agents.utils.skill_utils import load_skill

logger = get_logger(__name__)

# Last build's per-server outcome, refreshed by every initialize_mcp_client run.
# The /api/mcp endpoints read this so the UI can show real connect status
# without opening new MCP sessions. Shape:
#   {"active": {name: [tool names]}, "failed": {name: error}, "built_at": iso8601}
last_build_status: dict = {"active": {}, "failed": {}, "built_at": None}


def _runtime_mcp_servers() -> dict:
    """Runtime-added servers (the `/mcp` chat command), or {} when unavailable."""
    try:
        from src.utils.postgres_service_factory import PostgresServiceFactory
        factory = PostgresServiceFactory.get_instance()
        if factory is None:
            return {}
        return factory.mcp_server_registry.configs()
    except Exception as e:
        logger.warning(f"Runtime MCP server registry unavailable: {e}")
        return {}


def _describe_mcp_error(exc: BaseException, *, timeout: float | None = None) -> str:
    """A human-usable one-liner for an MCP connection failure.

    anyio task groups wrap the real failure in nested ExceptionGroups whose
    str() is 'unhandled errors in a TaskGroup (1 sub-exception)' — useless in
    the UI. Unwrap to the first leaf exception and name it.
    """
    import asyncio

    depth = 0
    while getattr(exc, "exceptions", None) and depth < 10:
        exc = exc.exceptions[0]
        depth += 1
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and timeout is not None:
        return f"timed out after {timeout:.0f}s"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def probe_mcp_server(name: str, server_cfg: dict, timeout: float = 10.0) -> List[str]:
    """Open a one-off session to a single server and return its tool names.

    Used by the /api/mcp endpoints for immediate connect feedback when a user
    adds or reconnects a server from the chat UI. Updates only this server's
    entry in last_build_status; raises on failure (caller reports str(exc)).
    """
    import asyncio

    _patch_langchain_mcp_dict_schema_recursion()
    _archi_only_fields = {
        "env_from_secrets", "host_file_mounts", "build_context", "image", "path", "skill",
    }
    cfg = {k: v for k, v in server_cfg.items() if k not in _archi_only_fields}
    cfg = expand_env_placeholders(cfg, os.environ)
    if cfg.get("transport") == "stdio":
        cfg["env"] = {**os.environ, **(cfg.get("env") or {})}
    else:
        cfg.pop("env", None)
    client = MultiServerMCPClient({name: cfg})
    try:
        tools = await asyncio.wait_for(client.get_tools(server_name=name), timeout)
    except Exception as e:
        message = _describe_mcp_error(e, timeout=timeout)
        last_build_status["failed"][name] = message
        last_build_status["active"].pop(name, None)
        raise RuntimeError(message) from e
    tool_names = [tool.name for tool in tools]
    last_build_status["active"][name] = tool_names
    last_build_status["failed"].pop(name, None)
    return tool_names


def get_effective_mcp_servers() -> dict:
    """Config-defined MCP servers merged with runtime-added ones.

    Config entries win on name collision — a chat user must not be able to
    shadow an operator-managed server.
    """
    config_servers = get_mcp_servers_config() or {}
    merged = dict(_runtime_mcp_servers())
    for name, cfg in config_servers.items():
        if name in merged:
            logger.warning(
                f"Runtime MCP server '{name}' shadowed by config-defined server; using config"
            )
        merged[name] = cfg
    return merged


def _patch_langchain_mcp_dict_schema_recursion() -> None:
    """Work around a langchain-core / langchain-mcp-adapters incompatibility.

    langchain-mcp-adapters sets each tool's ``args_schema`` to the MCP server's raw
    JSON-schema *dict* rather than a pydantic model. langchain-core's
    ``_filter_injected_args`` then calls ``get_all_basemodel_annotations(args_schema)``;
    for a non-pydantic input that helper recurses on ``get_origin(cls)``, which for a dict
    (then ``None``) never terminates -> ``RecursionError`` on every MCP tool call. It is
    caught and logged at DEBUG, so it is non-fatal, but it burns ~1000 stack frames per
    call and floods the logs. We add the missing base case: a non-type with no generic
    origin yields no annotations instead of recursing. Real pydantic ``args_schema`` models
    are unaffected. Idempotent; safe to call on every init.
    """
    from typing import get_origin
    from langchain_core.tools import base as _lc_tools_base

    if getattr(_lc_tools_base, "_archi_dict_schema_guard", False):
        return
    _orig = _lc_tools_base.get_all_basemodel_annotations

    def _guarded(cls, *args, **kwargs):
        if not isinstance(cls, type) and get_origin(cls) is None:
            return {}
        return _orig(cls, *args, **kwargs)

    _lc_tools_base.get_all_basemodel_annotations = _guarded
    _lc_tools_base._archi_dict_schema_guard = True
    logger.info("Applied langchain-core args_schema recursion guard for MCP dict schemas.")


async def initialize_mcp_client(servers: dict | None = None) -> Tuple[Optional[MultiServerMCPClient], List[BaseTool], str]:
    """
    Initializes the MCP client and fetches tool definitions.

    Args:
        servers: If provided, use these server definitions directly instead of
            reading from the deployment config.
    Returns:
        client: The active client instance (must be kept alive by the caller).
        tools: The list of LangChain-compatible tools.
        skills_text: Concatenated skill content from all MCP servers that declare
            a `skill`. Empty string if no server has a skill. The caller is
            responsible for appending this to the agent's system prompt — we inject
            here only once per agent rather than into each tool description, so
            the content doesn't multiply by tool count.
    """

    _patch_langchain_mcp_dict_schema_recursion()

    mcp_servers = servers if servers is not None else get_effective_mcp_servers()

    # Strip archi-only fields that langchain-mcp-adapters doesn't understand.
    # These are consumed by the compose template (sidecars), the legacy stdio
    # install path, or post-load tool customization — the MCP client itself only
    # knows about transport-specific fields.
    _archi_only_fields = {
        "env_from_secrets", "host_file_mounts", "build_context", "image", "path", "skill",
    }
    client_configs: dict[str, dict] = {}
    server_skills: dict[str, str] = {}
    failed_servers: dict[str, str] = {}
    full_config = get_full_config()
    for name, server_cfg in mcp_servers.items():
        # Load any declared skill so we can append it to this server's tool descriptions.
        skill_name = server_cfg.get("skill")
        if skill_name:
            skill_content = load_skill(skill_name, full_config)
            if skill_content:
                server_skills[name] = skill_content

        cfg = {k: v for k, v in server_cfg.items() if k not in _archi_only_fields}
        # Expand ${VAR} / ${VAR:-default} placeholders (the Claude .mcp.json syntax)
        # against this process's env at connect time — so secrets referenced from
        # url/headers/env come from the container environment instead of being
        # baked into rendered configs. Must run BEFORE the stdio os.environ merge
        # below: only declared values get expanded, never the inherited host env.
        try:
            cfg = expand_env_placeholders(cfg, os.environ)
        except ValueError as e:
            logger.error(f"Skipping MCP server '{name}': {e}")
            failed_servers[name] = str(e)
            continue
        transport = cfg.get("transport")
        if transport == "stdio":
            # stdio subprocesses inherit nothing by default (mcp.client.stdio uses
            # an empty env). Forward the parent process env so stdio MCP servers see
            # what they need.
            cfg["env"] = {**os.environ, **(cfg.get("env") or {})}
        else:
            # For HTTP-based transports, `env` is for the sidecar container (compose),
            # not the MCP client connection — drop it here.
            cfg.pop("env", None)
        client_configs[name] = cfg

    logger.info(f"Configuring MCP client with servers: {list(client_configs.keys())}")
    client = MultiServerMCPClient(client_configs)

    all_tools: List[BaseTool] = []
    tools_by_server: dict[str, list[str]] = {}

    for name in client_configs.keys():
        try:
            tools = await client.get_tools(server_name=name)
            for tool in tools:
                # Return error messages to the LLM instead of crashing the agent chain.
                tool.handle_tool_error = True
                logger.info(f"Loaded tool from MCP server '{name}': {tool.name} - {tool.description}")
            all_tools.extend(tools)
            tools_by_server[name] = [tool.name for tool in tools]
        except Exception as e:
            message = _describe_mcp_error(e)
            logger.error(f"Failed to fetch tools from MCP server '{name}': {message}")
            failed_servers[name] = message

    logger.info(f"Active MCP servers: {[n for n in client_configs if n not in failed_servers]}")
    logger.warning(f"Failed MCP servers: {list(failed_servers.keys())}")

    # Publish the outcome for the /api/mcp endpoints (read-only UI status).
    last_build_status["active"] = tools_by_server
    last_build_status["failed"] = dict(failed_servers)
    last_build_status["built_at"] = datetime.now(timezone.utc).isoformat()

    # Build a single combined skills block keyed by server name — this is appended
    # to the agent's system prompt once, rather than duplicated across every tool.
    skills_parts: List[str] = []
    for name, skill_content in server_skills.items():
        if name not in failed_servers:
            skills_parts.append(
                f"\n--- {name} MCP Server Domain Knowledge ---\n{skill_content}"
            )
    skills_text = "".join(skills_parts)

    return client, all_tools, skills_text
