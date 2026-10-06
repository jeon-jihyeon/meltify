import pytest


@pytest.fixture(autouse=True)
def no_shell_password(monkeypatch):
    """A MELTIFY_PASSWORD in the developer's shell would open every encrypted fixture"""
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)
