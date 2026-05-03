"""
Layer 3: Tool function tests with mocked subprocess and HTTP calls.

Key regression: SMART attribute parsing must use cols[9] (RAW_VALUE first token)
not cols[-1], because some attributes have trailing annotations like
"(Min/Max 20/46)" that cause cols[-1] to return "46)" instead of the true value.
"""

import os
import sys
import datetime
import json
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_ago(days: float) -> int:
    """Return a Unix-epoch millisecond timestamp N days ago."""
    dt = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    return int(dt.timestamp() * 1000)


def _make_subprocess_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# SMART attribute parsing — cols[9] regression
# ---------------------------------------------------------------------------

# Real-world smartctl -H -A output excerpt.  The Temperature_Celsius line has
# a trailing "(Min/Max 20/46)" annotation.  If the code used cols[-1] it would
# return "46)" instead of the correct value "35".
SMARTCTL_HEALTHY_OUTPUT = """\
smartctl 7.3 2022-02-28 r5338 [x86_64-linux-6.8.0] (local build)
Copyright (C) 2002-22, Bruce Allen, Christian Franke, www.smartmontools.org

=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  1 Raw_Read_Error_Rate     0x000f   100   100   006    Pre-fail  Always       -       0
  5 Reallocated_Sector_Ct   0x0033   100   100   036    Pre-fail  Always       -       0
  9 Power_On_Hours          0x0032   099   099   000    Old_age   Always       -       2745
190 Airflow_Temperature_Cel 0x0022   065   045   000    Old_age   Always       -       35
194 Temperature_Celsius     0x0022   035   045   000    Old_age   Always       -       35 (Min/Max 20/46)
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       0
198 Offline_Uncorrectable   0x0010   100   100   000    Old_age   Always       -       0
"""

SMARTCTL_FAILED_OUTPUT = """\
smartctl 7.3 2022-02-28 r5338 [x86_64-linux-6.8.0] (local build)

=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: FAILED!

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   001   001   036    Pre-fail  FAILING_NOW  -       4096
194 Temperature_Celsius     0x0022   060   040   000    Old_age   Always       -       40 (Min/Max 15/55)
"""


class TestSmartParsing:
    """Regression tests for SMART attribute parsing."""

    @pytest.fixture(autouse=True)
    def setup_devices(self, monkeypatch):
        monkeypatch.setattr(tools, "SMART_DEVICES", [("/dev/sda", "Test Drive")])

    def _run_smart(self, output: str, monkeypatch) -> str:
        monkeypatch.setattr(
            tools.subprocess, "run",
            lambda *a, **kw: _make_subprocess_result(stdout=output),
        )
        return tools.query_system_health("smart")

    def test_temperature_uses_first_token_not_last(self, monkeypatch):
        """
        REGRESSION: cols[9] (first RAW_VALUE token) must be used, not cols[-1].
        Temperature_Celsius raw value is "35 (Min/Max 20/46)" — cols[-1] would
        return "46)" which would fail int() conversion or show wrong temp.
        """
        result = self._run_smart(SMARTCTL_HEALTHY_OUTPUT, monkeypatch)
        assert "35°C" in result, f"Expected '35°C' in result, got: {result}"

    def test_annotation_not_in_output(self, monkeypatch):
        """The trailing annotation text must not appear in the formatted output."""
        result = self._run_smart(SMARTCTL_HEALTHY_OUTPUT, monkeypatch)
        assert "46)" not in result, f"Annotation '46)' leaked into output: {result}"
        assert "Min/Max" not in result

    def test_health_passed_shown(self, monkeypatch):
        result = self._run_smart(SMARTCTL_HEALTHY_OUTPUT, monkeypatch)
        assert "PASSED" in result

    def test_health_failed_shown(self, monkeypatch):
        result = self._run_smart(SMARTCTL_FAILED_OUTPUT, monkeypatch)
        assert "FAILED" in result

    def test_reallocated_sectors_nonzero_flagged(self, monkeypatch):
        result = self._run_smart(SMARTCTL_FAILED_OUTPUT, monkeypatch)
        assert "⚠️" in result, "Non-zero reallocated sectors should trigger warning emoji"

    def test_reallocated_sectors_zero_not_flagged(self, monkeypatch):
        result = self._run_smart(SMARTCTL_HEALTHY_OUTPUT, monkeypatch)
        # Reallocated_Sector_Ct = 0 → no warning
        lines = [l for l in result.splitlines() if "Reallocated" in l]
        for line in lines:
            assert "⚠️" not in line, f"Zero reallocated sectors should not be flagged: {line}"

    def test_power_on_hours_formatted(self, monkeypatch):
        result = self._run_smart(SMARTCTL_HEALTHY_OUTPUT, monkeypatch)
        assert "2,745h" in result or "2745h" in result

    def test_smartctl_not_found_graceful(self, monkeypatch):
        def raise_fnf(*a, **kw):
            raise FileNotFoundError("smartctl not found")
        monkeypatch.setattr(tools.subprocess, "run", raise_fnf)
        result = tools.query_system_health("smart")
        assert "not found" in result.lower() or "smartctl" in result.lower()

    def test_device_label_in_output(self, monkeypatch):
        result = self._run_smart(SMARTCTL_HEALTHY_OUTPUT, monkeypatch)
        assert "Test Drive" in result


# ---------------------------------------------------------------------------
# get_jenkins_build_history — since_days filtering
# ---------------------------------------------------------------------------

class TestJenkinsBuildHistory:
    """Tests for the time-window filtering mode (since_days parameter)."""

    def _mock_jenkins_response(self, builds: list[dict], monkeypatch):
        """Mock requests.get to return a build list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"builds": builds}
        mock_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(tools.requests, "get", lambda *a, **kw: mock_resp)

    def _make_build(self, number: int, result: str, days_ago: float) -> dict:
        return {
            "number": number,
            "result": result,
            "building": False,
            "timestamp": _ms_ago(days_ago),
            "duration": 60_000,
            "url": f"http://jenkins/job/Test/{number}/",
        }

    def test_since_days_includes_recent_builds(self, monkeypatch):
        builds = [
            self._make_build(10, "SUCCESS", days_ago=1),
            self._make_build(9,  "SUCCESS", days_ago=3),
            self._make_build(8,  "FAILURE", days_ago=8),  # outside 7-day window
        ]
        self._mock_jenkins_response(builds, monkeypatch)
        result = tools.get_jenkins_build_history("MyJob", since_days=7)
        assert "#10" in result
        assert "#9"  in result
        assert "#8"  not in result

    def test_since_days_excludes_old_builds(self, monkeypatch):
        builds = [
            self._make_build(5, "FAILURE", days_ago=10),
            self._make_build(4, "FAILURE", days_ago=20),
        ]
        self._mock_jenkins_response(builds, monkeypatch)
        result = tools.get_jenkins_build_history("MyJob", since_days=7)
        assert "No builds found" in result

    def test_since_days_includes_pass_fail_summary(self, monkeypatch):
        builds = [
            self._make_build(10, "SUCCESS", days_ago=1),
            self._make_build(9,  "SUCCESS", days_ago=2),
            self._make_build(8,  "FAILURE", days_ago=3),
        ]
        self._mock_jenkins_response(builds, monkeypatch)
        result = tools.get_jenkins_build_history("MyJob", since_days=7)
        assert "2 passed" in result
        assert "1 failed" in result

    def test_count_mode_no_summary(self, monkeypatch):
        builds = [self._make_build(10, "SUCCESS", days_ago=1)]
        self._mock_jenkins_response(builds, monkeypatch)
        result = tools.get_jenkins_build_history("MyJob", count=5)
        # Count mode should NOT include a "passed / failed" summary line
        assert "passed" not in result
        assert "failed" not in result

    def test_404_returns_not_found_message(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        monkeypatch.setattr(tools.requests, "get", lambda *a, **kw: mock_resp)
        result = tools.get_jenkins_build_history("NoSuchJob", count=5)
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# query_jellyfin("week")
# ---------------------------------------------------------------------------

class TestQueryJellyfinWeek:
    @pytest.fixture(autouse=True)
    def patch_token(self, monkeypatch):
        monkeypatch.setattr(tools, "JELLYFIN_TOKEN", "fake-token")
        monkeypatch.setattr(tools, "JELLYFIN_URL", "http://localhost:8096")

    def _make_get_mock(self, users_resp, items_by_type: dict):
        """
        Return a requests.get mock that serves:
          - /Users → users_resp
          - /Users/{uid}/Items → items filtered by IncludeItemTypes param
        """
        def fake_get(url, headers=None, params=None, timeout=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.status_code = 200
            if url.endswith("/Users"):
                resp.json.return_value = users_resp
            elif "/Items" in url:
                item_type = (params or {}).get("IncludeItemTypes", "")
                resp.json.return_value = {"Items": items_by_type.get(item_type, [])}
            else:
                resp.json.return_value = {}
            return resp
        return fake_get

    def test_week_query_sends_min_date_param(self, monkeypatch):
        """Verify MinDateLastSaved is sent in the request params."""
        seen_params = {}

        def capturing_get(url, headers=None, params=None, timeout=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.status_code = 200
            if url.endswith("/Users"):
                resp.json.return_value = [{"Id": "uid1", "Name": "alice"}]
            else:
                seen_params.update(params or {})
                resp.json.return_value = {"Items": []}
            return resp

        monkeypatch.setattr(tools.requests, "get", capturing_get)
        tools.query_jellyfin("week")
        assert "MinDateLastSaved" in seen_params

    def test_week_lists_added_movies(self, monkeypatch):
        users = [{"Id": "uid1", "Name": "alice"}]
        items = {
            "Movie":      [{"Name": "Blade Runner", "ProductionYear": 1982, "Type": "Movie"}],
            "Series":     [],
            "MusicAlbum": [],
        }
        monkeypatch.setattr(tools.requests, "get", self._make_get_mock(users, items))
        result = tools.query_jellyfin("week")
        assert "Blade Runner" in result
        assert "Movies" in result

    def test_week_nothing_added(self, monkeypatch):
        users = [{"Id": "uid1", "Name": "alice"}]
        monkeypatch.setattr(
            tools.requests, "get",
            self._make_get_mock(users, {"Movie": [], "Series": [], "MusicAlbum": []}),
        )
        result = tools.query_jellyfin("week")
        assert "Nothing added" in result or "nothing" in result.lower() or result.strip()

    def test_jellyfin_no_token_returns_error(self, monkeypatch):
        monkeypatch.setattr(tools, "JELLYFIN_TOKEN", "")
        result = tools.query_jellyfin("week")
        assert "JELLYFIN_API_KEY" in result

    def test_unknown_query_type_returns_error(self, monkeypatch):
        result = tools.query_jellyfin("nonexistent_type")
        assert "Unknown" in result or "unknown" in result


# ---------------------------------------------------------------------------
# get_log_tail — error paths
# ---------------------------------------------------------------------------

class TestGetLogTail:
    def test_unknown_log_name_returns_helpful_error(self):
        result = tools.get_log_tail("this_log_does_not_exist")
        assert "Unknown log" in result

    def test_docker_log_called_for_docker_container(self, monkeypatch):
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", {"myapp"})
        monkeypatch.setattr(tools, "ALLOWED_FILE_LOGS", {})
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _make_subprocess_result(stdout="some log output")
        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        result = tools.get_log_tail("myapp", lines=10)
        assert any("docker" in str(cmd).lower() for cmd in calls)
        assert "myapp" in str(calls)

    def test_file_log_called_for_file_log(self, monkeypatch):
        monkeypatch.setattr(tools, "ALLOWED_FILE_LOGS", {"rip-video": "/var/log/rip.log"})
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", set())
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _make_subprocess_result(stdout="some log lines")
        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        result = tools.get_log_tail("rip-video", lines=20)
        assert any("tail" in str(cmd).lower() for cmd in calls)

    def test_lines_capped_at_200(self, monkeypatch):
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", {"myapp"})
        monkeypatch.setattr(tools, "ALLOWED_FILE_LOGS", {})
        captured_cmd = []
        def fake_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return _make_subprocess_result(stdout="output")
        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        tools.get_log_tail("myapp", lines=9999)
        assert "200" in captured_cmd


# ---------------------------------------------------------------------------
# get_service_status — error paths
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# manage_schedule — create / list / cancel
# ---------------------------------------------------------------------------

class TestManageSchedule:
    """
    Tests for the manage_schedule tool function.
    Uses tmp_db so every test gets a clean, isolated database.
    """

    def _future(self, minutes: float = 60) -> str:
        return (
            datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        ).isoformat()

    def _past(self, minutes: float = 60) -> str:
        return (
            datetime.datetime.now() - datetime.timedelta(minutes=minutes)
        ).isoformat()

    # --- list ---

    def test_list_empty_db(self, tmp_db):
        result = tools.manage_schedule("list")
        assert "No scheduled tasks" in result

    def test_list_shows_pending_tasks(self, tmp_db):
        tools.manage_schedule(
            "create",
            fire_at=self._future(),
            description="Morning report",
        )
        result = tools.manage_schedule("list")
        assert "Morning report" in result

    def test_list_shows_task_id(self, tmp_db):
        tools.manage_schedule("create", fire_at=self._future(), description="x")
        result = tools.manage_schedule("list")
        assert "#" in result

    def test_list_shows_recurrence_rule(self, tmp_db):
        tools.manage_schedule(
            "create",
            fire_at=self._future(),
            description="weekly thing",
            task_type="recurring",
            recurrence_rule="weekly:1",
        )
        result = tools.manage_schedule("list")
        assert "weekly:1" in result

    def test_list_does_not_show_done_tasks(self, tmp_db):
        import scheduler as sched
        tools.manage_schedule("create", fire_at=self._future(), description="done soon")
        tasks = sched.list_pending()
        sched.mark_done(tasks[0]["id"])
        result = tools.manage_schedule("list")
        assert "No scheduled tasks" in result

    # --- create ---

    def test_create_returns_task_id(self, tmp_db):
        result = tools.manage_schedule("create", fire_at=self._future(), description="t")
        assert "#" in result

    def test_create_missing_fire_at_returns_error(self, tmp_db):
        result = tools.manage_schedule("create", description="no time given")
        assert "fire_at" in result

    def test_create_one_shot_type_note(self, tmp_db):
        result = tools.manage_schedule(
            "create",
            fire_at=self._future(),
            description="once",
            task_type="one_shot",
        )
        assert "fires once" in result

    def test_create_condition_check_type_note(self, tmp_db):
        result = tools.manage_schedule(
            "create",
            fire_at=self._future(),
            description="check jenkins",
            task_type="condition_check",
            condition_pattern=r'"result":\s*"SUCCESS"',
            max_attempts=4,
            check_interval_minutes=5,
        )
        assert "4" in result   # max_attempts
        assert "5" in result   # interval

    def test_create_recurring_type_note(self, tmp_db):
        result = tools.manage_schedule(
            "create",
            fire_at=self._future(),
            description="weekly",
            task_type="recurring",
            recurrence_rule="weekly:1",
        )
        assert "weekly:1" in result

    def test_create_with_string_tool_calls(self, tmp_db):
        """tool_calls may arrive as a JSON string — must be accepted."""
        import json
        tc = json.dumps([{"tool": "get_service_status", "args": {}}])
        result = tools.manage_schedule(
            "create",
            fire_at=self._future(),
            description="string tc",
            tool_calls=tc,
        )
        assert "error" not in result.lower()
        assert "#" in result

    # --- cancel ---

    def test_cancel_valid_task(self, tmp_db):
        import scheduler as sched
        tools.manage_schedule("create", fire_at=self._future(), description="bye")
        task_id = sched.list_pending()[0]["id"]
        result = tools.manage_schedule("cancel", id=task_id)
        assert "cancelled" in result.lower()

    def test_cancel_removes_from_pending(self, tmp_db):
        import scheduler as sched
        tools.manage_schedule("create", fire_at=self._future(), description="bye")
        task_id = sched.list_pending()[0]["id"]
        tools.manage_schedule("cancel", id=task_id)
        assert sched.list_pending() == []

    def test_cancel_nonexistent_id(self, tmp_db):
        result = tools.manage_schedule("cancel", id=99999)
        assert "not found" in result.lower() or "already done" in result.lower()

    def test_cancel_missing_id_param(self, tmp_db):
        result = tools.manage_schedule("cancel")
        assert "id" in result.lower()

    # --- unknown action ---

    def test_unknown_action_returns_error(self, tmp_db):
        result = tools.manage_schedule("fly_to_the_moon")
        assert "unknown" in result.lower() or "Unknown" in result


class TestGetServiceStatus:
    def test_unknown_service_returns_helpful_error(self, monkeypatch):
        monkeypatch.setattr(tools, "ALLOWED_DOCKER_LOGS", {"jellyfin"})
        monkeypatch.setattr(tools, "ALLOWED_SYSTEMD_SERVICES", {"sunshine"})
        monkeypatch.setattr(tools, "ALL_SERVICES", ["jellyfin", "sunshine"])
        result = tools.get_service_status("not_a_real_service")
        assert "Unknown service" in result
        assert "jellyfin" in result or "sunshine" in result


# ---------------------------------------------------------------------------
# get_hardware_info
# ---------------------------------------------------------------------------

class TestHardwareInfo:
    """get_hardware_info() — mocked subprocess calls."""

    DMI_BASEBOARD = (
        "Handle 0x0002, DMI type 2, 15 bytes\n"
        "Base Board Information\n"
        "\tManufacturer: ASUSTeK COMPUTER INC.\n"
        "\tProduct Name: Z97-AR\n"
        "\tVersion: Rev 1.xx\n"
        "\tSerial Number: 150340699600104\n"
    )

    DMI_MEMORY = (
        "Handle 0x003B, DMI type 17, 40 bytes\n"
        "Memory Device\n"
        "\tSize: 16384 MB\n"
        "\tType: DDR3\n"
        "\tSpeed: 1600 MT/s\n"
        "Handle 0x003C, DMI type 17, 40 bytes\n"
        "Memory Device\n"
        "\tSize: 16384 MB\n"
        "\tType: DDR3\n"
        "\tSpeed: 1600 MT/s\n"
    )

    CPUINFO = (
        "processor\t: 0\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 1\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 2\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 3\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 4\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 5\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 6\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
        "processor\t: 7\n"
        "model name\t: Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz\n"
    )

    NVIDIA_SMI = "NVIDIA GeForce GTX 970, 4096 MiB, 580.126.09\n"

    LSBK = (
        "NAME SIZE TYPE MODEL MOUNTPOINT\n"
        "sda 931.5G disk SanDisk SSD PLUS\n"
        "sdb   3.7T disk Seagate ST4000DM004\n"
    )

    def _fake_run(self, cmd, *args, **kwargs):
        """Return canned output based on the command."""
        if "dmidecode" in cmd and "-t" in cmd:
            idx = cmd.index("-t")
            arg = cmd[idx + 1]
            if arg == "baseboard":
                return MagicMock(returncode=0, stdout=self.DMI_BASEBOARD, stderr="")
            elif arg == "memory":
                return MagicMock(returncode=0, stdout=self.DMI_MEMORY, stderr="")
        if "nvidia-smi" in cmd:
            return MagicMock(returncode=0, stdout=self.NVIDIA_SMI, stderr="")
        if "lsblk" in cmd:
            return MagicMock(returncode=0, stdout=self.LSBK, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_hardware_info_returns_all_sections(self, monkeypatch):
        monkeypatch.setattr(tools.subprocess, "run", self._fake_run)
        # Mock /proc/cpuinfo
        monkeypatch.setattr("builtins.open", lambda path, *a, **kw: (
            type("f", (), {
                "__enter__": lambda s: s,
                "__exit__": lambda *a: None,
                "__iter__": lambda s: iter(self.CPUINFO.splitlines(True)),
                "read": lambda s: self.CPUINFO,
            })()
        ))
        result = tools.get_hardware_info()
        assert "ASUSTeK" in result
        assert "Z97-AR" in result
        assert "i7-4790K" in result
        assert "8 cores" in result
        assert "GTX 970" in result
        assert "4096 MiB" in result
        assert "32 GB" in result  # 16384 + 16384 = 32768 MB = 32 GB
        assert "DDR3" in result
        assert "SanDisk" in result
        assert "Seagate" in result

    def test_hardware_info_graceful_on_missing_dmidecode(self, monkeypatch):
        def _fake_run_missing(cmd, *a, **kw):
            if "dmidecode" in cmd:
                raise FileNotFoundError("dmidecode not found")
            if "nvidia-smi" in cmd:
                return MagicMock(returncode=0, stdout=self.NVIDIA_SMI, stderr="")
            if "lsblk" in cmd:
                return MagicMock(returncode=0, stdout=self.LSBK, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(tools.subprocess, "run", _fake_run_missing)
        monkeypatch.setattr("builtins.open", lambda path, *a, **kw: (
            type("f", (), {
                "__enter__": lambda s: s,
                "__exit__": lambda *a: None,
                "__iter__": lambda s: iter(self.CPUINFO.splitlines(True)),
                "read": lambda s: self.CPUINFO,
            })()
        ))
        result = tools.get_hardware_info()
        assert "dmidecode not installed" in result or "dmidecode" in result
        assert "i7-4790K" in result  # CPU still works
        assert "GTX 970" in result   # GPU still works

    def test_hardware_info_graceful_on_nvidia_smi_failure(self, monkeypatch):
        def _fake_run_no_gpu(cmd, *a, **kw):
            if "nvidia-smi" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="error")
            if "dmidecode" in cmd and "-t" in cmd:
                idx = cmd.index("-t")
                arg = cmd[idx + 1]
                if arg == "baseboard":
                    return MagicMock(returncode=0, stdout=self.DMI_BASEBOARD, stderr="")
                elif arg == "memory":
                    return MagicMock(returncode=0, stdout=self.DMI_MEMORY, stderr="")
            if "lsblk" in cmd:
                return MagicMock(returncode=0, stdout=self.LSBK, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(tools.subprocess, "run", _fake_run_no_gpu)
        monkeypatch.setattr("builtins.open", lambda path, *a, **kw: (
            type("f", (), {
                "__enter__": lambda s: s,
                "__exit__": lambda *a: None,
                "__iter__": lambda s: iter(self.CPUINFO.splitlines(True)),
                "read": lambda s: self.CPUINFO,
            })()
        ))
        result = tools.get_hardware_info()
        assert "ASUSTeK" in result
        assert "Z97-AR" in result
        assert "i7-4790K" in result
        # Should still have motherboard, CPU, RAM info even without GPU
        assert "32 GB" in result

    def test_query_system_dispatches_hardware(self, monkeypatch):
        monkeypatch.setattr(tools.subprocess, "run", self._fake_run)
        monkeypatch.setattr("builtins.open", lambda path, *a, **kw: (
            type("f", (), {
                "__enter__": lambda s: s,
                "__exit__": lambda *a: None,
                "__iter__": lambda s: iter(self.CPUINFO.splitlines(True)),
                "read": lambda s: self.CPUINFO,
            })()
        ))
        result = tools.query_system("hardware")
        assert "ASUSTeK" in result
        assert "Z97-AR" in result
        assert "i7-4790K" in result
