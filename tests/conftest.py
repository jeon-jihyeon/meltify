import json
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from meltify.evidence import Envelope


@pytest.fixture(scope="session")
def envelope_validator():
    schema = json.loads(files("meltify.data").joinpath("envelope.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(autouse=True)
def validate_serialized_commands(monkeypatch, envelope_validator):
    original = Envelope.to_dict

    def checked(env):
        payload = original(env)
        if env.command in ("read", "doctor"):
            envelope_validator.validate(json.loads(json.dumps(payload)))
        return payload

    monkeypatch.setattr(Envelope, "to_dict", checked)


@pytest.fixture(autouse=True)
def no_shell_password(monkeypatch):
    """A MELTIFY_PASSWORD in the developer's shell would open every encrypted fixture"""
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)


@pytest.fixture(autouse=True)
def private_cache(tmp_path_factory, monkeypatch):
    """OCR, speech and frames cache under a temp folder, never the developer's own cache"""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
