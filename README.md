# nomad-fsspec

An [fsspec](https://filesystem-spec.readthedocs.io) filesystem for the public raw data of
[NOMAD](https://nomad-lab.eu), the materials science repository. It is read-only.

```python
import fsspec

fs = fsspec.filesystem("nomad")  # https://nomad-lab.eu/prod/v1 by default
fs.ls("/")  # public datasets
fs.ls("/wWgAnNZNQxOHLf97H62dgw")  # the uploads of one dataset
fs.get_file(
    "/wWgAnNZNQxOHLf97H62dgw/Xa2LB__ESiKu-odRBPJhjA/nomad-demo-data/BrK_svSe/TBCC006.ABC/vasprun.xml.relax2",
    "vasprun.xml",
)
```

Paths are `/<dataset id>/<upload id>/<path inside the upload>`. Datasets and uploads have their
names in `display_name`. To use a NOMAD Oasis, pass `base_url`:

```python
fs = fsspec.filesystem("nomad", base_url="https://my-oasis.example.org/nomad-oasis")
```

Only public data is listed. NOMAD's rate limits (HTTP 429 and 503) are retried.

## Development

```bash
just install
just test        # unit tests
just test-live   # a few calls to nomad-lab.eu
just lint
```

## License

MIT
