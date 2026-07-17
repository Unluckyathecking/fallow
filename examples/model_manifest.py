# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create and round-trip a Fallow model manifest without network access."""

from fallow_protocol import ModelManifest, WorkerKind


def main() -> None:
    manifest = ModelManifest(
        model_id="example-7b-q4",
        family="example",
        quant="Q4_K_M",
        worker_kind=WorkerKind.CHAT,
        file_name="example-7b-q4.gguf",
        sha256="0" * 64,
        size_bytes=4_000_000_000,
        min_ram_mb=6_144,
        license="Apache-2.0",
        source_url="https://example.invalid/model-card",
    )

    encoded = manifest.model_dump_json(indent=2)
    decoded = ModelManifest.model_validate_json(encoded)
    assert decoded == manifest
    print(encoded)


if __name__ == "__main__":
    main()
