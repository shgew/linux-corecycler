"""config.paths sudo identity + ownership repair.

Under sudo, persistent state must resolve to the INVOKING user (never /root),
and root must never leave the files it created owned by root -- but the repair
must never raise, since a root-owned file beats losing the write.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from corecycler.config import paths


class TestAtomicWrite:
    def test_does_not_follow_a_preplanted_temporary_symlink(self, tmp_path, monkeypatch):
        destination = tmp_path / "settings.json"
        victim = tmp_path / "victim"
        victim.write_text("unchanged")
        planted = tmp_path / ".settings.json.planted.tmp"
        planted.symlink_to(victim)
        tokens = iter(["planted", "safe"])
        monkeypatch.setattr(paths.secrets, "token_hex", lambda _length: next(tokens))

        paths.atomic_write(destination, "saved")

        assert destination.read_text() == "saved"
        assert victim.read_text() == "unchanged"

    def test_refuses_when_every_temporary_name_is_preplanted(self, tmp_path, monkeypatch):
        destination = tmp_path / "settings.json"
        victim = tmp_path / "victim"
        victim.write_text("unchanged")
        planted = tmp_path / ".settings.json.planted.tmp"
        planted.symlink_to(victim)
        monkeypatch.setattr(paths.secrets, "token_hex", lambda _length: "planted")

        with pytest.raises(FileExistsError, match="cannot create a temporary file"):
            paths.atomic_write(destination, "saved")

        assert victim.read_text() == "unchanged"
        assert planted.is_symlink()

    def test_removes_a_temporary_symlink_swapped_in_before_replace(self, tmp_path, monkeypatch):
        destination = tmp_path / "settings.json"
        victim = tmp_path / "victim"
        victim.write_text("unchanged")
        temporary = tmp_path / ".settings.json.fixed.tmp"
        real_fstat = os.fstat

        def swap_for_symlink(fd):
            opened = real_fstat(fd)
            temporary.unlink()
            temporary.symlink_to(victim)
            return opened

        monkeypatch.setattr(paths.secrets, "token_hex", lambda _length: "fixed")
        monkeypatch.setattr(paths.os, "fstat", swap_for_symlink)

        with pytest.raises(OSError, match="temporary file changed"):
            paths.atomic_write(destination, "saved")

        assert victim.read_text() == "unchanged"
        assert not temporary.exists()

    def test_removes_temporary_file_when_replace_fails(self, tmp_path, monkeypatch):
        destination = tmp_path / "settings.json"
        monkeypatch.setattr(paths.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))

        with pytest.raises(OSError, match="replace failed"):
            paths.atomic_write(destination, "saved")

        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_durable_write_fsyncs_file_before_replace_and_parent_after(self, tmp_path, monkeypatch):
        destination = tmp_path / "state"
        events = []
        directory_fd = 12345
        real_open = os.open
        real_close = os.close
        real_replace = os.replace

        def replace(source, target):
            events.append("replace")
            return real_replace(source, target)

        def open_file(path, flags, mode=0o777):
            if flags == os.O_RDONLY | os.O_DIRECTORY:
                assert path == tmp_path
                events.append("open parent")
                return directory_fd
            return real_open(path, flags, mode)

        def fsync(fd):
            events.append("fsync parent" if fd == directory_fd else "fsync temporary")

        def close(fd):
            if fd == directory_fd:
                events.append("close parent")
            else:
                real_close(fd)

        monkeypatch.setattr(paths.os, "replace", replace)
        monkeypatch.setattr(paths.os, "open", open_file)
        monkeypatch.setattr(paths.os, "fsync", fsync)
        monkeypatch.setattr(paths.os, "close", close)

        paths.atomic_write(destination, "saved", durable=True)

        assert destination.read_text(encoding="utf-8") == "saved"
        assert events == ["fsync temporary", "replace", "open parent", "fsync parent", "close parent"]


class TestEnsureDirectory:
    def test_refuses_a_symlink_in_a_root_sudo_path(self, tmp_path, monkeypatch):
        victim = tmp_path / "victim"
        victim.mkdir()
        link = tmp_path / "link"
        link.symlink_to(victim, target_is_directory=True)
        monkeypatch.setattr(paths.os, "geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_UID", "1000")

        with pytest.raises(OSError, match="refusing symlink"):
            paths.ensure_directory(link / "state")

        assert not (victim / "state").exists()

    def test_refuses_an_existing_file_as_a_directory(self, tmp_path):
        target = tmp_path / "file"
        target.write_text("not a directory")

        with pytest.raises(NotADirectoryError):
            paths.ensure_directory(target)

    def test_accepts_a_directory_created_by_a_racing_process(self, tmp_path, monkeypatch):
        target = tmp_path / "state"
        real_mkdir = os.mkdir

        def race(directory):
            real_mkdir(directory)
            raise FileExistsError

        monkeypatch.setattr(paths.os, "mkdir", race)

        assert paths.ensure_directory(target) == target
        assert target.is_dir()

    def test_refuses_a_file_created_by_a_racing_process(self, tmp_path, monkeypatch):
        target = tmp_path / "state"

        def race(directory):
            Path(directory).write_text("not a directory")
            raise FileExistsError

        monkeypatch.setattr(paths.os, "mkdir", race)

        with pytest.raises(NotADirectoryError):
            paths.ensure_directory(target)


class TestUserHome:
    def test_root_with_only_sudo_uid_resolves_invoker(self, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.delenv("SUDO_USER", raising=False)
        fake = type("E", (), {"pw_dir": "/home/alice"})()
        with patch("os.geteuid", return_value=0), patch("pwd.getpwuid", return_value=fake):
            assert paths.user_home() == Path("/home/alice")

    def test_sudo_uid_wins_over_a_stale_sudo_user(self, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_USER", "ghost")
        fake = type("E", (), {"pw_dir": "/home/alice"})()
        with patch("os.geteuid", return_value=0), patch("pwd.getpwuid", return_value=fake):
            assert paths.user_home() == Path("/home/alice")

    def test_missing_sudo_uid_entry_falls_back_to_sudo_user(self, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_USER", "alice")
        fake = type("E", (), {"pw_dir": "/home/alice"})()
        with (
            patch("os.geteuid", return_value=0),
            patch("pwd.getpwuid", side_effect=KeyError),
            patch("pwd.getpwnam", return_value=fake),
        ):
            assert paths.user_home() == Path("/home/alice")

    def test_non_root_uses_invoker_home(self):
        with patch("os.geteuid", return_value=1000), patch("pathlib.Path.home", return_value=Path("/home/inv")):
            assert paths.user_home() == Path("/home/inv")

    def test_root_with_sudo_user_resolves_invoker(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        fake = type("E", (), {"pw_dir": "/home/alice"})()
        with patch("os.geteuid", return_value=0), patch("pwd.getpwnam", return_value=fake):
            assert paths.user_home() == Path("/home/alice")

    def test_root_sudo_user_root_falls_back(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "root")
        with patch("os.geteuid", return_value=0), patch("pathlib.Path.home", return_value=Path("/home/inv")):
            assert paths.user_home() == Path("/home/inv")

    def test_root_no_sudo_user_falls_back(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        with patch("os.geteuid", return_value=0), patch("pathlib.Path.home", return_value=Path("/home/inv")):
            assert paths.user_home() == Path("/home/inv")

    def test_root_stale_sudo_user_falls_back(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "ghost")
        with (
            patch("os.geteuid", return_value=0),
            patch("pwd.getpwnam", side_effect=KeyError),
            patch("pathlib.Path.home", return_value=Path("/home/inv")),
        ):
            assert paths.user_home() == Path("/home/inv")


class TestFixSudoOwnership:
    def test_non_root_is_noop(self):
        with patch("os.geteuid", return_value=1000), patch("os.chown") as chown:
            paths.fix_sudo_ownership(Path("/tmp/x"))
        chown.assert_not_called()

    def test_root_does_not_chown_preexisting_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        target = tmp_path / "db"
        target.write_text("x")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(target)
        chown.assert_not_called()

    def test_created_directory_is_repaired_without_following_symlinks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        target = tmp_path / "deep" / "state"
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.ensure_directory(target)
        assert target.is_dir()
        assert chown.call_count == 2
        assert all(call.kwargs == {"follow_symlinks": False} for call in chown.call_args_list)

    def test_symlink_is_never_chowned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        victim = tmp_path / "victim"
        victim.write_text("safe")
        link = tmp_path / "link"
        link.symlink_to(victim)
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(link)
        chown.assert_not_called()

    def test_created_inode_replaced_by_a_symlink_is_never_chowned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        target = tmp_path / "db"
        target.write_text("created")
        paths._record_created(target)
        target.unlink()
        victim = tmp_path / "victim"
        victim.write_text("safe")
        target.symlink_to(victim)

        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(target)

        chown.assert_not_called()
        assert victim.read_text() == "safe"

    def test_non_digit_uid_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "notnum")
        monkeypatch.setenv("SUDO_GID", "1000")
        f = tmp_path / "db"
        f.write_text("x")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(f)
        chown.assert_not_called()

    def test_missing_path_not_chowned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(tmp_path / "nope")
        chown.assert_not_called()

    def test_chown_oserror_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        f = tmp_path / "db"
        f.write_text("x")
        paths._record_created(f)
        with patch("os.geteuid", return_value=0), patch("os.chown", side_effect=OSError):
            paths.fix_sudo_ownership(f)

    def test_external_creation_tracker_never_follows_a_replacement_symlink(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        target = tmp_path / "tracked"
        missing = tmp_path / "missing"
        repair = paths.track_created_paths(target, missing)
        victim = tmp_path / "victim"
        victim.write_text("safe")
        target.symlink_to(victim)

        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            repair()

        chown.assert_not_called()
        assert victim.read_text() == "safe"
