"""Filesystem identity and secure persistent state paths."""

from __future__ import annotations

import contextlib
import os
import pwd
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_CREATED_INODES: dict[Path, tuple[int, int]] = {}


def user_home() -> Path:
    """Return the home directory of the invoking user."""
    if os.geteuid() == 0:
        sudo_uid = os.environ.get("SUDO_UID", "")
        if sudo_uid.isdigit() and int(sudo_uid) != 0:
            with contextlib.suppress(KeyError):
                return Path(pwd.getpwuid(int(sudo_uid)).pw_dir)
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and sudo_user != "root":
            with contextlib.suppress(KeyError):
                return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def _key(path: Path) -> Path:
    return path.absolute()


def _record_created(path: Path, details: os.stat_result | None = None) -> None:
    details = details or path.lstat()
    _CREATED_INODES[_key(path)] = (details.st_dev, details.st_ino)


def _validate_no_symlinks(path: Path) -> None:
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID", "").isdigit():
        return
    current = Path(path.anchor)
    for part in path.absolute().parts[1:]:
        current /= part
        try:
            details = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(details.st_mode):
            raise OSError(f"refusing symlink in application path: {current}")


def track_created_paths(*paths: Path) -> Callable[[], None]:
    """Return an ownership repair callback limited to paths absent now."""
    missing: list[Path] = []
    for path in paths:
        path = path.absolute()
        _validate_no_symlinks(path)
        try:
            path.lstat()
        except FileNotFoundError:
            missing.append(path)

    created: list[Path] = []

    def repair() -> None:
        for path in missing:
            if path in created:
                continue
            try:
                details = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(details.st_mode):
                continue
            _record_created(path, details)
            created.append(path)
        fix_sudo_ownership(*created)

    return repair


def ensure_directory(path: Path) -> Path:
    """Create a directory tree and repair only the directories created here."""
    path = path.absolute()
    _validate_no_symlinks(path)
    missing: list[Path] = []
    current = path
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            current = current.parent
            continue
        if not stat.S_ISDIR(details.st_mode):
            raise NotADirectoryError(current)
        break

    created: list[Path] = []
    for directory in reversed(missing):
        try:
            os.mkdir(directory)
        except FileExistsError:
            details = directory.lstat()
            if not stat.S_ISDIR(details.st_mode):
                raise NotADirectoryError(directory) from None
        else:
            _record_created(directory)
            created.append(directory)
    fix_sudo_ownership(*created)
    return path


def ensure_state_directory() -> Path:
    """Create the application state directory and return its instance lock path."""
    state_dir = ensure_directory(user_home() / ".local" / "share" / "corecycler")
    return state_dir / "corecycler.lock"


def atomic_write(path: Path, content: str, *, durable: bool = False) -> None:
    """Atomically replace a file through an exclusive, non-following temporary file."""
    path = path.absolute()
    ensure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temporary: Path | None = None
    fd = -1
    try:
        for _ in range(128):
            candidate = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            temporary = candidate
            break
        if temporary is None:
            raise FileExistsError(f"cannot create a temporary file for {path}")

        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(content)
            if durable:
                stream.flush()
                os.fsync(fd)

        opened = os.fstat(fd)
        on_disk = temporary.lstat()
        if (opened.st_dev, opened.st_ino) != (on_disk.st_dev, on_disk.st_ino) or stat.S_ISLNK(on_disk.st_mode):
            raise OSError(f"temporary file changed before replacement: {temporary}")
        os.replace(temporary, path)
        temporary = None
        _record_created(path, opened)
        fix_sudo_ownership(path)
        if durable:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def invoking_uid() -> int:
    """Return the uid of the user who started the run."""
    if os.geteuid() == 0:
        sudo_uid = os.environ.get("SUDO_UID", "")
        if sudo_uid.isdigit():
            return int(sudo_uid)
    return os.geteuid()


def resolve_work_dir(configured: str = "") -> Path:
    """Return the configured stress work root or a per-user default."""
    if configured:
        return Path(configured)
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime:
        root = Path(runtime)
        with contextlib.suppress(OSError):
            if root.is_dir() and root.stat().st_uid == invoking_uid():
                return root / "corecycler" / "work"
    return user_home() / ".cache" / "corecycler" / "work"


def ensure_work_dir(configured: str = "") -> Path:
    return ensure_directory(resolve_work_dir(configured))


def fix_sudo_ownership(*paths: Path) -> None:
    """Repair ownership only for inode identities created by this module."""
    if os.geteuid() != 0:
        return
    uid = os.environ.get("SUDO_UID", "")
    gid = os.environ.get("SUDO_GID", "")
    if not (uid.isdigit() and gid.isdigit()):
        return
    for path in paths:
        expected = _CREATED_INODES.get(_key(path))
        if expected is None:
            continue
        try:
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or (details.st_dev, details.st_ino) != expected:
                continue
            os.chown(path, int(uid), int(gid), follow_symlinks=False)
        except OSError:
            continue
