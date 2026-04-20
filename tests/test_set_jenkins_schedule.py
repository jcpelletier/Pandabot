"""
Tests for set_jenkins_schedule — view, preview, XML manipulation, confirmation gate.

Every test that would mutate a Jenkins job must call with confirmed=False
or must assert that no POST was made, ensuring the confirmation gate cannot
be accidentally bypassed.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tools


# ---------------------------------------------------------------------------
# XML fixtures — representative Jenkins config.xml shapes
# ---------------------------------------------------------------------------

XML_WITH_TIMER = """\
<?xml version='1.1' encoding='UTF-8'?>
<project>
  <triggers>
    <hudson.triggers.TimerTrigger>
      <spec>H 0 * * *</spec>
    </hudson.triggers.TimerTrigger>
  </triggers>
</project>"""

XML_SELF_CLOSING_TRIGGERS = """\
<?xml version='1.1' encoding='UTF-8'?>
<project>
  <triggers/>
</project>"""

XML_OPEN_TRIGGERS = """\
<?xml version='1.1' encoding='UTF-8'?>
<project>
  <triggers>
  </triggers>
</project>"""

XML_NO_TRIGGERS = """\
<?xml version='1.1' encoding='UTF-8'?>
<project>
  <description>A job with no trigger element</description>
</project>"""

XML_OTHER_TRIGGER = """\
<?xml version='1.1' encoding='UTF-8'?>
<project>
  <triggers>
    <hudson.triggers.SCMTrigger>
      <spec>H/5 * * * *</spec>
    </hudson.triggers.SCMTrigger>
  </triggers>
</project>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_jenkins(monkeypatch, xml_response: str) -> list:
    """
    Patch requests.get to return xml_response and requests.post to capture
    the submitted XML body. Returns the list that POST bodies are appended to.
    """
    post_bodies: list[str] = []

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = xml_response
    get_resp.raise_for_status = MagicMock()

    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.raise_for_status = MagicMock()

    def fake_get(url, auth=None, timeout=None):
        return get_resp

    def fake_post(url, data=None, headers=None, auth=None, timeout=None):
        body = data.decode("utf-8") if isinstance(data, bytes) else (data or "")
        post_bodies.append(body)
        return post_resp

    monkeypatch.setattr(tools.requests, "get", fake_get)
    monkeypatch.setattr(tools.requests, "post", fake_post)
    return post_bodies


@pytest.fixture(autouse=True)
def _allow_test_job(monkeypatch):
    """Make 'TestJob' and 'Process_Movies' valid in all tests in this module."""
    monkeypatch.setattr(tools, "JENKINS_JOBS", ["TestJob", "Process_Movies"])
    monkeypatch.setattr(tools, "JENKINS_URL", "http://jenkins:8080")


# ---------------------------------------------------------------------------
# Whitelist guard
# ---------------------------------------------------------------------------

class TestWhitelist:
    def test_unknown_job_rejected_immediately(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("UnknownJob", "H * * * *", confirmed=True)
        assert "not in the allowed list" in result
        assert post_bodies == []

    def test_known_job_proceeds(self, monkeypatch):
        _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob")
        assert "not in the allowed list" not in result


# ---------------------------------------------------------------------------
# View mode (no schedule arg)
# ---------------------------------------------------------------------------

class TestViewMode:
    def test_view_shows_current_spec(self, monkeypatch):
        _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob")
        assert "H 0 * * *" in result
        assert "TestJob" in result

    def test_view_with_no_trigger_says_none(self, monkeypatch):
        _mock_jenkins(monkeypatch, XML_NO_TRIGGERS)
        result = tools.set_jenkins_schedule("TestJob")
        assert "none" in result.lower()

    def test_view_does_not_post(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        tools.set_jenkins_schedule("TestJob")
        assert post_bodies == []


# ---------------------------------------------------------------------------
# Confirmation gate — confirmed=False must never POST
# ---------------------------------------------------------------------------

class TestConfirmationGate:
    def test_unconfirmed_update_does_not_post(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        tools.set_jenkins_schedule("TestJob", "H * * * *", confirmed=False)
        assert post_bodies == []

    def test_unconfirmed_add_does_not_post(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_NO_TRIGGERS)
        tools.set_jenkins_schedule("TestJob", "H 3 * * *", confirmed=False)
        assert post_bodies == []

    def test_unconfirmed_disable_does_not_post(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        tools.set_jenkins_schedule("TestJob", "disabled", confirmed=False)
        assert post_bodies == []

    def test_unconfirmed_returns_preview_with_current_and_new(self, monkeypatch):
        _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob", "H * * * *", confirmed=False)
        assert "H 0 * * *" in result   # current
        assert "H * * * *" in result   # proposed
        assert "yes" in result.lower()

    def test_unconfirmed_same_spec_returns_no_change(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob", "H 0 * * *", confirmed=False)
        assert "no change" in result.lower() or "already" in result.lower()
        assert post_bodies == []


# ---------------------------------------------------------------------------
# Confirmed — XML mutation correctness
# ---------------------------------------------------------------------------

class TestConfirmedUpdate:
    def test_updates_existing_spec(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob", "H * * * *", confirmed=True)
        assert "✅" in result
        assert len(post_bodies) == 1
        assert "H * * * *" in post_bodies[0]
        assert "H 0 * * *" not in post_bodies[0]

    def test_old_spec_replaced_not_duplicated(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        tools.set_jenkins_schedule("TestJob", "H/15 * * * *", confirmed=True)
        assert post_bodies[0].count("<spec>") == 1

    def test_adds_trigger_to_self_closing_triggers(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_SELF_CLOSING_TRIGGERS)
        tools.set_jenkins_schedule("TestJob", "H 3 * * *", confirmed=True)
        assert "H 3 * * *" in post_bodies[0]
        assert "hudson.triggers.TimerTrigger" in post_bodies[0]

    def test_adds_trigger_to_open_triggers_element(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_OPEN_TRIGGERS)
        tools.set_jenkins_schedule("TestJob", "H 3 * * *", confirmed=True)
        assert "H 3 * * *" in post_bodies[0]

    def test_adds_trigger_when_no_triggers_element(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_NO_TRIGGERS)
        tools.set_jenkins_schedule("TestJob", "H 3 * * *", confirmed=True)
        assert "H 3 * * *" in post_bodies[0]
        assert "hudson.triggers.TimerTrigger" in post_bodies[0]

    def test_preserves_other_triggers_when_adding(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_OTHER_TRIGGER)
        tools.set_jenkins_schedule("TestJob", "H 0 * * *", confirmed=True)
        # SCMTrigger must still be present
        assert "SCMTrigger" in post_bodies[0]
        assert "TimerTrigger" in post_bodies[0]

    def test_success_message_includes_job_and_spec(self, monkeypatch):
        _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob", "H * * * *", confirmed=True)
        assert "TestJob" in result
        assert "H * * * *" in result


class TestConfirmedDisable:
    def test_removes_timer_trigger(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        tools.set_jenkins_schedule("TestJob", "disabled", confirmed=True)
        assert "TimerTrigger" not in post_bodies[0]

    def test_preserves_rest_of_config(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        tools.set_jenkins_schedule("TestJob", "disabled", confirmed=True)
        assert "<project>" in post_bodies[0]

    def test_preserves_other_trigger_when_disabling(self, monkeypatch):
        post_bodies = _mock_jenkins(monkeypatch, XML_OTHER_TRIGGER)
        # No TimerTrigger to remove — should still succeed gracefully
        result = tools.set_jenkins_schedule("TestJob", "disabled", confirmed=True)
        assert "SCMTrigger" in post_bodies[0]

    def test_disable_success_message(self, monkeypatch):
        _mock_jenkins(monkeypatch, XML_WITH_TIMER)
        result = tools.set_jenkins_schedule("TestJob", "disabled", confirmed=True)
        assert "✅" in result
        assert "removed" in result.lower() or "disabled" in result.lower()


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------

class TestHttpErrors:
    def test_jenkins_404_returns_not_found(self, monkeypatch):
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status = MagicMock()
        monkeypatch.setattr(tools.requests, "get", lambda *a, **kw: resp)
        result = tools.set_jenkins_schedule("TestJob", "H * * * *")
        assert "not found" in result.lower()

    def test_jenkins_get_exception_returns_error(self, monkeypatch):
        import requests as req
        monkeypatch.setattr(
            tools.requests, "get",
            lambda *a, **kw: (_ for _ in ()).throw(req.RequestException("timeout")),
        )
        result = tools.set_jenkins_schedule("TestJob")
        assert "Could not fetch" in result or "timeout" in result.lower()

    def test_jenkins_post_failure_returns_error(self, monkeypatch):
        import requests as req
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.text = XML_WITH_TIMER
        get_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(tools.requests, "get", lambda *a, **kw: get_resp)
        monkeypatch.setattr(
            tools.requests, "post",
            lambda *a, **kw: (_ for _ in ()).throw(req.RequestException("write failed")),
        )
        result = tools.set_jenkins_schedule("TestJob", "H * * * *", confirmed=True)
        assert "Failed to save" in result or "write failed" in result.lower()
