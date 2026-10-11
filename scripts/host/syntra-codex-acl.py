#!/usr/bin/python3
"""Root-owned ACL operations; called only under the launcher's isolation lock."""

import ctypes
import os
import pwd
import stat
import subprocess
import sys
from collections import deque
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


class ACL:
    """Use Linux libacl on pinned inodes, without a subprocess for every file."""

    OWNER, USER, GROUP_OWNER, GROUP, MASK, OTHER = 1, 2, 4, 8, 16, 32
    DEFAULT = 0x4000

    def __init__(self):
        self.lib = ctypes.CDLL("libacl.so.1", use_errno=True)
        pointer, integer = ctypes.c_void_p, ctypes.c_int
        for name, result, arguments in (
            ("acl_get_fd", pointer, (integer,)),
            ("acl_get_file", pointer, (ctypes.c_char_p, integer)),
            ("acl_get_entry", integer, (pointer, integer, ctypes.POINTER(pointer))),
            ("acl_get_tag_type", integer, (pointer, ctypes.POINTER(integer))),
            ("acl_get_qualifier", pointer, (pointer,)),
            ("acl_get_permset", integer, (pointer, ctypes.POINTER(pointer))),
            ("acl_get_perm", integer, (pointer, integer)),
            ("acl_from_text", pointer, (ctypes.c_char_p,)),
            ("acl_set_fd", integer, (integer, pointer)),
            ("acl_set_file", integer, (ctypes.c_char_p, integer, pointer)),
            ("acl_free", integer, (pointer,)),
        ):
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, arguments
        self.worker = pwd.getpwnam(WORKER).pw_uid
        self.control = pwd.getpwnam(CONTROL).pw_uid

    @staticmethod
    def checked(value, pointer=False):
        if (pointer and not value) or (not pointer and value < 0):
            raise OSError(ctypes.get_errno(), "ACL operation failed")
        return value

    def read(self, fd, default=False):
        handle = self.checked(
            self.lib.acl_get_file(os.fsencode(f"/proc/self/fd/{fd}"), self.DEFAULT)
            if default
            else self.lib.acl_get_fd(fd),
            pointer=True,
        )
        entries = {}
        try:
            entry, first = ctypes.c_void_p(), 0
            while self.checked(
                self.lib.acl_get_entry(handle, first, ctypes.byref(entry))
            ):
                first = 1
                tag, perms = ctypes.c_int(), ctypes.c_void_p()
                self.checked(self.lib.acl_get_tag_type(entry, ctypes.byref(tag)))
                self.checked(self.lib.acl_get_permset(entry, ctypes.byref(perms)))
                qualifier = None
                if tag.value in (self.USER, self.GROUP):
                    raw = self.checked(self.lib.acl_get_qualifier(entry), pointer=True)
                    try:
                        qualifier = ctypes.cast(raw, ctypes.POINTER(ctypes.c_uint))[0]
                    finally:
                        self.lib.acl_free(raw)
                entries[tag.value, qualifier] = sum(
                    bit
                    for bit in (4, 2, 1)
                    if self.checked(self.lib.acl_get_perm(perms, bit))
                )
            return entries
        finally:
            self.lib.acl_free(handle)

    def write(self, fd, entries, default=False):
        tags = {
            self.OWNER: "u",
            self.USER: "u",
            self.GROUP_OWNER: "g",
            self.GROUP: "g",
            self.MASK: "m",
            self.OTHER: "o",
        }
        text = ",".join(
            f"{tags[tag]}:{'' if qualifier is None else qualifier}:"
            + "".join(
                letter if bits & bit else "-"
                for letter, bit in (("r", 4), ("w", 2), ("x", 1))
            )
            for (tag, qualifier), bits in entries.items()
        )
        handle = self.checked(
            self.lib.acl_from_text(text.encode("ascii")), pointer=True
        )
        try:
            self.checked(
                self.lib.acl_set_file(
                    os.fsencode(f"/proc/self/fd/{fd}"), self.DEFAULT, handle
                )
                if default
                else self.lib.acl_set_fd(fd, handle)
            )
        finally:
            self.lib.acl_free(handle)

    def update(self, fd, operation, directory, git_pointer=False):
        info = os.fstat(fd)
        access = self.read(fd)
        for default in (False, True) if directory else (False,):
            entries = self.read(fd, default=True) if default else dict(access)
            original = dict(entries)
            if not entries:
                if operation == "revoke":
                    continue
                entries = {
                    key: value
                    for key, value in access.items()
                    if key[0] in (self.OWNER, self.GROUP_OWNER, self.OTHER)
                }
            if operation == "handoff":
                rights = 5 if directory or info.st_mode & 0o111 else 4
                entries[self.USER, self.control] = rights
                entries[self.MASK, None] = rights
                if not default and info.st_uid == self.control:
                    entries[self.OWNER, None] |= 4 | (1 if directory else 0)
            else:
                key = self.USER, self.worker
                if operation == "grant":
                    entries[key] = (
                        4
                        if git_pointer
                        else 7
                        if directory or info.st_mode & 0o111
                        else 6
                    )
                elif key not in entries:
                    continue
                else:
                    del entries[key]
                mask = 0
                for (tag, _qualifier), bits in entries.items():
                    if tag in (self.USER, self.GROUP_OWNER, self.GROUP):
                        mask |= bits
                entries[self.MASK, None] = mask
            if entries != original:
                self.write(fd, entries, default)


def walk(fd, device, operation, editor, links, relative=""):
    info = os.fstat(fd)
    if info.st_dev != device:
        raise ValueError("unexpected filesystem")
    directory = stat.S_ISDIR(info.st_mode)
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError("unsafe file type or hard link")
    if operation != "verify":
        editor.update(fd, operation, directory)
    else:
        # Opening the inode as syntra-build already proves effective access.
        if directory and not os.access(f"/proc/self/fd/{fd}", os.R_OK | os.X_OK):
            raise ValueError("unreadable directory")
    if not directory:
        return
    for name in os.listdir(fd):
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not relative and name == ".git":
            if stat.S_ISLNK(entry.st_mode):
                raise ValueError("unsafe Git pointer")
            # Git metadata is never repaired or granted source write access.
            if operation in {"grant", "revoke"} and stat.S_ISREG(entry.st_mode):
                child = os.open(name, FLAGS, dir_fd=fd)
                try:
                    pointer = os.fstat(child)
                    if pointer.st_dev != device or pointer.st_nlink != 1:
                        raise ValueError("unsafe Git pointer")
                    editor.update(child, operation, False, git_pointer=True)
                finally:
                    os.close(child)
            continue
        if stat.S_ISLNK(entry.st_mode):
            # Every link is opaque at the root boundary, including venv Python.
            links.append(f"{relative}/{name}".lstrip("/"))
            continue
        child = os.open(name, FLAGS, dir_fd=fd)
        try:
            walk(
                child,
                device,
                operation,
                editor,
                links,
                f"{relative}/{name}".lstrip("/"),
            )
        finally:
            os.close(child)


def verify_source_link(fd, root, name):
    """Resolve only inside the pinned root, never opening an external target."""
    pending, stack, followed = deque(Path(name).parts), [os.dup(fd)], 0
    try:
        while pending:
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if len(stack) == 1:
                    raise ValueError("escaping source symlink")
                os.close(stack.pop())
                continue
            try:
                entry = os.stat(part, dir_fd=stack[-1], follow_symlinks=False)
            except FileNotFoundError:
                depth = len(stack)
                for remaining in pending:
                    depth += -1 if remaining == ".." else 0 if remaining == "." else 1
                    if depth < 0:
                        raise ValueError("escaping dangling source symlink")
                return
            if stat.S_ISLNK(entry.st_mode):
                followed += 1
                if followed > 40:
                    raise ValueError("source symlink loop")
                target = Path(os.readlink(part, dir_fd=stack[-1]))
                if target.is_absolute():
                    target = target.relative_to(root)
                    while len(stack) > 1:
                        os.close(stack.pop())
                pending.extendleft(reversed(target.parts))
            elif stat.S_ISDIR(entry.st_mode):
                stack.append(os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=stack[-1]))
            elif pending:
                return
    finally:
        for opened in stack:
            os.close(opened)


def verify_source_links(fd, root, links):
    # Git runs only as the control-plane identity, with no inherited configuration
    # or fsmonitor hooks. Tracked paths remain relevant even when force-ignored.
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=True,
    )
    if result.stderr:
        raise ValueError("Git source enumeration warning")
    relevant = set()
    for raw in result.stdout.split(b"\0"):
        if raw:
            path = Path(os.fsdecode(raw))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe Git source path")
            relevant.add(str(path))
            relevant.update(str(parent) for parent in path.parents)
    for name in links:
        if name in relevant:
            verify_source_link(fd, root, name)


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
            links = []
            editor = None if operation == "verify" else ACL()
            walk(fd, os.fstat(fd).st_dev, operation, editor, links)
            if operation == "verify":
                verify_source_links(fd, path, links)
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
