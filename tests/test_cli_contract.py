import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import meltify
from meltify.cli import main
from meltify.converters import Block, Converted, RecognizeJob
from meltify.evidence import Envelope, Src
from meltify.safe import MissingTool


@pytest.fixture
def validate(envelope_validator):
    return lambda payload: envelope_validator.validate(json.loads(json.dumps(payload)))


def test_api_body_and_cli_index_share_cited_content(tmp_path, capsys, validate):
    source = tmp_path / "notes.txt"
    source.write_text("hello\nworld\n")
    env = meltify.read(source, out=tmp_path / "api", shallow=True, content=True)
    payload = json.loads(json.dumps(env.to_dict()))
    validate(payload)
    assert payload["ok"] and payload["complete"]
    [row] = payload["results"]
    assert [c["text"] for c in row["content"]] == ["1| hello\n2| world"]
    assert row["content"][0]["src"] == {"path": str(source), "line": 1}
    assert row["content"][0]["cite"] == f"{source}:1"
    assert row["coverage"]["status"] == "complete"
    assert row["artifacts"][0]["role"] == "markdown"

    assert (
        main(
            [
                "read",
                str(source),
                "--out",
                str(tmp_path / "cli"),
                "--json",
                "--shallow",
                "--content",
            ]
        )
        == 0
    )
    cli = json.loads(capsys.readouterr().out)
    validate(cli)
    assert cli["results"][0]["content"] == row["content"]
    [index] = [a["path"] for a in cli["artifacts"] if a["role"] == "results"]
    assert json.loads(Path(index).read_text()) == cli["results"][0]


def test_partial_read_preserves_page_and_requested_scope(tmp_path, monkeypatch, validate):
    from meltify import converters

    source = tmp_path / "scan.probe"
    source.write_text("probe")
    at = Src(str(source), page=2, img=1)
    converted = Converted(
        "probe",
        blocks=[Block(Src(str(source), page=1), "native text")],
        jobs=[RecognizeJob("image", at, data=b"pixels")],
    )
    monkeypatch.setattr(converters, "pick", lambda path: ("probe", lambda path, src: converted))
    payload = meltify.read(
        source, out=tmp_path / "out", shallow=True, pages="1-2", content=True
    ).to_dict()
    validate(json.loads(json.dumps(payload)))
    assert payload["ok"] and not payload["complete"]
    [row] = payload["results"]
    assert row["coverage"]["status"] == "partial"
    assert row["coverage"]["scope"]["pages"] == "1-2"
    assert any(u["cite"] == at.cite() for u in row["coverage"]["unread"])
    assert row["content"][0]["text"] == "native text"


def test_nested_attachment_reference_points_to_saved_input(tmp_path, validate):
    source = tmp_path / "bundle.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("memo.txt", "hello")
    payload = meltify.read(source, out=tmp_path / "out", shallow=True, content=True).to_dict()
    validate(json.loads(json.dumps(payload)))
    parent = next(r for r in payload["results"] if r["cite"] == str(source))
    [attachment] = [a for a in parent["artifacts"] if a["role"] == "attachment"]
    assert attachment["cite"] == f"{source}#att=memo.txt"
    assert Path(attachment["path"]).read_text() == "hello"
    child = next(r for r in payload["results"] if r["cite"] == attachment["cite"])
    assert child["content"][0]["src"]["parts"] == ("memo.txt",)


@pytest.mark.parametrize("reason", ["engine", "budget", "failed", None])
def test_recognition_gaps_and_completed_blank_scan(tmp_path, monkeypatch, validate, reason):
    from meltify import converters
    from meltify.commands import read
    from meltify.recognize import Outcome

    source = tmp_path / "scan.probe"
    source.write_text("probe")
    at = Src(str(source), page=2)
    converted = Converted("probe", jobs=[RecognizeJob("page", at, path=source)])
    monkeypatch.setattr(converters, "pick", lambda path: ("probe", lambda path, src: converted))
    recognizer = SimpleNamespace(
        check_named=lambda **kwargs: None,
        run=lambda jobs: [Outcome(left=reason)],
        seen=0,
    )
    monkeypatch.setattr(read, "_recognizer", lambda *args: recognizer)
    payload = meltify.read(source, out=tmp_path / "out", no_pictures=True, content=True).to_dict()
    validate(payload)
    [row] = payload["results"]
    assert payload["ok"]
    assert payload["complete"] == (reason is None)
    assert row["coverage"]["status"] == ("complete" if reason is None else "partial")
    if reason:
        assert {"reason": reason, "src": at.to_dict(), "cite": at.cite()} in row["coverage"][
            "unread"
        ]
    else:
        assert row["needs"] == ["no text found"]
        assert row["content"] == []


def test_item_failure_is_incomplete_without_changing_exit_code(tmp_path, monkeypatch, validate):
    from meltify import converters

    source = tmp_path / "bad.probe"
    source.write_text("bad")

    def fail(path, src):
        raise RuntimeError("cannot read this item")

    monkeypatch.setattr(converters, "pick", lambda path: ("probe", fail))
    env = meltify.read(source, out=tmp_path / "out", shallow=True, content=True)
    validate(env.to_dict())
    assert env.ok and env.exit_code == 0 and not env.complete
    assert env.results[0]["coverage"]["status"] == "failed"
    assert env.results[0]["content"] == []


def test_default_results_and_index_do_not_duplicate_body(tmp_path, capsys, validate):
    source = tmp_path / "memo.txt"
    marker = "SYNTHETIC_BODY_7341"
    source.write_text(marker)
    env = meltify.read(source, out=tmp_path / "api", shallow=True)
    assert "content" not in env.results[0]
    assert env.complete and env.results[0]["chars"] > 0
    assert marker not in json.dumps(env.to_dict())
    assert marker not in Path(env.artifacts[0]["path"]).read_text()
    assert marker in Path(env.results[0]["out"]).read_text()
    assert main(["read", str(source), "--out", str(tmp_path / "cli"), "--json", "--shallow"]) == 0
    output = capsys.readouterr()
    assert marker not in output.out and marker not in output.err
    payload = json.loads(output.out)
    validate(payload)
    assert "content" not in payload["results"][0]


@pytest.mark.parametrize(
    ("reading", "total", "frames", "reason"),
    [
        ("missing", 1, 20, "engine"),
        ("failed", 1, 20, "failed"),
        ("blank", 2, 1, "kept 1 of 2 frames"),
        ("blank", 1, 20, None),
        ("blank", 2, 0, None),
    ],
)
def test_video_frame_gaps_cannot_be_hidden_by_speech(
    tmp_path, monkeypatch, validate, reading, total, frames, reason
):
    from meltify import converters
    from meltify.engines import ocr
    from meltify.engines.consensus import EngineReading
    from meltify.recognize import Footage, Frame, Picture, Recognizer, Shot
    from tests.fixtures.make_docs import card

    source = tmp_path / "clip.probe"
    source.write_bytes(b"video")
    image = tmp_path / "frame.png"
    image.write_bytes(card("FRAME", (120, 80)))
    at = Src(str(source))
    converted = Converted("media", jobs=[RecognizeJob("video", at, path=source, listen=False)])
    monkeypatch.setattr(converters, "pick", lambda path: ("media", lambda path, src: converted))
    monkeypatch.setattr(ocr, "select", lambda *args: [])
    picture = {
        "missing": None,
        "failed": Picture((120, 80), []),
        "blank": Picture((120, 80), [EngineReading("local", "local", [])]),
    }[reading]
    kept = [Frame(Shot(3, "frame", ("scene",), image), picture)] if frames else []
    monkeypatch.setattr(Recognizer, "footage", lambda *args: Footage([], [], kept, total))
    payload = meltify.read(source, out=tmp_path / "out", no_pictures=True, frames=frames).to_dict()
    validate(payload)
    [row] = [r for r in payload["results"] if r["kind"] == "media"]
    assert payload["ok"] and payload["complete"] == (reason is None)
    assert row["coverage"]["scope"]["frames"] == frames
    if reason:
        expected = at.cite() if reason.startswith("kept") else Src(str(source), t=(3, 3)).cite()
        assert any(
            u["reason"] == reason and u["cite"] == expected for u in row["coverage"]["unread"]
        )


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ValueError("bad input"), 2),
        (MissingTool("engine", "install it"), 3),
        (RuntimeError("broken"), 1),
    ],
)
def test_command_errors_still_validate(failure, code, monkeypatch, capsys, validate):
    from meltify.commands import read

    def fail(*args):
        raise failure

    monkeypatch.setattr(read, "run", fail)
    assert main(["read", "notes.txt", "--json"]) == code
    payload = json.loads(capsys.readouterr().out)
    validate(payload)
    assert not payload["ok"] and not payload["complete"]


@pytest.mark.parametrize(("option", "value"), [("start", "nan"), ("end", "inf"), ("fps", "inf")])
def test_nonfinite_scope_is_rejected_before_output(tmp_path, capsys, option, value):
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    with pytest.raises(ValueError, match="finite"):
        meltify.read(source, out=tmp_path / "out", shallow=True, **{option: value})
    assert not (tmp_path / "out").exists()
    if option != "fps":
        assert main(["read", str(source), "--json", f"--{option}", value]) == 2
        payload = json.loads(capsys.readouterr().out, parse_constant=lambda s: pytest.fail(s))
        assert not payload["ok"] and not payload["complete"]


def test_doctor_contract_remains_independent_of_read_coverage(monkeypatch, capsys, validate):
    from meltify.commands import doctor

    monkeypatch.setattr(doctor, "run", lambda *args: Envelope(command="doctor", version="0.0.0"))
    assert main(["doctor", "--quick", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    validate(payload)
    assert "complete" not in payload
