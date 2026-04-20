"""
Tests for manage_files delete_matching action.

delete_matching recursively deletes all files under a directory that match
one or more glob patterns, in a single confirmed operation.
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
        <tmp_path>/media/Video/Season 05/
            episode_01.mkv
            episode_01.eng.srt
            episode_01.fra.srt
            episode_02.mkv
            episode_02.eng.srt
            Extras/
                extra_01.mkv
                extra_01.eng.srt
        <tmp_path>/staging/
    """
    media = tmp_path / "media"
    staging = tmp_path / "staging"
    season = media / "Video" / "Season 05"
    extras = season / "Extras"
    season.mkdir(parents=True)
    extras.mkdir()
    staging.mkdir()

    (season / "episode_01.mkv").write_bytes(b"v" * 100)
    (season / "episode_01.eng.srt").write_text("subtitle1")
    (season / "episode_01.fra.srt").write_text("subtitle1fr")
    (season / "episode_02.mkv").write_bytes(b"v" * 100)
    (season / "episode_02.eng.srt").write_text("subtitle2")
    (extras / "extra_01.mkv").write_bytes(b"v" * 50)
    (extras / "extra_01.eng.srt").write_text("extrasub")

    monkeypatch.setattr(tools, "MEDIA_PATH", str(media))
    monkeypatch.setattr(tools, "STAGING_PATH", str(staging))

    return {"media": media, "staging": staging, "season": season, "extras": extras}


def dm(source, dest, confirmed=False):
    return tools.manage_files(action="delete_matching", source=source, dest=dest, confirmed=confirmed)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

class TestPreview:
    def test_preview_shows_source_directory(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt")
        assert str(sandbox["season"]) in result

    def test_preview_shows_file_count(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt")
        # 3 top-level + 1 in Extras = 4
        assert "4" in result

    def test_preview_shows_total_size(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt")
        assert "total" in result.lower() or "B" in result

    def test_preview_shows_subdirectory_name(self, sandbox):
        # Extras subdir should appear by name in the grouped breakdown
        result = dm(str(sandbox["season"]), "*.srt")
        assert "Extras" in result

    def test_preview_shows_per_dir_count(self, sandbox):
        # Top-level has 3 srt files, Extras has 1
        result = dm(str(sandbox["season"]), "*.srt")
        assert "3" in result  # top-level count
        assert "1" in result  # Extras count

    def test_preview_warns_cannot_be_undone(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt")
        assert "cannot be undone" in result.lower() or "⚠️" in result

    def test_preview_asks_for_confirmation(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt")
        assert "yes" in result.lower() or "confirm" in result.lower()

    def test_no_match_returns_informative_message(self, sandbox):
        result = dm(str(sandbox["season"]), "*.sup")
        assert "no files" in result.lower()

    def test_multi_pattern_preview_shows_both_counts(self, sandbox):
        # 3 srt + 2 mkv in top-level, 1 srt + 1 mkv in Extras = 7 total
        result = dm(str(sandbox["season"]), "*.srt,*.mkv")
        assert "7" in result


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------

class TestDryRunNeverMutates:
    def test_srt_files_untouched_after_preview(self, sandbox):
        dm(str(sandbox["season"]), "*.srt", confirmed=False)
        assert (sandbox["season"] / "episode_01.eng.srt").exists()
        assert (sandbox["extras"] / "extra_01.eng.srt").exists()

    def test_video_files_untouched_after_preview(self, sandbox):
        dm(str(sandbox["season"]), "*.srt", confirmed=False)
        assert (sandbox["season"] / "episode_01.mkv").exists()


# ---------------------------------------------------------------------------
# Confirmed execution
# ---------------------------------------------------------------------------

class TestConfirmedExecution:
    def test_deletes_all_matching_files(self, sandbox):
        dm(str(sandbox["season"]), "*.srt", confirmed=True)
        assert not (sandbox["season"] / "episode_01.eng.srt").exists()
        assert not (sandbox["season"] / "episode_01.fra.srt").exists()
        assert not (sandbox["season"] / "episode_02.eng.srt").exists()
        assert not (sandbox["extras"] / "extra_01.eng.srt").exists()

    def test_video_files_preserved(self, sandbox):
        dm(str(sandbox["season"]), "*.srt", confirmed=True)
        assert (sandbox["season"] / "episode_01.mkv").exists()
        assert (sandbox["season"] / "episode_02.mkv").exists()
        assert (sandbox["extras"] / "extra_01.mkv").exists()

    def test_confirmed_reports_count(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt", confirmed=True)
        assert "4" in result
        assert "✅" in result

    def test_multi_pattern_deletes_both_types(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt,*.mkv", confirmed=True)
        assert "✅" in result
        assert not (sandbox["season"] / "episode_01.mkv").exists()
        assert not (sandbox["season"] / "episode_01.eng.srt").exists()

    def test_subdirectories_themselves_not_deleted(self, sandbox):
        dm(str(sandbox["season"]), "*.srt", confirmed=True)
        assert sandbox["extras"].exists()

    def test_returns_success_message_with_pattern(self, sandbox):
        result = dm(str(sandbox["season"]), "*.srt", confirmed=True)
        assert "*.srt" in result or "srt" in result.lower()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrors:
    def test_source_is_file_not_dir(self, sandbox):
        f = sandbox["season"] / "episode_01.mkv"
        result = dm(str(f), "*.srt")
        assert "directory" in result.lower()

    def test_source_not_found(self, sandbox):
        result = dm(str(sandbox["media"] / "Ghost"), "*.srt")
        assert "not found" in result.lower()

    def test_missing_pattern_returns_error(self, sandbox):
        result = dm(str(sandbox["season"]), "")
        assert "requires dest" in result.lower() or "pattern" in result.lower()

    def test_source_outside_roots_rejected(self, sandbox, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        result = dm(str(outside), "*.srt")
        assert "not allowed" in result.lower()
