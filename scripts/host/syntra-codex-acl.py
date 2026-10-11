#!/usr/bin/python3
"""Root-owned ACL operations; called only under the launcher's isolation lock."""

import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path("/var/lib/syntra-build/workspaces")
WORKER = "syntra-codex"
CONTROL = "syntra-build"
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_directory(path):
    fd = os.open("/", FLAGS | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def acl(fd, specification, remove=False):
    # /proc resolves this inherited descriptor, never an untrusted pathname.
    subprocess.run(
        [
            "/usr/bin/setfacl",
            "-x" if remove else "-m",
            specification,
            f"/proc/self/fd/{fd}",
        ],
        pass_fds=(fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def walk(fd, root, device, operation, top=True):
    info = os.fstat(fd)
    if info.st_dev != device:
        raise ValueError("unexpected filesystem")
    directory = stat.S_ISDIR(info.st_mode)
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError("unsafe file type or hard link")
    worker = pwd.getpwnam(WORKER).pw_uid
    control = pwd.getpwnam(CONTROL).pw_uid
    if operation == "handoff":
        rights = "r-x" if directory or info.st_mode & 0o111 else "r--"
        spec = f"u:{control}:{rights},m::{rights}"
        if info.st_uid == control:
            owner_bits = (info.st_mode >> 6) & 7
            owner_bits |= 4 | (1 if directory else 0)
            owner_rights = "".join(
                letter if owner_bits & bit else "-"
                for letter, bit in (("r", 4), ("w", 2), ("x", 1))
            )
            spec += f",u::{owner_rights}"
        if directory:
            spec += f",d:u:{control}:r-x,d:m::r-x"
        acl(fd, spec)
    elif operation == "grant":
        rights = "rwx" if directory or info.st_mode & 0o111 else "rw-"
        spec = f"u:{worker}:{rights}"
        if directory:
            spec += f",d:u:{worker}:rwx"
        acl(fd, spec)
    elif operation == "revoke":
        acl(fd, f"u:{worker}", remove=True)
        if directory:
            acl(fd, f"d:u:{worker}", remove=True)
    elif operation == "verify":
        # Opening the inode as syntra-build already proves effective access.
        if directory and not os.access(f"/proc/self/fd/{fd}", os.R_OK | os.X_OK):
            raise ValueError("unreadable directory")
    if not directory:
        return
    for name in os.listdir(fd):
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if top and name == ".git":
            if stat.S_ISLNK(entry.st_mode):
                raise ValueError("unsafe Git pointer")
            # Git metadata is never repaired or granted source write access.
            if operation == "grant" and stat.S_ISREG(entry.st_mode):
                child = os.open(name, FLAGS, dir_fd=fd)
                try:
                    acl(child, f"u:{worker}:r--")
                finally:
                    os.close(child)
            elif operation == "revoke" and stat.S_ISREG(entry.st_mode):
                child = os.open(name, FLAGS, dir_fd=fd)
                try:
                    acl(child, f"u:{worker}", remove=True)
                finally:
                    os.close(child)
            continue
        if stat.S_ISLNK(entry.st_mode):
            target = Path(os.readlink(name, dir_fd=fd))
            location = Path(os.readlink(f"/proc/self/fd/{fd}")) / name
            resolved = (location.parent / target).resolve()
            if operation != "revoke" and not resolved.is_relative_to(root):
                raise ValueError("escaping symlink")
            continue
        child = os.open(name, FLAGS, dir_fd=fd)
        try:
            walk(child, root, device, operation, top=False)
        finally:
            os.close(child)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {
        "handoff",
        "verify",
        "grant",
        "revoke",
    }:
        return 64
    operation, path = sys.argv[1], Path(sys.argv[2])
    try:
        relative = path.relative_to(ROOT)
        if len(relative.parts) != 2 or any(
            str(UUID(part)) != part for part in relative.parts
        ):
            raise ValueError("invalid managed identity")
        fd = open_directory(path)
        try:
            # Reject a substituted Git pointer before any source ACL mutation.
            pointer = os.stat(".git", dir_fd=fd, follow_symlinks=False)
            if not (stat.S_ISREG(pointer.st_mode) or stat.S_ISDIR(pointer.st_mode)):
                raise ValueError("invalid Git metadata")
            walk(fd, path, os.fstat(fd).st_dev, operation)
        finally:
            os.close(fd)
        if operation == "handoff":
            subprocess.run(
                [
                    "/usr/bin/setpriv",
                    f"--reuid={CONTROL}",
                    f"--regid={CONTROL}",
                    "--init-groups",
                    "--no-new-privs",
                    "/usr/bin/python3",
                    "-I",
                    str(Path(__file__).resolve()),
                    "verify",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("trusted worktree ACL operation failed", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
