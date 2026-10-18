import logging

import pytest

from reader.collect import Hasher, RunStats
from reader.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def hasher():
    return Hasher("test-salt")


@pytest.fixture
def stats(store):
    return RunStats(id=store.start_run("test", "", 0))


@pytest.fixture(autouse=True)
def _reset_reader_logging():
    yield
    logging.getLogger("reader").handlers[:] = []  # cli.main installs a stderr handler
