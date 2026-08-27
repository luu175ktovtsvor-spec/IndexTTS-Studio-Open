"""Integrity checks for the official IndexTTS 2.5 model files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


MODEL_FILE_METADATA: dict[str, dict[str, Any]] = {
    "config.yaml": {
        "size": 2_860,
        "sha256": "18adf417be3e8f5e2e48e30f7420c719170a6870619436250f360d626877870e",
    },
    "gpt.pth": {
        "size": 3_259_599_833,
        "sha256": "43a8f4c30eccdf201958d3b9713511482c19d56dc20b0b1c4ee1e6b080b19d85",
    },
    "s2mel.pth": {
        "size": 414_908_601,
        "sha256": "9b1b0003fc189c94cc349758d7ebc25f903b7eb2de4602879959cc64ce816456",
    },
    "codec.pth": {
        "size": 607_290_935,
        "sha256": "d15cbed16a40f478438c961fb043f68dfa6353bf56c966761315db3433e9722c",
    },
    "feat1.pt": {
        "size": 57_170,
        "sha256": "f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4",
    },
    "feat2.pt": {
        "size": 374_866,
        "sha256": "9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80",
    },
    "multilingual_zh_ja_yue_char_del.tiktoken": {
        "size": 907_395,
        "sha256": "747979631e813193436aabcff7c1c235d37de8097b71c563ec8b63b7a515c718",
    },
    "wav2vec2bert_stats.pt": {
        "size": 9_343,
        "sha256": "c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2",
    },
    "qwen0.6bemo4-merge/model.safetensors": {
        "size": 1_192_135_096,
        "sha256": "11293257a8df593c154a8ecd5fc039f3076de35411e35f06d41b471e136f6641",
    },
}

RUNTIME_MODEL_FILES = (
    "hf_cache/bigvgan/bigvgan_generator.pt",
    "hf_cache/campplus_cn_common.bin",
    "hf_cache/semantic_codec/model.safetensors",
    "hf_cache/w2v-bert-2.0/model.safetensors",
)

REQUIRED_MODEL_FILES = tuple(MODEL_FILE_METADATA) + RUNTIME_MODEL_FILES


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_model_directory(
    directory: str | Path,
    *,
    verify_hashes: bool = False,
    metadata: Mapping[str, Mapping[str, Any]] = MODEL_FILE_METADATA,
    required_files: tuple[str, ...] = REQUIRED_MODEL_FILES,
) -> dict[str, Any]:
    root = Path(directory).expanduser().resolve()
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    checked_hashes = 0

    for name in required_files:
        path = root / name
        if not path.is_file():
            missing.append(name)
            continue
        try:
            size = path.stat().st_size
        except OSError as error:
            invalid.append({"name": name, "reason": str(error)})
            continue
        if size <= 0:
            invalid.append({"name": name, "reason": "文件为空"})
            continue
        expected = metadata.get(name)
        if expected and size != int(expected["size"]):
            invalid.append(
                {
                    "name": name,
                    "reason": f"文件大小不符（当前 {size}，应为 {expected['size']}）",
                }
            )
            continue
        if verify_hashes and expected and expected.get("sha256"):
            checked_hashes += 1
            if sha256_file(path) != expected["sha256"]:
                invalid.append({"name": name, "reason": "SHA-256 校验不一致"})

    return {
        "directory": str(root),
        "ok": not missing and not invalid,
        "missing": missing,
        "invalid": invalid,
        "requiredFiles": len(required_files),
        "officialSizeChecks": len(metadata),
        "checkedHashes": checked_hashes,
        "hashVerification": verify_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify IndexTTS 2.5 model files")
    parser.add_argument("directory", nargs="?", default="checkpoints")
    parser.add_argument("--quick", action="store_true", help="Skip SHA-256 checks")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args()
    result = inspect_model_directory(args.directory, verify_hashes=not args.quick)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(
            f"模型文件校验通过：{result['requiredFiles']} 个必需文件，"
            f"{result['checkedHashes']} 个 SHA-256。"
        )
    else:
        print("模型文件校验失败。")
        for name in result["missing"]:
            print(f"- 缺少：{name}")
        for item in result["invalid"]:
            print(f"- 异常：{item['name']}：{item['reason']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
