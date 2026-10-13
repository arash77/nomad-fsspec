import pytest

from nomad_fsspec import filesystem


@pytest.fixture(autouse=True)
def forget_shared_listings():
    # kept ZIP listings are shared by every instance in the process
    filesystem._zip_trees.clear()
    yield
    filesystem._zip_trees.clear()
