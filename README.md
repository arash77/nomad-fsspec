# nomad-fsspec

An [fsspec](https://filesystem-spec.readthedocs.io) filesystem for the raw data of
[NOMAD](https://nomad-lab.eu), the materials science repository. It is read-only.

```python
import fsspec

fs = fsspec.filesystem("nomad")  # https://nomad-lab.eu/prod/v1 by default
fs.ls("/")  # public datasets
fs.ls("/wWgAnNZNQxOHLf97H62dgw", detail=False)  # the uploads of one dataset, by id
fs.upload_names(["Xa2LB__ESiKu-odRBPJhjA"])  # {"Xa2LB__ESiKu-odRBPJhjA": "nomad-demo-data.zip"}
fs.get_file(
    "/wWgAnNZNQxOHLf97H62dgw/Xa2LB__ESiKu-odRBPJhjA/nomad-demo-data/BrK_svSe/TBCC006.ABC/vasprun.xml.relax2",
    "vasprun.xml",
)
```

Paths are `/<dataset id>/<upload id>/<path inside the upload>`, and only public data is listed.
Datasets have their names in `display_name`. Uploads inside a dataset are listed by id, since
naming all uploads of a large dataset at once makes NOMAD time out; `upload_names` looks names up
25 at a time and keeps them.

With a token, the top level lists your uploads and those shared with you, published or not, as
NOMAD's "Your uploads" page does. Paths are then `/<upload id>/<path inside the upload>`:

```python
fs = fsspec.filesystem("nomad", token="...")
fs.ls("/")  # your uploads, with their names in display_name
```

A [personal access token](https://docs.nomad-lab.eu/howto/manage/program/auth.html) with the
`uploads:read` scope is enough. To use a NOMAD Oasis, pass `base_url`:

```python
fs = fsspec.filesystem("nomad", base_url="https://my-oasis.example.org/nomad-oasis")
```

NOMAD's rate limits (HTTP 429 and 503) are retried. Redirects are not followed: a base URL that
redirects is reported with the address it redirects to.

## Development

```bash
uv sync
uv run pytest -m "not live"  # unit tests
uv run pytest -m live        # a few calls to nomad-lab.eu
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

## License

MIT
