import fsspec

from nomad_fsspec import NomadFileSystem


def test_registered_as_the_nomad_protocol():
    assert isinstance(fsspec.filesystem("nomad", skip_instance_cache=True), NomadFileSystem)
