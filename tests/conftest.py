import pytest


@pytest.fixture(autouse=True)
def no_shell_password(monkeypatch):
    """A MELTIFY_PASSWORD in the developer's shell would open every encrypted fixture"""
    monkeypatch.delenv("MELTIFY_PASSWORD", raising=False)


@pytest.fixture(autouse=True)
def private_cache(tmp_path_factory, monkeypatch):
    """OCR, speech and frames cache under a temp folder, never the developer's own cache"""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
