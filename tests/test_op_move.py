import json
import pytest
from unittest.mock import MagicMock, patch
import tools

class TestOpenProjectMove:
    @pytest.fixture(autouse=True)
    def setup_op(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_OPENPROJECT", True)
        monkeypatch.setattr(tools, "OP_URL", "http://op.example.com")
        monkeypatch.setattr(tools, "OP_API_KEY", "fake-key")

    def test_move_op_work_package_success(self, monkeypatch):
        # Mock responses
        # 1. GET work package to get lockVersion
        mock_wp = {
            "id": 123,
            "subject": "Test WP",
            "lockVersion": 5,
            "_links": {
                "type": {"title": "Task"},
                "status": {"title": "Open"},
                "priority": {"title": "Normal"},
                "project": {"title": "Old Project"}
            }
        }

        # 2. PATCH work package to move it
        mock_moved_wp = mock_wp.copy()
        mock_moved_wp["_links"] = mock_wp["_links"].copy()
        mock_moved_wp["_links"]["project"] = {"title": "New Project"}

        def fake_op(method, path, **kwargs):
            if method == "GET" and path == "/work_packages/123":
                return mock_wp
            if method == "PATCH" and path == "/work_packages/123":
                assert kwargs["json"]["lockVersion"] == 5
                assert kwargs["json"]["_links"]["project"]["href"] == "/api/v3/projects/new-project"
                return mock_moved_wp
            return {}

        monkeypatch.setattr(tools, "_op", fake_op)

        result_str = tools.move_op_work_package(123, "new-project")
        result = json.loads(result_str)

        assert result["id"] == 123
        assert result["project"] == "New Project"

    def test_move_op_work_package_not_enabled(self, monkeypatch):
        monkeypatch.setattr(tools, "ENABLE_OPENPROJECT", False)
        result = tools.move_op_work_package(123, "new-project")
        assert "not enabled" in result

    def test_move_op_work_package_error(self, monkeypatch):
        def fake_op_error(method, path, **kwargs):
            raise Exception("API failure")

        monkeypatch.setattr(tools, "_op", fake_op_error)
        result = tools.move_op_work_package(123, "new-project")
        assert "OpenProject error: API failure" in result
