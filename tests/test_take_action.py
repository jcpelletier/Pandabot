"""
Tests for take_action — move / rename / delete with confirmation gate.

All tests use tmp_path for an isolated, real filesystem sandbox.
MEDIA_PATH and STAGING_PATH are monkeypatched to point at subdirs inside
tmp_path, so no real server files are ever touched.

Design contract:
  - confirmed=False must NEVER mutate the filesystem (preview only).
  - confirmed=True only executes when source exists and constraints pass.
  - All paths must stay within the allowed roots; escapes are rejected.
  - Operating on an allowed root itself is rejected.
  - rename dest must be a bare name (no path separators).
  - move requires the destination parent directory to exist.
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
    Build an isolated media tree and redirect tools.py globals.

        <tmp_path>/
            media/
                Movies/
                    Alien (1979)/
                        Alien (1979).mkv   (fake 100 bytes)
                    Blade Runner (1982)/
                        Blade Runner (1982).mkv
                Shows/
            staging/
                RawRip.iso               (fake 50 bytes)
    """
    media = tmp_path / "media"
    staging = tmp_path / "staging"

    alien_dir = media / "Movies" / "Alien (1979)"
    blade_dir = media / "Movies" / "Blade Runner (1982)"
    shows_dir = media / "Shows"
    alien_dir.mkdir(parents=True)
    blade_dir.mkdir(parents=True)
    shows_dir.mkdir(parents=True)
    staging.mkdir(parents=True)

    (alien_dir / "Alien (1979).mkv").write_bytes(b"x" * 100)
    (blade_dir / "Blade Runner (1982).mkv").write_bytes(b"x" * 80)
    (staging / "RawRip.iso").write_bytes(b"x" * 50)

    monkeypatch.setattr(tools, "MEDIA_PATH", str(media))
    monkeypatch.setattr(tools, "STAGING_PATH", str(staging))

    return {
        "media": media,
        "staging": staging,
        "alien_dir": alien_dir,
        "alien_mkv": alien_dir / "Alien (1979).mkv",
        "blade_dir": blade_dir,
        "blade_mkv": blade_dir / "Blade Runner (1982).mkv",
        "shows_dir": shows_dir,
        "raw_iso": staging / "RawRip.iso",
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def ta(action, source, dest="", confirmed=False):
    return tools.take_action(action=action, source=source, dest=dest, confirmed=confirmed)


# ---------------------------------------------------------------------------
# Path boundary enforcement
# ---------------------------------------------------------------------------

class TestPathBoundaries:
    def test_absolute_outside_roots_rejected(self, sandbox, tmp_path):
        outside = str(tmp_path / "outside.txt")
        open(outside, "w").close()
        result = ta("delete", outside)
        assert "not allowed" in result.lower()
        assert os.path.exists(outside)

    def test_relative_traversal_rejected(self, sandbox, tmp_path):
        # A relative path that would escape media root via ../..
        result = ta("delete", "../../etc/passwd")
        assert "not allowed" in result.lower() or "not found" in result.lower()

    def test_symlink_escape_rejected(self, sandbox, tmp_path):
        # Create a symlink inside media pointing outside
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret")
        symlink = sandbox["media"] / "Movies" / "escape_link"
        try:
            symlink.symlink_to(outside_dir)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        result = ta("delete", str(symlink / "secret.txt"))
        assert "not allowed" in result.lower()

    def test_staging_path_is_allowed(self, sandbox):
        result = ta("delete", str(sandbox["raw_iso"]))
        assert "cannot be undone" in result.lower() or "ready to" in result.lower()

    def test_media_path_root_rejected(self, sandbox):
        result = ta("delete", str(sandbox["media"]))
        assert "root" in result.lower() or "cannot operate" in result.lower()

    def test_staging_root_rejected(self, sandbox):
        result = ta("delete", str(sandbox["staging"]))
        assert "root" in result.lower() or "cannot operate" in result.lower()


# ---------------------------------------------------------------------------
# Source not found
# ---------------------------------------------------------------------------

class TestSourceNotFound:
    def test_nonexistent_file_returns_not_found(self, sandbox):
        result = ta("delete", str(sandbox["media"] / "Movies" / "Ghost.mkv"))
        assert "not found" in result.lower()

    def test_nonexistent_path_never_deletes(self, sandbox):
        result = ta("delete", str(sandbox["media"] / "Movies" / "Ghost.mkv"), confirmed=True)
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# confirmed=False never mutates
# ---------------------------------------------------------------------------

class TestDryRunNeverMutates:
    def test_delete_file_dry_run_file_still_exists(self, sandbox):
        ta("delete", str(sandbox["alien_mkv"]), confirmed=False)
        assert sandbox["alien_mkv"].exists()

    def test_delete_dir_dry_run_dir_still_exists(self, sandbox):
        ta("delete", str(sandbox["alien_dir"]), confirmed=False)
        assert sandbox["alien_dir"].exists()

    def test_rename_dry_run_original_unchanged(self, sandbox):
        ta("rename", str(sandbox["alien_mkv"]), dest="NewName.mkv", confirmed=False)
        assert sandbox["alien_mkv"].exists()
        assert not (sandbox["alien_dir"] / "NewName.mkv").exists()

    def test_move_dry_run_source_unchanged(self, sandbox):
        ta("move", str(sandbox["alien_mkv"]), dest=str(sandbox["shows_dir"]), confirmed=False)
        assert sandbox["alien_mkv"].exists()
        assert not (sandbox["shows_dir"] / "Alien (1979).mkv").exists()


# ---------------------------------------------------------------------------
# delete — file
# ---------------------------------------------------------------------------

class TestDeleteFile:
    def test_preview_mentions_filename(self, sandbox):
        result = ta("delete", str(sandbox["alien_mkv"]))
        assert "Alien (1979).mkv" in result

    def test_preview_shows_size(self, sandbox):
        result = ta("delete", str(sandbox["alien_mkv"]))
        # 100 bytes shows some byte representation
        assert "100" in result or "B" in result

    def test_preview_warns_cannot_be_undone(self, sandbox):
        result = ta("delete", str(sandbox["alien_mkv"]))
        assert "cannot be undone" in result.lower() or "⚠️" in result

    def test_confirmed_deletes_file(self, sandbox):
        result = ta("delete", str(sandbox["alien_mkv"]), confirmed=True)
        assert "✅" in result
        assert not sandbox["alien_mkv"].exists()

    def test_confirmed_returns_success_message(self, sandbox):
        result = ta("delete", str(sandbox["alien_mkv"]), confirmed=True)
        assert "deleted" in result.lower() or "✅" in result


# ---------------------------------------------------------------------------
# delete — directory
# ---------------------------------------------------------------------------

class TestDeleteDirectory:
    def test_preview_shows_file_manifest(self, sandbox):
        result = ta("delete", str(sandbox["alien_dir"]))
        assert "Alien (1979).mkv" in result

    def test_preview_shows_file_count_or_total(self, sandbox):
        result = ta("delete", str(sandbox["alien_dir"]))
        assert "1 file" in result or "total" in result.lower()

    def test_confirmed_removes_directory_tree(self, sandbox):
        result = ta("delete", str(sandbox["alien_dir"]), confirmed=True)
        assert "✅" in result
        assert not sandbox["alien_dir"].exists()

    def test_manifest_truncates_at_20(self, sandbox, tmp_path):
        # Create a directory with 25 files
        many_dir = sandbox["media"] / "Movies" / "ManyFiles"
        many_dir.mkdir()
        for i in range(25):
            (many_dir / f"file_{i:03}.txt").write_bytes(b"x")
        result = ta("delete", str(many_dir))
        assert "and" in result and "more" in result


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

class TestRename:
    def test_preview_shows_old_and_new_name(self, sandbox):
        result = ta("rename", str(sandbox["alien_mkv"]), dest="Alien.mkv")
        assert "Alien (1979).mkv" in result
        assert "Alien.mkv" in result

    def test_preview_asks_for_confirmation(self, sandbox):
        result = ta("rename", str(sandbox["alien_mkv"]), dest="Alien.mkv")
        assert "yes" in result.lower() or "confirm" in result.lower()

    def test_confirmed_renames_file(self, sandbox):
        result = ta("rename", str(sandbox["alien_mkv"]), dest="Alien.mkv", confirmed=True)
        assert "✅" in result
        assert not sandbox["alien_mkv"].exists()
        assert (sandbox["alien_dir"] / "Alien.mkv").exists()

    def test_rename_directory(self, sandbox):
        new_name = "Alien 1979"
        result = ta("rename", str(sandbox["alien_dir"]), dest=new_name, confirmed=True)
        assert "✅" in result
        assert not sandbox["alien_dir"].exists()
        assert (sandbox["media"] / "Movies" / new_name).exists()

    def test_path_separator_in_dest_rejected(self, sandbox):
        result = ta("rename", str(sandbox["alien_mkv"]), dest="Movies/Alien.mkv")
        assert "path separator" in result.lower() or "bare name" in result.lower() or "no path" in result.lower()

    def test_backslash_in_dest_rejected(self, sandbox):
        result = ta("rename", str(sandbox["alien_mkv"]), dest="sub\\Alien.mkv")
        assert "path separator" in result.lower() or "bare name" in result.lower() or "no path" in result.lower()

    def test_collision_rejected(self, sandbox):
        # Blade Runner is in the same directory (Movies), but alien is in alien_dir
        # Create a collision in the same folder
        (sandbox["alien_dir"] / "collision.mkv").write_bytes(b"x")
        result = ta("rename", str(sandbox["alien_mkv"]), dest="collision.mkv", confirmed=True)
        assert "exists" in result.lower() or "cannot rename" in result.lower()
        # Original must be unchanged
        assert sandbox["alien_mkv"].exists()

    def test_missing_dest_returns_error(self, sandbox):
        result = ta("rename", str(sandbox["alien_mkv"]), dest="")
        assert "requires dest" in result.lower() or "rename requires" in result.lower()


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------

class TestMove:
    def test_preview_shows_source_and_dest(self, sandbox):
        result = ta("move", str(sandbox["alien_mkv"]), dest=str(sandbox["shows_dir"]))
        assert "Alien (1979).mkv" in result
        assert str(sandbox["shows_dir"]) in result

    def test_preview_asks_for_confirmation(self, sandbox):
        result = ta("move", str(sandbox["alien_mkv"]), dest=str(sandbox["shows_dir"]))
        assert "yes" in result.lower() or "confirm" in result.lower()

    def test_confirmed_moves_file_into_directory(self, sandbox):
        result = ta("move", str(sandbox["alien_mkv"]), dest=str(sandbox["shows_dir"]), confirmed=True)
        assert "✅" in result
        assert not sandbox["alien_mkv"].exists()
        assert (sandbox["shows_dir"] / "Alien (1979).mkv").exists()

    def test_confirmed_moves_directory(self, sandbox):
        result = ta("move", str(sandbox["alien_dir"]), dest=str(sandbox["shows_dir"]), confirmed=True)
        assert "✅" in result
        assert not sandbox["alien_dir"].exists()
        assert (sandbox["shows_dir"] / "Alien (1979)").exists()

    def test_collision_rejected(self, sandbox):
        # Copy the file to shows_dir first to create a collision
        import shutil
        shutil.copy(str(sandbox["alien_mkv"]), str(sandbox["shows_dir"] / "Alien (1979).mkv"))
        result = ta("move", str(sandbox["alien_mkv"]), dest=str(sandbox["shows_dir"]), confirmed=True)
        assert "exists" in result.lower() or "cannot move" in result.lower()
        assert sandbox["alien_mkv"].exists()

    def test_missing_parent_rejected(self, sandbox):
        result = ta("move", str(sandbox["alien_mkv"]),
                    dest=str(sandbox["media"] / "NonExistentDir" / "Alien.mkv"), confirmed=True)
        assert "parent" in result.lower() or "does not exist" in result.lower()

    def test_dest_outside_roots_rejected(self, sandbox, tmp_path):
        outside = str(tmp_path / "outside")
        os.makedirs(outside, exist_ok=True)
        result = ta("move", str(sandbox["alien_mkv"]), dest=outside, confirmed=True)
        assert "not allowed" in result.lower()

    def test_missing_dest_returns_error(self, sandbox):
        result = ta("move", str(sandbox["alien_mkv"]), dest="")
        assert "requires dest" in result.lower() or "move requires" in result.lower()

    def test_move_across_roots(self, sandbox):
        """Moving from media to staging should be allowed (both are allowed roots)."""
        result = ta("move", str(sandbox["alien_mkv"]), dest=str(sandbox["staging"]), confirmed=True)
        assert "✅" in result
        assert (sandbox["staging"] / "Alien (1979).mkv").exists()


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------

class TestUnknownAction:
    def test_unknown_action_returns_error(self, sandbox):
        result = ta("copy", str(sandbox["alien_mkv"]))
        assert "unknown action" in result.lower()
