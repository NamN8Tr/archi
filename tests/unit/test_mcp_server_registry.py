"""Unit tests for the runtime MCP server registry (the /mcp chat command).

Covers the pure validation layer — the transport restriction that keeps
runtime adds HTTP-only, the Claude-format normalization reuse, and the field
whitelist. DB CRUD is a thin psycopg2 layer exercised in deployment testing.
"""

import pytest

from src.utils.mcp_server_registry import (
    McpServerValidationError,
    validate_runtime_server,
)


def test_http_url_entry_normalizes_to_streamable_http():
    cfg = validate_runtime_server("my-tools", {"type": "http", "url": "http://127.0.0.1:9000/mcp"})
    assert cfg == {"transport": "streamable_http", "url": "http://127.0.0.1:9000/mcp"}


def test_sse_entry_keeps_sse_transport():
    cfg = validate_runtime_server("legacy", {"type": "sse", "url": "https://tools.example.org/sse"})
    assert cfg["transport"] == "sse"


def test_type_inferred_from_url():
    cfg = validate_runtime_server("inferred", {"url": "https://tools.example.org/mcp"})
    assert cfg["transport"] == "streamable_http"


def test_headers_and_skill_pass_through():
    cfg = validate_runtime_server("with-extras", {
        "type": "http",
        "url": "https://tools.example.org/mcp",
        "headers": {"Authorization": "Bearer abc"},
        "skill": "my-skill",
    })
    assert cfg["headers"] == {"Authorization": "Bearer abc"}
    assert cfg["skill"] == "my-skill"


def test_comment_keys_are_dropped():
    cfg = validate_runtime_server("commented", {
        "type": "http",
        "url": "https://tools.example.org/mcp",
        "_comment": "docs only",
    })
    assert "_comment" not in cfg


def test_stdio_is_rejected_for_runtime_adds():
    with pytest.raises(McpServerValidationError, match="stdio"):
        validate_runtime_server("evil", {"type": "stdio", "command": "python", "args": ["-c", "1"]})


def test_stdio_inferred_from_command_is_rejected():
    with pytest.raises(McpServerValidationError, match="stdio"):
        validate_runtime_server("evil", {"command": "bash"})


@pytest.mark.parametrize("name", ["", "-leading", "has space", "a" * 65, "semi;colon"])
def test_bad_names_rejected(name):
    with pytest.raises(McpServerValidationError, match="name"):
        validate_runtime_server(name, {"type": "http", "url": "https://x.example/mcp"})


@pytest.mark.parametrize("url", ["", "ftp://x.example/mcp", "not-a-url", "file:///etc/passwd"])
def test_bad_urls_rejected(url):
    # An empty url is rejected by the shared normalizer ("has no 'url'"),
    # the rest by the runtime http(s) check ("must be a valid http(s) URL").
    with pytest.raises(McpServerValidationError, match="(?i)url"):
        validate_runtime_server("srv", {"type": "http", "url": url})


def test_deploy_time_fields_rejected():
    with pytest.raises(McpServerValidationError, match="Unsupported field"):
        validate_runtime_server("srv", {
            "type": "http",
            "url": "https://x.example/mcp",
            "host_file_mounts": ["/etc/passwd"],
        })


def test_env_field_rejected():
    with pytest.raises(McpServerValidationError, match="Unsupported field"):
        validate_runtime_server("srv", {
            "type": "http",
            "url": "https://x.example/mcp",
            "env": {"PATH": "/tmp"},
        })


def test_headers_must_be_flat_string_map():
    with pytest.raises(McpServerValidationError, match="headers"):
        validate_runtime_server("srv", {
            "type": "http",
            "url": "https://x.example/mcp",
            "headers": {"nested": {"a": 1}},
        })
