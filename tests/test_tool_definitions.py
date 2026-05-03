"""
Layer 2: Feature flag / tool definition tests.

Verifies that _build_tool_definitions() returns the correct tool set
depending on which ENABLE_* flags are active, and that schemas are
structurally sound.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _names(defs: list[dict]) -> set[str]:
    return {d["name"] for d in defs}


def _tool(defs: list[dict], name: str) -> dict:
    for d in defs:
        if d["name"] == name:
            return d
    raise KeyError(f"Tool '{name}' not found in definitions")


# ---------------------------------------------------------------------------
# Presence / absence by feature flag
# ---------------------------------------------------------------------------

class TestToolPresence:
    def test_all_flags_on_includes_all_tools(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JELLYFIN", True)
        monkeypatch.setattr(tools, "ENABLE_JENKINS",  True)
        monkeypatch.setattr(tools, "ENABLE_RIPPING",  True)
        monkeypatch.setattr(tools, "ENABLE_SMART",    True)
        defs = tools._build_tool_definitions()
        names = _names(defs)
        assert "query_jellyfin"      in names
        assert "query_jenkins"       in names
        assert "trigger_jenkins_job" in names
        assert "query_ripping"       in names
        assert "query_system"        in names

    def test_jenkins_disabled_removes_jenkins_tools(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JENKINS", False)
        defs = tools._build_tool_definitions()
        names = _names(defs)
        assert "query_jenkins"   not in names
        assert "trigger_jenkins_job" not in names

    def test_jenkins_disabled_keeps_other_tools(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JENKINS", False)
        defs = tools._build_tool_definitions()
        names = _names(defs)
        # Core tools must survive
        assert "query_system"    in names
        assert "get_log_tail"    in names
        assert "manage_schedule" in names

    def test_jellyfin_disabled_removes_jellyfin_tool(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JELLYFIN", False)
        defs = tools._build_tool_definitions()
        assert "query_jellyfin" not in _names(defs)

    def test_ripping_disabled_removes_ripping_tool(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_RIPPING", False)
        defs = tools._build_tool_definitions()
        assert "query_ripping" not in _names(defs)

    def test_all_flags_off_still_has_core_tools(self, monkeypatch):
        for flag in ("ENABLE_JENKINS", "ENABLE_JELLYFIN", "ENABLE_RIPPING", "ENABLE_SMART"):
            monkeypatch.setattr(tools, flag, False)
        defs = tools._build_tool_definitions()
        names = _names(defs)
        for expected in [
            "query_system", "get_log_tail", "get_service_status",
            "get_performance_history", "query_media_library", "manage_schedule",
        ]:
            assert expected in names, f"Missing core tool: {expected}"


# ---------------------------------------------------------------------------
# SMART aspect in query_system
# ---------------------------------------------------------------------------

class TestSmartAspect:
    def test_smart_enabled_adds_smart_aspect(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_SMART", True)
        monkeypatch.setattr(tools, "SMART_DEVICES", [("/dev/sda", "My SSD")])
        defs = tools._build_tool_definitions()
        health = _tool(defs, "query_system")
        aspects = health["input_schema"]["properties"]["aspect"]["enum"]
        assert "smart" in aspects

    def test_smart_disabled_removes_smart_aspect(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_SMART", False)
        defs = tools._build_tool_definitions()
        health = _tool(defs, "query_system")
        aspects = health["input_schema"]["properties"]["aspect"]["enum"]
        assert "smart" not in aspects

    def test_smart_enabled_but_no_devices_omits_smart_aspect(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_SMART", True)
        monkeypatch.setattr(tools, "SMART_DEVICES", [])
        defs = tools._build_tool_definitions()
        health = _tool(defs, "query_system")
        aspects = health["input_schema"]["properties"]["aspect"]["enum"]
        assert "smart" not in aspects

    def test_smart_description_includes_device_labels(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_SMART", True)
        monkeypatch.setattr(tools, "SMART_DEVICES", [
            ("/dev/sda", "Boot SSD"),
            ("/dev/sdb", "Media HDD"),
        ])
        defs = tools._build_tool_definitions()
        health = _tool(defs, "query_system")
        desc = health["description"]
        assert "Boot SSD" in desc
        assert "Media HDD" in desc


# ---------------------------------------------------------------------------
# Hardware aspect in query_system
# ---------------------------------------------------------------------------

class TestHardwareAspect:
    def test_hardware_aspect_present_by_default(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_SMART", False)
        defs = tools._build_tool_definitions()
        health = _tool(defs, "query_system")
        aspects = health["input_schema"]["properties"]["aspect"]["enum"]
        assert "hardware" in aspects

    def test_hardware_aspect_in_description(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_SMART", False)
        defs = tools._build_tool_definitions()
        health = _tool(defs, "query_system")
        desc = health["description"]
        assert "hardware" in desc
        assert "motherboard" in desc
        assert "CPU" in desc
        assert "GPU" in desc
        assert "RAM" in desc


# ---------------------------------------------------------------------------
# Log enum matches whitelists
# ---------------------------------------------------------------------------

class TestLogEnum:
    def test_log_enum_matches_file_and_docker_logs(self, monkeypatch):
        monkeypatch.setattr(tools, "ALLOWED_FILE_LOGS", {"mylog": "/var/log/mylog.log"})
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", {"mycontainer"})
        defs = tools._build_tool_definitions()
        log_tool = _tool(defs, "get_log_tail")
        enum = log_tool["input_schema"]["properties"]["log_name"]["enum"]
        assert "mylog" in enum
        assert "mycontainer" in enum

    def test_log_enum_does_not_contain_unlisted_name(self, monkeypatch):
        monkeypatch.setattr(tools, "ALLOWED_FILE_LOGS", {})
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", {"legitcontainer"})
        defs = tools._build_tool_definitions()
        log_tool = _tool(defs, "get_log_tail")
        enum = log_tool["input_schema"]["properties"]["log_name"]["enum"]
        assert "unlisted" not in enum


# ---------------------------------------------------------------------------
# Jenkins tool descriptions reference known jobs
# ---------------------------------------------------------------------------

class TestJenkinsDescription:
    def test_jenkins_jobs_appear_in_description(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JENKINS", True)
        monkeypatch.setattr(tools, "JENKINS_JOBS", ["MyBuild", "MyTest"])
        defs = tools._build_tool_definitions()
        trigger = _tool(defs, "trigger_jenkins_job")
        assert "MyBuild" in trigger["description"]
        assert "MyTest" in trigger["description"]
        qj = _tool(defs, "query_jenkins")
        assert "MyBuild" in qj["description"]
        assert "MyTest" in qj["description"]


# ---------------------------------------------------------------------------
# restart_container gating
# ---------------------------------------------------------------------------

class TestRestartContainer:
    def test_hidden_when_whitelist_empty(self, monkeypatch):
        monkeypatch.setattr(tools, "RESTARTABLE_CONTAINERS", set())
        defs = tools._build_tool_definitions()
        assert "restart_container" not in _names(defs)

    def test_present_when_whitelist_populated(self, monkeypatch):
        monkeypatch.setattr(tools, "RESTARTABLE_CONTAINERS", {"jellyfin", "excalidraw"})
        defs = tools._build_tool_definitions()
        assert "restart_container" in _names(defs)

    def test_enum_matches_whitelist(self, monkeypatch):
        monkeypatch.setattr(tools, "RESTARTABLE_CONTAINERS", {"jellyfin", "excalidraw"})
        defs = tools._build_tool_definitions()
        t = _tool(defs, "restart_container")
        assert set(t["input_schema"]["properties"]["container"]["enum"]) == {"jellyfin", "excalidraw"}


# ---------------------------------------------------------------------------
# Schema structural validation
# ---------------------------------------------------------------------------

class TestSchemaStructure:
    def test_all_tools_have_required_fields(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JELLYFIN", True)
        monkeypatch.setattr(tools, "ENABLE_JENKINS",  True)
        monkeypatch.setattr(tools, "ENABLE_RIPPING",  True)
        defs = tools._build_tool_definitions()
        for tool_def in defs:
            name = tool_def.get("name", "<unnamed>")
            assert "name" in tool_def,         f"{name}: missing 'name'"
            assert "description" in tool_def,  f"{name}: missing 'description'"
            assert "input_schema" in tool_def, f"{name}: missing 'input_schema'"
            schema = tool_def["input_schema"]
            assert schema.get("type") == "object", f"{name}: input_schema.type must be 'object'"
            assert "properties" in schema,     f"{name}: missing 'properties'"

    def test_no_tool_has_empty_enum(self, monkeypatch):
        """Enums must not be empty lists — Claude will reject the schema."""
        monkeypatch.setattr(tools, "ENABLE_JELLYFIN", True)
        monkeypatch.setattr(tools, "ENABLE_JENKINS",  True)
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", {"jellyfin"})
        monkeypatch.setattr(tools, "ALLOWED_FILE_LOGS", {})
        monkeypatch.setattr(tools, "ALL_SERVICES", ["jellyfin"])
        defs = tools._build_tool_definitions()
        for tool_def in defs:
            name = tool_def["name"]
            for prop_name, prop in tool_def["input_schema"].get("properties", {}).items():
                if "enum" in prop:
                    assert prop["enum"], (
                        f"{name}.{prop_name}: enum is empty — "
                        "Claude will reject this schema"
                    )

    def test_no_duplicate_tool_names(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_JELLYFIN", True)
        monkeypatch.setattr(tools, "ENABLE_JENKINS",  True)
        defs = tools._build_tool_definitions()
        names = [d["name"] for d in defs]
        assert len(names) == len(set(names)), "Duplicate tool names found"
