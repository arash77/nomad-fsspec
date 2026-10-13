import pytest

from nomad_fsspec import filesystem


@pytest.fixture(autouse=True)
def forget_shared_listings():
    # kept ZIP listings and public datasets are shared by every instance in the process
    filesystem._zip_trees.clear()
    filesystem._shared_datasets.clear()
    yield
    filesystem._zip_trees.clear()
    filesystem._shared_datasets.clear()
