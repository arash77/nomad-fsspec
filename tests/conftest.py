import pytest

from nomad_fsspec import filesystem


@pytest.fixture(autouse=True)
def forget_shared_listings():
    # kept ZIP listings, public listings and public uploads are shared by every instance
    shared = (filesystem._zip_trees, filesystem._shared_listings, filesystem._public_uploads)
    for kept in shared:
        kept.clear()
    yield
    for kept in shared:
        kept.clear()
