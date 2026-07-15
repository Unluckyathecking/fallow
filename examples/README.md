# Examples

Examples are intentionally deterministic and safe to run without a coordinator, GPU, model
download or network access.

## Create and validate a model manifest

After `uv sync --frozen --dev`, run:

```bash
uv run python examples/model_manifest.py
```

The example creates a typed `ModelManifest`, serialises it to JSON and validates the JSON back
into an equivalent object. The hash and URL are illustrative; the script does not download or
execute a model.

Fallow's service entrypoints are not implemented yet. Examples of a live deployment will be
added only when they can be exercised by end-to-end CI.
