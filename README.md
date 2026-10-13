# nomad-fsspec

An [fsspec](https://filesystem-spec.readthedocs.io) filesystem for the raw data of
[NOMAD](https://nomad-lab.eu), the materials science repository. It is read-only.

```python
import fsspec

fs = fsspec.filesystem("nomad")  # https://nomad-lab.eu/prod/v1 by default
fs.ls("/")  # two folders: /datasets and /uploads
fs.ls("/datasets")  # public datasets
fs.ls("/datasets/wWgAnNZNQxOHLf97H62dgw", detail=False)  # the uploads of one dataset, by id
fs.upload_names(["Xa2LB__ESiKu-odRBPJhjA"])  # {"Xa2LB__ESiKu-odRBPJhjA": "nomad-demo-data.zip"}
fs.get_file(
    "/datasets/wWgAnNZNQxOHLf97H62dgw/Xa2LB__ESiKu-odRBPJhjA/nomad-demo-data/BrK_svSe/TBCC006.ABC/vasprun.xml.relax2",
    "vasprun.xml",
)
```

The top level has two folders, so a path means the same thing with or without a token:

- `/datasets/<dataset id>/<upload id>/<path inside the upload>` for public datasets. Datasets
  have their names in `display_name`. Uploads inside a dataset are listed by id, since naming
  all uploads of a large dataset at once makes NOMAD time out; `upload_names` looks names up
  25 at a time and keeps them, and `search_uploads` finds the ids of uploads by name.
- `/uploads/<upload id>/<path inside the upload>` for your uploads and those shared with you,
  published or not, as NOMAD's uploads page lists them. Listing `/uploads` needs a token.
  Published uploads can be read through it without one.

```python
fs = fsspec.filesystem("nomad", token="...")
fs.ls("/uploads")  # your uploads, with their names in display_name
```

A published upload is listed from the central directory of its ZIP, which takes one or two byte
ranges of `/uploads/<upload id>/raw` without the token, even for uploads with hundreds of thousands of
files. The listing is kept for the life of the process and shared, since published uploads do not
change, up to 500,000 files in all (about 50 MB), the oldest dropped first. NOMAD's own folder listing adds up every size below a folder, which can take a minute on a
large upload, so it is only used for uploads without a public ZIP: unpublished or under embargo.
NOMAD allows one connection to that ZIP route per client every 5 seconds; a busy answer is retried.

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
