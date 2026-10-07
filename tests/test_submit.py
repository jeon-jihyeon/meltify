import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meltify.cli import main
from meltify.commands.submit import Policy, Submitter, dig, retry_after
from meltify.output import read_jsonl
from tests.fixtures.mock_server import ScoringServer


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(round(s, 2))
        self.t += s


@dataclass
class Resp:
    status_code: int
    body: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    text: str = ""

    def json(self):
        return self.body


def _policy(**kw) -> Policy:
    base = dict(
        url="http://x/submit",
        gap=61.0,
        mode="multipart",
        field="file",
        auth_env="",
        score_path="result.similarity",
        goal="max",
        until_score=None,
        max_attempts=0,
        deadline=None,
        idle_exit=0.0,
        max_errors=5,
    )
    base.update(kw)
    return Policy(**base)


def _sub(tmp_path, responses, clock, **kw):
    it = iter(responses)
    return Submitter(
        _policy(**kw),
        tmp_path / "attempts.jsonl",
        now=clock.now,
        sleep=clock.sleep,
        post=lambda *a, **k: next(it),
        say=lambda m: None,
    )


def _file(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_gap_is_kept_and_survives_restart(tmp_path):
    clock = Clock()
    s = _sub(tmp_path, [Resp(200, {"result": {"similarity": 0.4}})], clock)
    s.submit(_file(tmp_path, "a.png", b"a"))
    clock.t += 10
    restarted = _sub(tmp_path, [Resp(200, {"result": {"similarity": 0.7}})], clock)
    rec = restarted.submit(_file(tmp_path, "b.png", b"b"))
    assert clock.slept == [51.0]
    assert rec["score"] == 0.7 and restarted.best["file"].endswith("b.png")


def test_429_waits_retry_after_and_duplicates_are_skipped(tmp_path):
    clock = Clock()
    s = _sub(
        tmp_path,
        [Resp(429, headers={"Retry-After": "5"}), Resp(200, {"result": {"similarity": 0.5}})],
        clock,
    )
    assert s.submit(_file(tmp_path, "a.png", b"a"))["status"] == "ok"
    assert clock.slept == [5.0]
    assert s.submit(_file(tmp_path, "copy.png", b"a"))["status"] == "skipped duplicate"
    assert len(s.attempts) == 1


def test_http_errors_are_recorded_not_raised(tmp_path):
    s = _sub(tmp_path, [Resp(500, text="boom")], Clock())
    rec = s.submit(_file(tmp_path, "a.png", b"a"))
    assert rec["status"] == "http 500" and rec["score"] is None


@pytest.mark.parametrize(
    "kw,scores,want",
    [
        ({"until_score": 0.8}, [0.5, 0.9, 0.95], "reached until_score"),
        ({"max_attempts": 2}, [0.1, 0.2, 0.3], "reached max_attempts"),
        ({"goal": "min", "until_score": 0.2}, [0.5, 0.1, 0.3], "reached until_score"),
    ],
)
def test_run_stops_on_conditions(tmp_path, kw, scores, want):
    queue, sent = tmp_path / "q", tmp_path / "s"
    queue.mkdir()
    for i in range(3):
        _file(queue, f"c{i}.png", bytes([i]))
    responses = [Resp(200, {"result": {"similarity": v}}) for v in scores]
    s = _sub(tmp_path, responses, Clock(), **kw)
    assert s.run(queue, sent) == want


def test_run_stops_when_idle_or_failing(tmp_path):
    queue, sent = tmp_path / "q", tmp_path / "s"
    s = _sub(tmp_path, [], Clock(), idle_exit=5)
    assert s.run(queue, sent) == "queue idle"
    for i in range(3):
        _file(queue, f"c{i}.png", bytes([i]))
    s = _sub(tmp_path, [Resp(500)] * 3, Clock(), max_errors=2)
    assert s.run(queue, sent) == "2 errors in a row"


def test_dig():
    assert dig({"a": {"b": [1, {"c": 2}]}}, "a.b.1.c") == 2
    assert dig({"a": 1}, "x.y") is None
    assert dig({"a": [1]}, "a.3") is None and dig({"a": [1]}, "a.-1") == 1


def test_retry_after_accepts_dates_and_clamps():
    now = 1_000_000.0
    assert retry_after("Mon, 12 Jan 1970 13:46:50 GMT", now, 61) == 10.0
    assert retry_after("Mon, 12 Jan 1970 13:46:30 GMT", now, 61) == 0.0
    assert retry_after("-5", now, 61) == 0.0
    assert retry_after("soon", now, 61) == 61
    assert retry_after(None, now, 61) == 61


def test_odd_responses_are_still_logged(tmp_path):
    clock = Clock()
    s = _sub(
        tmp_path,
        [Resp(429, headers={"Retry-After": "-1"}), Resp(200, {"result": []})],
        clock,
        score_path="result.3",
    )
    rec = s.submit(_file(tmp_path, "a.png", b"a"))
    assert rec["status"] == "ok" and rec["score"] is None
    assert len(read_jsonl(tmp_path / "attempts.jsonl")) == 1
    s = _sub(tmp_path, [Resp(200, {"result": {"similarity": True}})], clock)
    assert s.submit(_file(tmp_path, "b.png", b"b"))["score"] is None


def test_interrupt_after_a_request_is_logged(tmp_path):
    def stop(s):
        raise KeyboardInterrupt

    s = _sub(tmp_path, [Resp(429, headers={"Retry-After": "5"})], Clock())
    s.sleep = stop
    with pytest.raises(KeyboardInterrupt):
        s.submit(_file(tmp_path, "a.png", b"a"))
    (rec,) = read_jsonl(tmp_path / "attempts.jsonl")
    assert rec["status"] == "http 429"


def test_sent_files_are_never_overwritten_and_log_points_at_them(tmp_path):
    queue, sent = tmp_path / "q", tmp_path / "s"
    queue.mkdir()
    scores = [0.9, 0.1]
    responses = [Resp(200, {"result": {"similarity": v}}) for v in scores]
    s = _sub(tmp_path, responses, Clock(), max_attempts=1)
    for data in (b"first", b"second"):
        _file(queue, "same.png", data)
        s.run(queue, sent)
    assert len(list(sent.iterdir())) == 2
    assert all(Path(a["file"]).parent == sent for a in s.attempts)
    assert Path(s.best["file"]).read_bytes() == b"first"


def test_deadline_cuts_waits_short(tmp_path):
    clock = Clock()
    queue, sent = tmp_path / "q", tmp_path / "s"
    queue.mkdir()
    s = _sub(tmp_path, [Resp(200, {"result": {"similarity": 0.1}})], clock)
    s.submit(_file(tmp_path, "a.png", b"a"))
    s.policy.deadline = clock.t + 20
    _file(queue, "b.png", b"b")
    assert s.run(queue, sent) == "deadline passed"
    assert clock.slept == [20.0] and len(s.attempts) == 1
    assert (queue / "b.png").exists()

    clock = Clock()
    s = _sub(tmp_path / "x", [Resp(429, headers={"Retry-After": "100"})], clock)
    s.policy.deadline = clock.t + 10
    rec = s.submit(_file(tmp_path, "c.png", b"c"))
    assert clock.slept == [10.0] and rec["status"] == "http 429"


def test_against_a_real_rate_limited_server(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    queue = tmp_path / "queue"
    queue.mkdir()
    for i, data in enumerate([b"one", b"two", b"one"]):
        _file(queue, f"{i}.png", data)
    with ScoringServer(gap=0.3, scores=[0.2, 0.9]) as srv:
        code = main(
            [
                "submit", "run", "--queue", str(queue), "--url", srv.url, "--gap", "0.35",
                "--score-path", "result.similarity", "--idle-exit", "1", "--json",
            ]
        )  # fmt: skip
        out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [r["status"] for r in out["results"]] == ["ok", "ok"]
    assert "best 0.9" in out["summary"]
    assert srv.calls == 2


def test_status_and_usage(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["submit", "status"]) == 0
    assert main(["submit", "once"]) == 2
    assert main(["submit", "once", "x.png", "--url", "http://x"]) == 2


def test_policy_reads_config():
    from meltify.commands.submit import _policy

    args = argparse.Namespace(
        url=None, gap=None, mode=None, field=None, score_path=None, goal=None,
        until_score=None, max_attempts=None, deadline="2026-10-31T14:50", idle_exit=None,
    )  # fmt: skip
    conf = {"url": "u", "gap": 2.0, "mode": "json", "field": "f", "score_path": "s", "goal": "min",
            "until_score": "", "max_attempts": 0, "idle_exit": 0.0, "auth_env": "", "deadline": "",
            "max_errors": 5}  # fmt: skip
    p = _policy(args, conf)
    assert p.url == "u" and p.until_score is None and p.deadline is not None
