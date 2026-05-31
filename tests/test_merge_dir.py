"""
Tests for manage_files merge action.

merge moves all files from a source directory into an existing destination
directory in one operation, then removes the (now-empty) source folder.
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
        <tmp_path>/media/Music/
            Artist/
                Album [Disc 1]/
                    01 - Track One.flac
                    02 - Track Two.flac
                    03 - Track Three.flac
                Album [Disc 2]/
                    04 - Track Four.flac
                    05 - Track Five.flac
                Best Of/
                    bonus.flac
        <tmp_path>/staging/
    """
    media = tmp_path / "media"
    staging = tmp_path / "staging"
    artist = media / "Music" / "Artist"
    disc1 = artist / "Album [Disc 1]"
    disc2 = artist / "Album [Disc 2]"
    best_of = artist / "Best Of"

    for d in (disc1, disc2, best_of, staging):
        d.mkdir(parents=True)

    (disc1 / "01 - Track One.flac").write_bytes(b"a" * 100)
    (disc1 / "02 - Track Two.flac").write_bytes(b"b" * 100)
    (disc1 / "03 - Track Three.flac").write_bytes(b"c" * 100)
    (disc2 / "04 - Track Four.flac").write_bytes(b"d" * 100)
    (disc2 / "05 - Track Five.flac").write_bytes(b"e" * 100)
    (best_of / "bonus.flac").write_bytes(b"f" * 100)

    monkeypatch.setattr(tools, "MEDIA_PATH", str(media))
    monkeypatch.setattr(tools, "STAGING_PATH", str(staging))

    return {
        "media": media,
        "staging": staging,
        "artist": artist,
        "disc1": disc1,
        "disc2": disc2,
        "best_of": best_of,
    }


def mg(source, dest, confirmed=False):
    return tools.manage_files(action="merge", source=source, dest=dest, confirmed=confirmed)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

class TestPreview:
    def test_preview_shows_source_and_dest(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert str(sandbox["disc2"]) in result
        assert str(sandbox["disc1"]) in result

    def test_preview_shows_file_count(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert "2" in result

    def test_preview_shows_filenames(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert "04 - Track Four.flac" in result

    def test_preview_mentions_source_removal(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert "removed" in result.lower() or "remove" in result.lower()

    def test_preview_asks_for_confirmation(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert "yes" in result.lower() or "confirm" in result.lower()

    def test_preview_with_conflict_shows_warning(self, sandbox):
        # Create a file in disc1 with the same name as one in disc2
        (sandbox["disc1"] / "04 - Track Four.flac").write_bytes(b"conflict")
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert "conflict" in result.lower()
        assert "04 - Track Four.flac" in result

    def test_preview_with_conflict_does_not_ask_to_confirm(self, sandbox):
        (sandbox["disc1"] / "04 - Track Four.flac").write_bytes(b"conflict")
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]))
        assert "yes" not in result.lower()


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------

class TestDryRunNeverMutates:
    def test_source_files_untouched_after_preview(self, sandbox):
        mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=False)
        assert (sandbox["disc2"] / "04 - Track Four.flac").exists()
        assert (sandbox["disc2"] / "05 - Track Five.flac").exists()

    def test_source_dir_still_exists_after_preview(self, sandbox):
        mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=False)
        assert sandbox["disc2"].exists()

    def test_dest_files_untouched_after_preview(self, sandbox):
        mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=False)
        assert (sandbox["disc1"] / "01 - Track One.flac").exists()


# ---------------------------------------------------------------------------
# Confirmed execution
# ---------------------------------------------------------------------------

class TestConfirmedExecution:
    def test_files_appear_in_dest(self, sandbox):
        mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=True)
        assert (sandbox["disc1"] / "04 - Track Four.flac").exists()
        assert (sandbox["disc1"] / "05 - Track Five.flac").exists()

    def test_source_dir_removed(self, sandbox):
        mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=True)
        assert not sandbox["disc2"].exists()

    def test_original_dest_files_preserved(self, sandbox):
        mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=True)
        assert (sandbox["disc1"] / "01 - Track One.flac").exists()
        assert (sandbox["disc1"] / "02 - Track Two.flac").exists()
        assert (sandbox["disc1"] / "03 - Track Three.flac").exists()

    def test_confirmed_returns_success(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=True)
        assert "✅" in result
        assert "2" in result

    def test_confirmed_with_conflict_is_blocked(self, sandbox):
        (sandbox["disc1"] / "04 - Track Four.flac").write_bytes(b"conflict")
        result = mg(str(sandbox["disc2"]), str(sandbox["disc1"]), confirmed=True)
        assert "conflict" in result.lower()
        assert "✅" not in result
        # Source must not have been partially merged
        assert (sandbox["disc2"] / "04 - Track Four.flac").exists()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrors:
    def test_source_is_file_not_dir(self, sandbox):
        f = sandbox["disc1"] / "01 - Track One.flac"
        result = mg(str(f), str(sandbox["disc2"]))
        assert "directory" in result.lower()

    def test_dest_does_not_exist(self, sandbox):
        result = mg(str(sandbox["disc2"]), str(sandbox["artist"] / "Ghost"))
        assert "existing directory" in result.lower() or "must be an existing" in result.lower()

    def test_dest_is_a_file(self, sandbox):
        f = sandbox["disc1"] / "01 - Track One.flac"
        result = mg(str(sandbox["disc2"]), str(f))
        assert "directory" in result.lower()

    def test_missing_dest_returns_error(self, sandbox):
        result = mg(str(sandbox["disc2"]), "")
        assert "requires dest" in result.lower()

    def test_source_outside_roots_rejected(self, sandbox, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        result = mg(str(outside), str(sandbox["disc1"]))
        assert "not allowed" in result.lower()

    def test_dest_outside_roots_rejected(self, sandbox, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        result = mg(str(sandbox["disc2"]), str(outside))
        assert "not allowed" in result.lower()

    def test_source_not_found(self, sandbox):
        result = mg(str(sandbox["artist"] / "Nonexistent"), str(sandbox["disc1"]))
        assert "not found" in result.lower()

    def test_same_source_and_dest_rejected(self, sandbox):
        result = mg(str(sandbox["disc1"]), str(sandbox["disc1"]))
        assert "same" in result.lower()
