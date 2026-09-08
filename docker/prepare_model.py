#!/usr/bin/env python3
"""Prepare the persistent IndexTTS 2.5 model directory for Docker startup."""

from __future__ import annotations

import os
from pathlib import Path
import sys

# Direct script execution places /app/docker, rather than /app, on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from indextts.utils.model_download import ensure_models_available, snapshot_download
from indextts.utils.model_integrity import MODEL_FILE_METADATA, inspect_model_directory

MODEL_REPOSITORY = "IndexTeam/IndexTTS-2.5"


def prepare_model(directory: str | Path) -> dict:
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    primary = inspect_model_directory(
        root,
        verify_hashes=False,
        required_files=tuple(MODEL_FILE_METADATA),
    )
    if not primary["ok"]:
        for item in primary["invalid"]:
            path = root / item["name"]
            if path.is_file() or path.is_symlink():
                path.unlink()
        print(f"Downloading {MODEL_REPOSITORY} to {root}...")
        snapshot_download(MODEL_REPOSITORY, local_dir=str(root))

    ensure_models_available(str(root))

    verify_hashes = os.environ.get("INDEXTTS_VERIFY_MODEL", "0") == "1"
    result = inspect_model_directory(root, verify_hashes=verify_hashes)
    if not result["ok"]:
        details = [f"missing {name}" for name in result["missing"]]
        details.extend(
            f"invalid {item['name']}: {item['reason']}" for item in result["invalid"]
        )
        raise RuntimeError("Model preparation failed: " + "; ".join(details))
    return result


def main() -> int:
    directory = sys.argv[1] if len(sys.argv) > 1 else "checkpoints"
    result = prepare_model(directory)
    suffix = (
        f", {result['checkedHashes']} hashes verified"
        if result["hashVerification"]
        else ""
    )
    print(f"Model ready: {result['requiredFiles']} required files{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
