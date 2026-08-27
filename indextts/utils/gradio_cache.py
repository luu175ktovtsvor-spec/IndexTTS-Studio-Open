"""Scoped cache management for the optional Gradio WebUI."""

from __future__ import annotations

import argparse
import atexit
import os
from pathlib import Path
import shutil


def application_gradio_cache(root: str | Path) -> Path:
    application_root = Path(root).expanduser().resolve()
    cache = application_root / "outputs" / "gradio-cache"
    if cache.name != "gradio-cache" or cache.parent != application_root / "outputs":
        raise RuntimeError("拒绝使用应用目录以外的 Gradio 缓存路径")
    return cache


def clear_application_gradio_cache(root: str | Path) -> int:
    cache = application_gradio_cache(root)
    if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
        cache.unlink(missing_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in cache.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
        removed += 1
    return removed


def configure_application_gradio_cache(root: str | Path) -> Path:
    cache = application_gradio_cache(root)
    clear_application_gradio_cache(root)
    os.environ["GRADIO_TEMP_DIR"] = str(cache)
    atexit.register(clear_application_gradio_cache, root)
    return cache


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean the IndexTTS Gradio cache")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    removed = clear_application_gradio_cache(args.root)
    print(f"Gradio cache cleared: {removed} top-level item(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
