"""
Tests for the query_codebase tool.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tools

def test_query_codebase_registered(monkeypatch):
    """Verify that query_codebase is registered in TOOL_DEFINITIONS when ENABLE_DEV_AGENT is True."""
    monkeypatch.setattr(tools, "ENABLE_DEV_AGENT", True)
    # tools.TOOL_DEFINITIONS is built at import time or when _build_tool_definitions is called.
    # We call _build_tool_definitions to see the effect of the flag change.
    defs = tools._build_tool_definitions()
    tool_names = [d["name"] for d in defs]
    assert "query_codebase" in tool_names

    # Find the tool definition
    query_tool = next(d for d in defs if d["name"] == "query_codebase")
    assert "question" in query_tool["input_schema"]["properties"]
    assert "question" in query_tool["input_schema"]["required"]

def test_query_codebase_not_registered_when_disabled(monkeypatch):
    """Verify that query_codebase is NOT registered when ENABLE_DEV_AGENT is False."""
    monkeypatch.setattr(tools, "ENABLE_DEV_AGENT", False)
    defs = tools._build_tool_definitions()
    tool_names = [d["name"] for d in defs]
    assert "query_codebase" not in tool_names

def test_query_codebase_execution(monkeypatch):
    """Verify that execute_tool correctly dispatches to query_codebase."""
    mock_query = MagicMock(return_value="Answer with file.py:10")
    # Mock the underlying function in the tools module (where it was imported)
    monkeypatch.setattr(tools, "_query_codebase", mock_query)

    result = tools.execute_tool("query_codebase", {"question": "How does X work?"})

    assert result == "Answer with file.py:10"
    mock_query.assert_called_once_with("How does X work?")

def test_query_codebase_execution_missing_param():
    """Verify that query_codebase execution handles missing parameters gracefully."""
    result = tools.execute_tool("query_codebase", {})
    assert "Error" in result
    assert "question" in result
