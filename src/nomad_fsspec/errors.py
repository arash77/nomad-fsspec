class NomadError(Exception):
    """NOMAD could not be reached or answered with an error.

    Not an ``OSError`` on purpose: fsspec's ``walk`` skips folders that raise ``OSError``, which
    would show a failed request as an empty folder.
    """
