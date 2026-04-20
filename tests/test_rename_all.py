"""
Tests for manage_files rename_all action.

rename_all renames every file in a directory to sequential generic names
in a single confirmed operation, preserving extensions. This is the
"reset identified media back to raw rip names for reprocessing" workflow.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """
    Isolated media tree:
        <tmp_path>/media/Shows/DS9 Season 5/
            Star Trek DS9 S05E01.mkv
            Star Trek DS9 S05E02.mkv
            Star Trek DS9 S05E03.mkv
        <tmp_path>/staging/
    """
    media = tmp_path / "media"
    staging = tmp_path / "staging"
    season_dir = media / "Shows" / "DS9 Season 5"
    season_dir.mkdir(parents=True)
    staging.mkdir()

    for i in range(1, 4):
        (season_dir / f"Star Trek DS9 S05E0{i}.mkv").write_bytes(b"x" * 10)

    monkeypatch.setattr(tools, "MEDIA_PATH", str(media))
    monkeypatch.setattr(tools, "STAGING_PATH", str(staging))

    return {
        "media": media,
        "staging": staging,
        "season_dir": season_dir,
    }


def ra(source, dest="", confirmed=False):
    return tools.manage_files(action="rename_all", source=source, dest=dest, confirmed=confirmed)


# ---------------------------------------------------------------------------
# Preview (confirmed=False)
# ---------------------------------------------------------------------------

class TestPreview:
    def test_preview_shows_original_names(self, sandbox):
        result = ra(str(sandbox["season_dir"]))
        assert "S05E01" in result
        assert "S05E02" in result
        assert "S05E03" in result

    def test_preview_shows_new_names(self, sandbox):
        result = ra(str(sandbox["season_dir"]))
        assert "rip_01.mkv" in result
        assert "rip_02.mkv" in result
        assert "rip_03.mkv" in result

    def test_preview_shows_arrow(self, sandbox):
        result = ra(str(sandbox["season_dir"]))
        assert "→" in result

    def test_preview_asks_for_confirmation(self, sandbox):
        result = ra(str(sandbox["season_dir"]))
        assert "yes" in result.lower() or "confirm" in result.lower()

    def test_preview_shows_file_count(self, sandbox):
        result = ra(str(sandbox["season_dir"]))
        assert "3" in result

    def test_preview_shows_directory_path(self, sandbox):
        result = ra(str(sandbox["season_dir"]))
        assert str(sandbox["season_dir"]) in result

    def test_custom_pattern_shown_in_preview(self, sandbox):
        result = ra(str(sandbox["season_dir"]), dest="episode_{n:03d}")
        assert "episode_001.mkv" in result
        assert "episode_002.mkv" in result


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------

class TestDryRunNeverMutates:
    def test_original_files_still_exist_after_preview(self, sandbox):
        ra(str(sandbox["season_dir"]), confirmed=False)
        assert (sandbox["season_dir"] / "Star Trek DS9 S05E01.mkv").exists()
        assert (sandbox["season_dir"] / "Star Trek DS9 S05E02.mkv").exists()
        assert (sandbox["season_dir"] / "Star Trek DS9 S05E03.mkv").exists()

    def test_new_names_not_created_after_preview(self, sandbox):
        ra(str(sandbox["season_dir"]), confirmed=False)
        assert not (sandbox["season_dir"] / "rip_01.mkv").exists()


# ---------------------------------------------------------------------------
# Confirmed execution
# ---------------------------------------------------------------------------

class TestConfirmedExecution:
    def test_confirmed_renames_all_files(self, sandbox):
        result = ra(str(sandbox["season_dir"]), confirmed=True)
        assert "✅" in result
        assert (sandbox["season_dir"] / "rip_01.mkv").exists()
        assert (sandbox["season_dir"] / "rip_02.mkv").exists()
        assert (sandbox["season_dir"] / "rip_03.mkv").exists()

    def test_confirmed_removes_original_names(self, sandbox):
        ra(str(sandbox["season_dir"]), confirmed=True)
        assert not (sandbox["season_dir"] / "Star Trek DS9 S05E01.mkv").exists()

    def test_confirmed_reports_count(self, sandbox):
        result = ra(str(sandbox["season_dir"]), confirmed=True)
        assert "3" in result

    def test_custom_pattern_applied(self, sandbox):
        ra(str(sandbox["season_dir"]), dest="ep_{n:03d}", confirmed=True)
        assert (sandbox["season_dir"] / "ep_001.mkv").exists()
        assert (sandbox["season_dir"] / "ep_002.mkv").exists()
        assert (sandbox["season_dir"] / "ep_003.mkv").exists()

    def test_extensions_preserved(self, sandbox, tmp_path):
        """Files with different extensions keep their own extension."""
        mixed_dir = sandbox["media"] / "Shows" / "Mixed"
        mixed_dir.mkdir()
        (mixed_dir / "show_S01E01.mkv").write_bytes(b"x")
        (mixed_dir / "show_S01E02.avi").write_bytes(b"x")
        ra(str(mixed_dir), confirmed=True)
        assert (mixed_dir / "rip_01.mkv").exists()
        assert (mixed_dir / "rip_02.avi").exists()

    def test_files_sorted_alphabetically(self, sandbox, tmp_path):
        """Numbering follows alphabetical sort order."""
        sorted_dir = sandbox["media"] / "Shows" / "Sorted"
        sorted_dir.mkdir()
        (sorted_dir / "zzz_last.mkv").write_bytes(b"x")
        (sorted_dir / "aaa_first.mkv").write_bytes(b"x")
        ra(str(sorted_dir), confirmed=True)
        # aaa_first → rip_01, zzz_last → rip_02
        assert (sorted_dir / "rip_01.mkv").exists()
        assert (sorted_dir / "rip_02.mkv").exists()
        assert not (sorted_dir / "aaa_first.mkv").exists()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrors:
    def test_source_is_file_not_dir(self, sandbox):
        f = sandbox["season_dir"] / "Star Trek DS9 S05E01.mkv"
        result = ra(str(f))
        assert "directory" in result.lower()

    def test_source_not_found(self, sandbox):
        result = ra(str(sandbox["media"] / "Shows" / "Ghost Season 1"))
        assert "not found" in result.lower()

    def test_source_outside_allowed_roots(self, sandbox, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.mkv").write_bytes(b"x")
        result = ra(str(outside))
        assert "not allowed" in result.lower()

    def test_empty_directory_returns_informative_message(self, sandbox):
        empty = sandbox["media"] / "Shows" / "EmptySeason"
        empty.mkdir()
        result = ra(str(empty))
        assert "no files" in result.lower()

    def test_pattern_without_n_produces_duplicates_rejected(self, sandbox):
        result = ra(str(sandbox["season_dir"]), dest="fixed_name")
        assert "duplicate" in result.lower() or "{n}" in result or "counter" in result.lower()

    def test_invalid_pattern_format_string(self, sandbox):
        result = ra(str(sandbox["season_dir"]), dest="{bad_key}")
        assert "invalid pattern" in result.lower() or "placeholder" in result.lower()

    def test_subdirectories_not_renamed(self, sandbox, tmp_path):
        """rename_all only touches files, not subdirectories."""
        parent = sandbox["media"] / "Shows" / "WithSubdir"
        parent.mkdir()
        subdir = parent / "extras"
        subdir.mkdir()
        (parent / "episode.mkv").write_bytes(b"x")
        ra(str(parent), confirmed=True)
        # Subdir must still exist with its original name
        assert subdir.exists()
