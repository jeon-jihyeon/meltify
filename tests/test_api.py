import threading

import pytest

import meltify
from meltify import passwords


def test_read_melts_a_file_and_returns_the_envelope(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_text("hello\nworld\n")

    env = meltify.read(src, out=tmp_path / "out", shallow=True)

    assert isinstance(env, meltify.Envelope)
    assert env.ok
    [row] = env.results
    assert row["cite"] == str(src)
    assert (tmp_path / "out" / "read" / "notes.txt.md").read_text().count("hello") == 1


def test_read_rejects_unknown_options(tmp_path):
    with pytest.raises(TypeError, match="shallw"):
        meltify.read(tmp_path, shallw=True)


def test_read_needs_a_path():
    with pytest.raises(TypeError):
        meltify.read()


def _passwords(monkeypatch) -> list[str | None]:
    """The password each read run starts with"""
    from meltify.converters import run

    seen: list[str | None] = []
    real = run.use
    monkeypatch.setattr(run, "use", lambda context: seen.append(context.password) or real(context))
    return seen


def test_a_password_given_in_code_skips_the_argv_warning(tmp_path, monkeypatch):
    seen = _passwords(monkeypatch)
    src = tmp_path / "notes.txt"
    src.write_text("hello\n")

    env = meltify.read(src, out=tmp_path / "out", password="s3cr3t")

    assert seen == ["s3cr3t"]
    assert not any("--password" in w for w in env.warnings)


def test_only_the_api_is_exported():
    assert set(meltify.__all__) == {"Envelope", "MissingTool", "__version__", "read"}


def test_concurrent_reads_keep_their_own_password_and_switches(tmp_path, monkeypatch):
    from meltify import converters
    from meltify.converters import Converted, run

    barrier = threading.Barrier(2, timeout=30)
    seen = {}
    real = converters.pick

    def probe(path, src):
        # Both runs have set up their state before either one reads it
        barrier.wait()
        seen[path.stem] = (passwords.password(), run.current().shallow)
        return Converted("text")

    monkeypatch.setattr(
        converters, "pick", lambda p: ("probe", probe) if p.suffix == ".probe" else real(p)
    )
    calls = {"a": {"password": "pw-a", "shallow": True}, "b": {"password": "pw-b"}}
    threads = []
    for name, options in calls.items():
        (tmp_path / f"{name}.probe").write_text("x")
        args = (tmp_path / f"{name}.probe",)
        options = {"out": tmp_path / name, **options}
        threads.append(threading.Thread(target=meltify.read, args=args, kwargs=options))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": ("pw-a", True), "b": ("pw-b", False)}
    # Nothing a run sets is left behind for its caller either
    # A lone run has no partner to wait for
    barrier = threading.Barrier(1)
    meltify.read(tmp_path / "a.probe", out=tmp_path / "c", password="pw-c", shallow=True)
    assert passwords.password() is None and run.current() == run.RunContext()


def test_string_options_get_the_flag_types(tmp_path, monkeypatch):
    seen = _passwords(monkeypatch)
    secret = tmp_path / "pw.txt"
    secret.write_text("s3cr3t\n")
    src = tmp_path / "notes.txt"
    src.write_text("hello\n")

    env = meltify.read(src, out=tmp_path / "out", password_file=str(secret), jobs="2")

    assert env.ok and seen == ["s3cr3t"]
