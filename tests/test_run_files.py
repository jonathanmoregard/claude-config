#!/usr/bin/env python3
"""tests/agent-e2e/_lib/run_files.py: what the agent can plant in its run dir
is Refused (the run grades FAIL); a host I/O fault propagates as OSError, so a
grader reports it as no verdict, never as the agent's FAIL."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-e2e" / "_lib"))
from run_files import Refused, read_run  # noqa: E402


@pytest.fixture
def run(tmp_path: Path) -> Path:
    (tmp_path / "run" / "work").mkdir(parents=True)
    (tmp_path / "run" / "work" / "f.txt").write_bytes(b"hello")
    return tmp_path / "run"


def test_reads_a_regular_file(run: Path) -> None:
    assert read_run(run, "work/f.txt") == b"hello"
    assert read_run(run, "work/f.txt", 2) == b"he"


def test_absent_file_is_none(run: Path) -> None:
    assert read_run(run, "work/missing.txt") is None


@pytest.mark.parametrize("plant,why", [
    (lambda r, t: os.symlink(t / "secret", r / "work" / "x"), "a symlink"),
    (lambda r, t: os.symlink(t, r / "work" / "x"), "a symlink"),
    (lambda r, t: os.mkfifo(r / "work" / "x"), "not a regular file"),
    (lambda r, t: (r / "work" / "x").mkdir(), "not a regular file"),
])
def test_planted_content_is_refused(run: Path, tmp_path: Path, plant, why: str) -> None:
    (tmp_path / "secret").write_bytes(b"secret")
    plant(run, tmp_path)
    with pytest.raises(Refused, match=why):
        read_run(run, "work/x")


def test_symlinked_directory_on_the_path_is_refused(run: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "f.txt").write_bytes(b"secret")
    os.symlink(tmp_path / "elsewhere", run / "linked")
    with pytest.raises(Refused, match="a symlink"):
        read_run(run, "linked/f.txt")


def test_file_where_a_directory_belongs_is_refused(run: Path) -> None:
    with pytest.raises(Refused, match="not a directory"):
        read_run(run, "work/f.txt/x")


def test_socket_is_refused(run: Path) -> None:
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(run / "work" / "s"))
        with pytest.raises(Refused, match="not a regular file"):
            read_run(run, "work/s")
    finally:
        sock.close()


def test_missing_run_dir_is_the_hosts_fault(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_run(tmp_path / "no-such-run", "work/f.txt")


@pytest.mark.parametrize("rel", ["../secret", "work/../../secret", "/etc/passwd", "", "."])
def test_rel_must_stay_below_the_run_dir(run: Path, rel: str) -> None:
    with pytest.raises(ValueError):
        read_run(run, rel)


def test_host_io_fault_propagates_unchanged(run: Path, monkeypatch) -> None:
    def eio(fd):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    monkeypatch.setattr(os, "fstat", eio)
    with pytest.raises(OSError) as exc:
        read_run(run, "work/f.txt")
    assert exc.type is OSError and exc.value.errno == errno.EIO
