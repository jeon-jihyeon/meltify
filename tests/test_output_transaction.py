import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import meltify
from meltify import cli, output
from meltify.commands import read


def _hold_output(directory, pipe):
    with output.exclusive(directory):
        pipe.send("locked")
        pipe.recv()
    pipe.close()


@pytest.mark.parametrize("terminate", [False, True])
def test_process_lock_releases_after_exit(tmp_path, terminate):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_output, args=(tmp_path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "writer did not acquire the lock"
        assert parent.recv() == "locked"
        with pytest.raises(ValueError, match="already in use"), output.exclusive(tmp_path):
            pytest.fail("a second process acquired the same output")
        if terminate:
            process.terminate()
        else:
            parent.send("release")
        process.join(10)
        assert not process.is_alive()
        with output.exclusive(tmp_path):
            assert (tmp_path / ".meltify.lock").is_file()
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(10)
        process.close()


def test_concurrent_reads_cannot_remove_another_runs_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("first\n")
    second.write_text("second\n")
    entered, release = Event(), Event()
    convert = read._convert_all

    def held(reader, files, urls, jobs):
        if files == [first]:
            entered.set()
            assert release.wait(10), "test did not release the first reader"
        return convert(reader, files, urls, jobs)

    monkeypatch.setattr(read, "_convert_all", held)
    out = tmp_path / "out"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(meltify.read, first, out=out, shallow=True, jobs=1)
        try:
            assert entered.wait(10), "first read did not reach conversion"
            with pytest.raises(ValueError, match="already in use"):
                meltify.read(second, out=out, shallow=True, jobs=1)
            other = meltify.read(second, out=tmp_path / "other", shallow=True, jobs=1)
            assert other.ok and other.complete
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result.ok and result.complete
    rows = [json.loads(line) for line in (out / "read/index.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["cite"] == str(first)
    assert (out / "read/first.txt.md").is_file()
    assert not (out / "read/second.txt.md").exists()


def test_busy_cli_returns_usage_envelope_without_touching_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    source = tmp_path / "notes.txt"
    source.write_text("hello\n")
    out = tmp_path / "out"
    with output.exclusive(out / "read"):
        before = list((out / "read").iterdir())
        assert cli.main(["read", str(source), "--out", str(out), "--shallow", "--json"]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert not payload["ok"] and not payload["complete"]
        assert payload["errors"][0]["code"] == "usage"
        assert list((out / "read").iterdir()) == before


@pytest.mark.parametrize("failure", ["replace", "serialize"])
def test_atomic_index_failure_preserves_existing_bytes(tmp_path, monkeypatch, failure):
    path = tmp_path / "index.jsonl"
    path.write_text('{"old":true}\n')
    if failure == "replace":

        def fail(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(os, "replace", fail)
    with pytest.raises((OSError, TypeError)):
        output.replace_jsonl(path, [{"new": object() if failure == "serialize" else True}])
    assert path.read_text() == '{"old":true}\n'
    assert list(tmp_path.iterdir()) == [path]


def test_failed_index_commit_keeps_stale_documents_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("first\n")
    second.write_text("second\n")
    out = tmp_path / "out"
    meltify.read(first, out=out, shallow=True, jobs=1)
    index = out / "read/index.jsonl"
    old = index.read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            meltify.read(second, out=out, shallow=True, jobs=1)
    assert index.read_bytes() == old
    assert (out / "read/first.txt.md").is_file()
    assert meltify.read(second, out=out, shallow=True, jobs=1).ok
    assert not (out / "read/first.txt.md").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_invalid_lock_file_is_rejected_without_changing_target(tmp_path, kind):
    lock = tmp_path / ".meltify.lock"
    target = tmp_path / "target"
    target.write_text("untouched")
    if kind == "symlink":
        lock.symlink_to(target)
    else:
        os.mkfifo(lock)
    with pytest.raises((OSError, ValueError)), output.exclusive(tmp_path):
        pytest.fail("an invalid lock was accepted")
    assert target.read_text() == "untouched"
