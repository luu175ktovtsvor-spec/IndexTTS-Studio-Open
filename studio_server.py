#!/usr/bin/env python3
"""Local API and static host for IndexTTS Studio.

The interface is intentionally separate from IndexTTS's upstream Gradio page.
All generation options map directly to the installed IndexTTS 2.5 API.
"""

from __future__ import annotations

import asyncio
from array import array
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from indextts.utils.examples_downloader import ensure_examples_available
from indextts.utils.model_integrity import (
    REQUIRED_MODEL_FILES,
    inspect_model_directory,
)
from indextts.utils.presets import (
    delete_preset,
    get_presets_dir,
    list_presets,
    load_preset,
    preset_exists,
    save_preset,
)
from studio_engine import MacIndexTTS2

ROOT = Path(__file__).resolve().parent
CHECKPOINTS = (
    Path(os.environ.get("INDEXTTS_CHECKPOINTS_DIR", str(ROOT / "checkpoints")))
    .expanduser()
    .resolve()
)
OUTPUT_ROOT = ROOT / "outputs"
OUTPUTS = OUTPUT_ROOT / "studio"
UPLOADS = OUTPUTS / "uploads"
GENERATIONS = OUTPUTS / "generations"
PRESETS = get_presets_dir()
STATIC = ROOT / "studio"
EXAMPLES = ROOT / "examples"
MAX_AUDIO_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_PRESET_NAME_CHARS = 60
MAX_TEXT_CHARS = 20_000
MAX_HISTORY_ITEMS = 100
MAX_HISTORY_BYTES = 5 * 1024 * 1024 * 1024
REFERENCE_WINDOW_SECONDS = 15.0
MAX_REFERENCE_START_SECONDS = 24 * 60 * 60
OUTPUT_TARGET_LOUDNESS_LUFS = -18
OUTPUT_TRUE_PEAK_DBFS = -2
VIDEO_MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
SUPPORTED_MEDIA_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".webm",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
}
ALLOWED_LANGUAGES = {"ZH", "EN", "JA", "ES", "AR"}
EXPORT_FORMATS: dict[str, dict[str, Any]] = {
    "wav": {
        "label": "WAV",
        "description": "原始无损",
        "extension": "wav",
        "mediaType": "audio/wav",
        "encoder": None,
        "arguments": [],
    },
    "mp3": {
        "label": "MP3",
        "description": "通用兼容",
        "extension": "mp3",
        "mediaType": "audio/mpeg",
        "encoder": "libmp3lame",
        "arguments": ["-c:a", "libmp3lame", "-b:a", "192k"],
    },
    "m4a": {
        "label": "M4A",
        "description": "体积较小",
        "extension": "m4a",
        "mediaType": "audio/mp4",
        "encoder": "aac",
        "arguments": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    },
    "flac": {
        "label": "FLAC",
        "description": "无损压缩",
        "extension": "flac",
        "mediaType": "audio/flac",
        "encoder": "flac",
        "arguments": ["-c:a", "flac", "-compression_level", "5"],
    },
    "ogg": {
        "label": "OGG",
        "description": "Opus 高压缩",
        "extension": "ogg",
        "mediaType": "audio/ogg",
        "encoder": "libopus",
        "arguments": ["-c:a", "libopus", "-b:a", "160k", "-vbr", "on"],
    },
}
for directory in (UPLOADS, GENERATIONS):
    directory.mkdir(parents=True, exist_ok=True)
for stale_upload in UPLOADS.iterdir():
    if stale_upload.is_file() or stale_upload.is_symlink():
        stale_upload.unlink(missing_ok=True)

app = FastAPI(title="IndexTTS Studio", docs_url=None, redoc_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
)


def _is_local_origin(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
        "testserver",
    }


@app.middleware("http")
async def protect_local_mutations(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site == "cross-site" or (origin and not _is_local_origin(origin)):
            return JSONResponse(
                status_code=403,
                content={"detail": "只允许从本机 IndexTTS Studio 发起修改操作"},
            )
    return await call_next(request)


app.mount("/ui", StaticFiles(directory=STATIC), name="ui")
app.mount("/examples", StaticFiles(directory=EXAMPLES), name="examples")
app.mount(
    "/files/generations",
    StaticFiles(directory=GENERATIONS),
    name="generated-audio",
)
app.mount("/files/presets", StaticFiles(directory=PRESETS), name="preset-audio")

_model: MacIndexTTS2 | None = None
_model_loading = False
_model_error: str | None = None
_model_lock = threading.Lock()
_generation_lock = threading.Lock()
_active_generation_lock = threading.Lock()
_generation_status_lock = threading.Lock()
_examples_lock = threading.Lock()
_export_encoders_lock = threading.Lock()
_shutdown_requested = threading.Event()
_export_encoders: set[str] | None = None
_active_generation_job_id: str | None = None
_active_generation_cancel: threading.Event | None = None
_generation_status: dict[str, Any] = {
    "state": "idle",
    "message": "等待生成",
    "stage": "idle",
    "progress": 0.0,
    "updatedAt": int(time.time()),
    "startedAt": None,
    "filename": None,
    "error": None,
    "tokenProgress": {
        "processed": 0,
        "total": 0,
        "currentSegment": 0,
        "totalSegments": 0,
        "activeTokens": 0,
        "message": "等待生成",
    },
}


class GenerationCancelled(RuntimeError):
    """Raised inside the worker when the active local task is cancelled."""


def _claim_generation_job(job_id: str) -> threading.Event | None:
    global _active_generation_cancel, _active_generation_job_id
    with _active_generation_lock:
        if _active_generation_job_id is not None:
            return None
        _active_generation_job_id = job_id
        _active_generation_cancel = threading.Event()
        return _active_generation_cancel


def _release_generation_job(job_id: str) -> None:
    global _active_generation_cancel, _active_generation_job_id
    with _active_generation_lock:
        if _active_generation_job_id == job_id:
            _active_generation_job_id = None
            _active_generation_cancel = None


def _request_generation_cancel() -> str | None:
    with _active_generation_lock:
        if _active_generation_job_id is None or _active_generation_cancel is None:
            return None
        _active_generation_cancel.set()
        return _active_generation_job_id


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set() or _shutdown_requested.is_set():
        raise GenerationCancelled("生成已取消")


async def _run_in_daemon_thread(function, *args):
    """Run blocking model work without preventing the local process from exiting."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def worker() -> None:
        try:
            result = function(*args)
        except BaseException as error:

            def finish_error(captured=error) -> None:
                if not future.done():
                    future.set_exception(captured)

            callback = finish_error
        else:

            def finish_success(captured=result) -> None:
                if not future.done():
                    future.set_result(captured)

            callback = finish_success
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass

    threading.Thread(
        target=worker,
        name="indextts-studio-worker",
        daemon=True,
    ).start()
    try:
        while True:
            if _shutdown_requested.is_set():
                future.cancel()
                raise asyncio.CancelledError
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=0.1)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        future.cancel()
        raise


class StudioUvicornServer(uvicorn.Server):
    def handle_exit(self, sig, frame) -> None:
        _shutdown_requested.set()
        super().handle_exit(sig, frame)


def _ffmpeg_binary() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FileNotFoundError("ffmpeg")
    return executable


def _available_export_formats() -> list[dict[str, str]]:
    global _export_encoders
    with _export_encoders_lock:
        if _export_encoders is None:
            try:
                result = subprocess.run(
                    [_ffmpeg_binary(), "-hide_banner", "-encoders"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                _export_encoders = {
                    parts[1]
                    for line in result.stdout.splitlines()
                    if len((parts := line.split())) >= 2 and len(parts[0]) == 6
                }
            except (
                FileNotFoundError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ):
                _export_encoders = set()
    return [
        {
            "id": format_id,
            "label": config["label"],
            "description": config["description"],
            "extension": config["extension"],
        }
        for format_id, config in EXPORT_FORMATS.items()
        if config["encoder"] is None or config["encoder"] in _export_encoders
    ]


def _cleanup_export(path: Path) -> None:
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _normalize_generated_audio(path: Path) -> None:
    """Keep generated speech at a consistent, safe listening level."""
    normalized = path.with_name(f".{path.stem}.normalized.wav")
    try:
        subprocess.run(
            [
                _ffmpeg_binary(),
                "-y",
                "-v",
                "error",
                "-i",
                str(path),
                "-af",
                (
                    f"loudnorm=I={OUTPUT_TARGET_LOUDNESS_LUFS}:"
                    f"TP={OUTPUT_TRUE_PEAK_DBFS}:LRA=11"
                ),
                "-ac",
                "1",
                "-ar",
                "22050",
                "-c:a",
                "pcm_s16le",
                str(normalized),
            ],
            check=True,
            timeout=90,
        )
        if not normalized.is_file() or normalized.stat().st_size <= 44:
            raise RuntimeError("音量处理没有生成有效音频")
        normalized.replace(path)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        raise RuntimeError("生成音频音量处理失败，请检查 ffmpeg") from error
    finally:
        normalized.unlink(missing_ok=True)


def _checkpoint_integrity(*, verify_hashes: bool = False) -> dict[str, Any]:
    return inspect_model_directory(CHECKPOINTS, verify_hashes=verify_hashes)


def _missing_checkpoint_files() -> list[str]:
    return _checkpoint_integrity()["missing"]


def _invalid_checkpoint_files() -> list[dict[str, str]]:
    return _checkpoint_integrity()["invalid"]


def _model_state() -> str:
    if _model is not None:
        return "ready"
    if _missing_checkpoint_files():
        return "missing"
    if _invalid_checkpoint_files():
        return "invalid"
    if _model_loading:
        return "loading"
    if _model_error:
        return "error"
    return "idle"


def _load_model() -> MacIndexTTS2:
    global _model, _model_error, _model_loading
    missing = _missing_checkpoint_files()
    if missing:
        raise RuntimeError(
            f"模型权重未安装完整（缺少 {len(missing)} 个关键文件）。"
            "请先下载 IndexTeam/IndexTTS-2.5 模型权重。"
        )
    invalid = _invalid_checkpoint_files()
    if invalid:
        raise RuntimeError(
            f"模型权重校验失败（{len(invalid)} 个文件异常）。"
            "请重新下载 IndexTeam/IndexTTS-2.5 模型权重。"
        )
    with _model_lock:
        if _model is None:
            _model_loading = True
            _model_error = None
            try:
                _model = MacIndexTTS2(
                    cfg_path=str(CHECKPOINTS / "config.yaml"),
                    model_dir=str(CHECKPOINTS),
                    use_bf16=False,
                    use_cuda_kernel=False,
                    use_qwen_emo=True,
                )
            except Exception as error:
                _model_error = str(error)
                raise
            finally:
                _model_loading = False
    return _model


def _set_generation_status(**changes: Any) -> None:
    with _generation_status_lock:
        _generation_status.update(changes)
        _generation_status["updatedAt"] = int(time.time())


def _generation_snapshot() -> dict[str, Any]:
    with _generation_status_lock:
        return dict(_generation_status)


def _reset_generation_status(message: str = "等待生成") -> None:
    _set_generation_status(
        state="idle",
        message=message,
        startedAt=None,
        filename=None,
        error=None,
        jobId=None,
        stage="idle",
        progress=0.0,
        tokenProgress={
            "processed": 0,
            "total": 0,
            "currentSegment": 0,
            "totalSegments": 0,
            "activeTokens": 0,
            "message": message,
        },
    )


def _url_for(path: str | Path | None) -> str | None:
    if not path:
        return None
    absolute = Path(path).resolve()
    for directory, prefix in (
        (GENERATIONS, "/files/generations/"),
        (PRESETS, "/files/presets/"),
        (EXAMPLES, "/examples/"),
    ):
        try:
            return prefix + quote(str(absolute.relative_to(directory)))
        except ValueError:
            continue
    return None


def _generation_files() -> list[Path]:
    return sorted(
        (path for path in GENERATIONS.glob("*.wav") if path.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def _history_summary() -> dict[str, int]:
    files = _generation_files()
    return {
        "count": len(files),
        "totalBytes": sum(path.stat().st_size for path in files),
        "maxItems": MAX_HISTORY_ITEMS,
        "maxBytes": MAX_HISTORY_BYTES,
    }


def _prune_generation_history(protected: Path | None = None) -> list[str]:
    protected_path = protected.resolve() if protected else None
    removed: list[str] = []
    retained_count = 0
    retained_bytes = 0
    for path in _generation_files():
        resolved = path.resolve()
        size = path.stat().st_size
        keep = resolved == protected_path or (
            retained_count < MAX_HISTORY_ITEMS
            and retained_bytes + size <= MAX_HISTORY_BYTES
        )
        if keep:
            retained_count += 1
            retained_bytes += size
            continue
        path.unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def _generation_path(filename: str) -> Path:
    safe_filename = Path(filename).name
    if safe_filename != filename or not safe_filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="无效的历史文件名")
    path = (GENERATIONS / safe_filename).resolve()
    try:
        path.relative_to(GENERATIONS)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="无效的历史文件名") from error
    return path


_prune_generation_history()


def _save_upload(
    upload: UploadFile | None,
    label: str,
    *,
    start_seconds: float = 0.0,
) -> str | None:
    if upload is None or not upload.filename:
        return None
    extension = Path(upload.filename).suffix.lower() or ".wav"
    if extension not in SUPPORTED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=400, detail="请导入常见音频或视频格式")
    is_video = (upload.content_type or "").lower().startswith("video/") or (
        extension in VIDEO_MEDIA_EXTENSIONS
    )
    max_upload_bytes = MAX_VIDEO_UPLOAD_BYTES if is_video else MAX_AUDIO_UPLOAD_BYTES
    size_error = "视频文件不能超过 1 GB" if is_video else "音频文件不能超过 100 MB"
    stem = f"{int(time.time())}-{label}-{uuid.uuid4().hex[:8]}"
    source = UPLOADS / f"{stem}.source{extension}"
    converted = UPLOADS / f"{stem}.wav"
    try:
        size = 0
        with source.open("wb") as destination:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_upload_bytes:
                    raise HTTPException(status_code=413, detail=size_error)
                destination.write(chunk)
        # Browsers choose different recording containers. Always convert on the
        # server so local import and direct recording produce the same model input.
        subprocess.run(
            [
                _ffmpeg_binary(),
                "-y",
                "-v",
                "error",
                "-ss",
                f"{start_seconds:.3f}",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-t",
                f"{REFERENCE_WINDOW_SECONDS:g}",
                "-ac",
                "1",
                "-ar",
                "22050",
                str(converted),
            ],
            check=True,
            timeout=60,
        )
    except HTTPException:
        source.unlink(missing_ok=True)
        converted.unlink(missing_ok=True)
        raise
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        source.unlink(missing_ok=True)
        converted.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="无法读取该文件中的声音，请换一个音频或视频文件",
        ) from error
    source.unlink(missing_ok=True)
    return str(converted)


def _remove_temporary_uploads(*paths: str | None) -> None:
    for path in paths:
        if not path:
            continue
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(UPLOADS)
        except ValueError:
            continue
        candidate.unlink(missing_ok=True)


def _dbfs(value: float) -> float:
    return round(20 * math.log10(max(value, 1e-12)), 1)


def _analyze_reference_audio(path: str | Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        width = source.getsampwidth()
        frames = source.getnframes()
        raw = source.readframes(frames)
    if width != 2 or channels != 1 or rate <= 0:
        raise RuntimeError("参考声音格式分析失败")
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    normalized = [sample / 32768.0 for sample in samples]
    duration = frames / rate if rate else 0.0
    if not normalized:
        return {
            "quality": "poor",
            "duration": 0.0,
            "rmsDbfs": -120.0,
            "peakDbfs": -120.0,
            "silenceRatio": 1.0,
            "clippingRatio": 0.0,
            "issues": ["no-audible-speech"],
            "fatal": True,
        }

    rms = math.sqrt(sum(value * value for value in normalized) / len(normalized))
    peak = max(abs(value) for value in normalized)
    clipping_ratio = sum(abs(value) >= 0.99 for value in normalized) / len(normalized)
    window_size = max(1, rate // 10)
    silent_windows = 0
    window_count = 0
    for start in range(0, len(normalized), window_size):
        window = normalized[start : start + window_size]
        if not window:
            continue
        window_count += 1
        window_rms = math.sqrt(sum(value * value for value in window) / len(window))
        if _dbfs(window_rms) <= -45:
            silent_windows += 1
    silence_ratio = silent_windows / window_count if window_count else 1.0
    rms_dbfs = _dbfs(rms)
    peak_dbfs = _dbfs(peak)
    issues: list[str] = []
    fatal = False

    if duration < 0.8:
        issues.append("too-short")
        fatal = True
    elif duration < 3:
        issues.append("very-short")
    elif duration < 8:
        issues.append("short")
    if rms_dbfs <= -55 or silence_ratio >= 0.95:
        issues.append("no-audible-speech")
        fatal = True
    elif rms_dbfs < -40:
        issues.append("too-quiet")
    elif rms_dbfs < -30:
        issues.append("quiet")
    if clipping_ratio > 0.02:
        issues.append("heavy-clipping")
    elif clipping_ratio > 0.001:
        issues.append("clipping")
    if silence_ratio > 0.6 and "no-audible-speech" not in issues:
        issues.append("much-silence")
    elif silence_ratio > 0.25:
        issues.append("some-silence")

    poor_codes = {
        "too-short",
        "no-audible-speech",
        "too-quiet",
        "heavy-clipping",
        "much-silence",
    }
    quality = (
        "poor"
        if any(code in poor_codes for code in issues)
        else "warning"
        if issues
        else "good"
    )
    return {
        "quality": quality,
        "duration": round(duration, 2),
        "rmsDbfs": rms_dbfs,
        "peakDbfs": peak_dbfs,
        "silenceRatio": round(silence_ratio, 4),
        "clippingRatio": round(clipping_ratio, 6),
        "issues": issues,
        "fatal": fatal,
    }


def _split_narration_units(
    text: str,
    sentence_pause_ms: int,
    paragraph_pause_ms: int,
) -> list[tuple[str, int]]:
    """Split a script into spoken sentences with explicit following pauses."""
    units: list[tuple[str, int]] = []
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n+", text.strip())
        if paragraph.strip()
    ]
    for paragraph in paragraphs:
        sentences = [
            value.strip()
            for value in re.findall(r".*?[。！？!?]+[”’\"']*|.+$", paragraph, re.DOTALL)
            if value.strip()
        ]
        for index, sentence in enumerate(sentences):
            pause_ms = (
                paragraph_pause_ms if index == len(sentences) - 1 else sentence_pause_ms
            )
            units.append((sentence, pause_ms))
    if units:
        units[-1] = (units[-1][0], 0)
    return units


def _concatenate_wavs_with_pauses(
    parts: list[tuple[Path, int]],
    output_path: Path,
) -> None:
    """Concatenate compatible PCM WAV files and insert the requested silences."""
    if not parts:
        raise RuntimeError("没有可拼接的自然口播片段")
    expected_format: tuple[int, int, int, str] | None = None
    try:
        with wave.open(str(output_path), "wb") as destination:
            for path, pause_ms in parts:
                with wave.open(str(path), "rb") as source:
                    audio_format = (
                        source.getnchannels(),
                        source.getsampwidth(),
                        source.getframerate(),
                        source.getcomptype(),
                    )
                    if expected_format is None:
                        expected_format = audio_format
                        destination.setnchannels(audio_format[0])
                        destination.setsampwidth(audio_format[1])
                        destination.setframerate(audio_format[2])
                        destination.setcomptype(audio_format[3], source.getcompname())
                    elif audio_format != expected_format:
                        raise RuntimeError("自然口播片段的音频格式不一致")
                    destination.writeframes(source.readframes(source.getnframes()))
                if pause_ms > 0 and expected_format is not None:
                    silence_frames = expected_format[2] * pause_ms // 1000
                    destination.writeframes(
                        b"\0" * expected_format[0] * expected_format[1] * silence_frames
                    )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def _settings_payload(raw: str) -> dict[str, Any]:
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="设置格式无效") from error
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="设置格式无效")
    return settings


def _normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    def number(
        source: dict[str, Any],
        key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            value = float(source.get(key, default))
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=400, detail=f"设置项 {key} 不是有效数字"
            ) from error
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise HTTPException(
                status_code=400,
                detail=f"设置项 {key} 必须在 {minimum:g} 到 {maximum:g} 之间",
            )
        return value

    def integer(
        source: dict[str, Any],
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = number(source, key, default, minimum, maximum)
        if not value.is_integer():
            raise HTTPException(status_code=400, detail=f"设置项 {key} 必须是整数")
        return int(value)

    def boolean(source: dict[str, Any], key: str, default: bool) -> bool:
        value = source.get(key, default)
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"设置项 {key} 必须是开或关")
        return value

    try:
        mode = int(settings.get("emotionMode", 0))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="情绪方式无效") from error
    if mode not in (0, 1, 2, 3):
        raise HTTPException(status_code=400, detail="情绪方式无效")
    vector = settings.get("emotionVector", [0.0] * 8)
    if not isinstance(vector, list):
        raise HTTPException(status_code=400, detail="情绪强度设置无效")
    normalized_vector = []
    for index, raw_value in enumerate((vector + [0.0] * 8)[:8], start=1):
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=400, detail=f"第 {index} 个情绪强度无效"
            ) from error
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise HTTPException(
                status_code=400, detail=f"第 {index} 个情绪强度必须在 0 到 1 之间"
            )
        normalized_vector.append(value)
    advanced = settings.get("advanced", {})
    if not isinstance(advanced, dict):
        raise HTTPException(status_code=400, detail="精细设置格式无效")
    language = str(settings.get("language", "ZH")).upper()
    if language not in ALLOWED_LANGUAGES:
        raise HTTPException(status_code=400, detail="生成语言无效")
    return {
        "emotionMode": mode,
        "emotionWeight": number(settings, "emotionWeight", 0.65, 0.0, 1.0),
        "emotionVector": normalized_vector,
        "emotionText": str(settings.get("emotionText", "")).strip(),
        "emotionRandom": boolean(settings, "emotionRandom", False),
        "referenceStart": number(
            settings,
            "referenceStart",
            0.0,
            0.0,
            MAX_REFERENCE_START_SECONDS,
        ),
        "language": language,
        "duration": number(settings, "duration", 1.0, 0.5, 2.0),
        "segmentTokens": integer(settings, "segmentTokens", 120, 20, 600),
        "naturalPacing": boolean(settings, "naturalPacing", False),
        "sentencePauseMs": integer(settings, "sentencePauseMs", 320, 0, 2000),
        "paragraphPauseMs": integer(settings, "paragraphPauseMs", 650, 0, 4000),
        "advanced": {
            "doSample": boolean(advanced, "doSample", True),
            "topP": number(advanced, "topP", 0.8, 0.0, 1.0),
            "topK": integer(advanced, "topK", 30, 0, 100),
            "temperature": number(advanced, "temperature", 0.8, 0.1, 2.0),
            "lengthPenalty": number(advanced, "lengthPenalty", 0.0, -2.0, 2.0),
            "numBeams": integer(advanced, "numBeams", 3, 1, 10),
            "repetitionPenalty": number(advanced, "repetitionPenalty", 10.0, 0.1, 20.0),
            "maxMelTokens": integer(advanced, "maxMelTokens", 1500, 50, 1815),
        },
    }


def _preset_response(name: str) -> dict[str, Any]:
    data = load_preset(name)
    if data is None:
        raise HTTPException(status_code=404, detail="未找到该音色预设")
    prompt_path = data.pop("prompt_audio", None)
    emotion_path = data.pop("emo_audio", None)
    data["name"] = name
    data["promptAudioUrl"] = _url_for(prompt_path)
    data["emotionAudioUrl"] = _url_for(emotion_path)
    return data


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    missing = _missing_checkpoint_files()
    invalid = _invalid_checkpoint_files()
    return {
        "ready": _model is not None,
        "modelAvailable": not missing and not invalid,
        "modelState": _model_state(),
        "model": "IndexTTS 2.5",
        "localOnly": True,
        "missingModelFiles": missing,
        "invalidModelFiles": invalid,
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Return real generation state so a reloaded workbench can recover its preview."""
    snapshot = _generation_snapshot()
    if (
        snapshot.get("state") == "complete"
        and snapshot.get("filename")
        and not (GENERATIONS / snapshot["filename"]).exists()
    ):
        _reset_generation_status()
        snapshot = _generation_snapshot()
    missing = _missing_checkpoint_files()
    invalid = _invalid_checkpoint_files()
    return {
        "model": "IndexTTS-2.5",
        "modelReady": _model is not None,
        "modelAvailable": not missing and not invalid,
        "modelState": _model_state(),
        "missingModelFiles": missing,
        "invalidModelFiles": invalid,
        "generation": snapshot,
    }


@app.post("/api/model-integrity")
async def verify_model_integrity() -> dict[str, Any]:
    return await _run_in_daemon_thread(
        lambda: _checkpoint_integrity(verify_hashes=True)
    )


@app.post("/api/generation/cancel")
def cancel_generation() -> dict[str, Any]:
    job_id = _request_generation_cancel()
    if job_id is None:
        return {"cancelled": False, "state": "idle", "jobId": None}
    _set_generation_status(
        state="cancelling",
        message="正在取消生成",
        stage="cancelling",
        jobId=job_id,
    )
    return {"cancelled": True, "state": "cancelling", "jobId": job_id}


@app.get("/api/status-stream")
async def status_stream() -> StreamingResponse:
    async def events():
        previous = None
        started = time.monotonic()
        sent_retry = False
        # EventSource reconnects automatically. Bounding each response keeps a
        # live browser tab from blocking a clean local-server shutdown.
        while time.monotonic() - started < 4.0:
            payload = status()
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if serialized != previous:
                retry = "retry: 750\n" if not sent_retry else ""
                yield f"data: {serialized}\n{retry}\n"
                sent_retry = True
                previous = serialized
            await asyncio.sleep(0.35)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/export-formats")
def export_formats() -> list[dict[str, str]]:
    return _available_export_formats()


@app.get("/api/export/{filename}")
def export_audio(filename: str, format: str = "wav") -> FileResponse:
    safe_filename = Path(filename).name
    if safe_filename != filename or Path(safe_filename).suffix.lower() != ".wav":
        raise HTTPException(status_code=400, detail="生成文件名称无效")
    source = (GENERATIONS / safe_filename).resolve()
    try:
        source.relative_to(GENERATIONS)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="生成文件名称无效") from error
    if not source.is_file():
        raise HTTPException(status_code=404, detail="未找到该生成音频")

    format_id = format.lower()
    config = EXPORT_FORMATS.get(format_id)
    available_ids = {item["id"] for item in _available_export_formats()}
    if config is None or format_id not in available_ids:
        raise HTTPException(status_code=400, detail="当前 ffmpeg 不支持该导出格式")
    download_name = f"{source.stem}.{config['extension']}"
    if format_id == "wav":
        return FileResponse(
            source,
            media_type=config["mediaType"],
            filename=download_name,
        )

    export_dir = Path(tempfile.mkdtemp(prefix="indextts-export-"))
    converted = export_dir / download_name
    try:
        subprocess.run(
            [
                _ffmpeg_binary(),
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                *config["arguments"],
                str(converted),
            ],
            check=True,
            timeout=90,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        _cleanup_export(converted)
        raise HTTPException(
            status_code=500, detail="音频格式转换失败，请检查 ffmpeg"
        ) from error
    return FileResponse(
        converted,
        media_type=config["mediaType"],
        filename=download_name,
        background=BackgroundTask(_cleanup_export, converted),
    )


@app.get("/api/presets")
def presets() -> list[str]:
    return list_presets()


@app.get("/api/presets/{name}")
def preset(name: str) -> dict[str, Any]:
    return _preset_response(name)


@app.delete("/api/presets/{name}")
def remove_preset(name: str) -> dict[str, bool]:
    return {"deleted": delete_preset(name)}


@app.post("/api/presets")
async def create_preset(
    name: str = Form(...),
    settings: str = Form(...),
    overwrite: bool = Form(False),
    prompt_audio: UploadFile | None = File(None),
    emotion_audio: UploadFile | None = File(None),
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="请先给这个声音取一个名字")
    if len(clean_name) > MAX_PRESET_NAME_CHARS:
        raise HTTPException(status_code=400, detail="声音名称不能超过 60 个字符")
    if preset_exists(clean_name) and not overwrite:
        raise HTTPException(
            status_code=409,
            detail="已存在同名声音，请确认是否覆盖",
        )
    prompt_path = None
    emotion_path = None
    try:
        normalized = _normalize_settings(_settings_payload(settings))
        prompt_path = _save_upload(
            prompt_audio,
            "preset-voice",
            start_seconds=normalized["referenceStart"],
        )
        if not prompt_path:
            raise HTTPException(status_code=400, detail="请先添加参考声音")
        emotion_path = _save_upload(emotion_audio, "preset-emotion")
        save_preset(
            clean_name,
            {
                "emo_control_method": normalized["emotionMode"],
                "emo_weight": normalized["emotionWeight"],
                "emo_vector": normalized["emotionVector"],
                "emo_text": normalized["emotionText"],
                "emo_random": normalized["emotionRandom"],
                # The stored prompt is already the selected 15-second window.
                "reference_start": 0.0,
                "language": normalized["language"],
                "duration_factor": normalized["duration"],
                "natural_pacing": normalized["naturalPacing"],
                "sentence_pause_ms": normalized["sentencePauseMs"],
                "paragraph_pause_ms": normalized["paragraphPauseMs"],
                "advanced_params": {
                    "do_sample": normalized["advanced"]["doSample"],
                    "top_p": normalized["advanced"]["topP"],
                    "top_k": normalized["advanced"]["topK"],
                    "temperature": normalized["advanced"]["temperature"],
                    "length_penalty": normalized["advanced"]["lengthPenalty"],
                    "num_beams": normalized["advanced"]["numBeams"],
                    "repetition_penalty": normalized["advanced"]["repetitionPenalty"],
                    "max_mel_tokens": normalized["advanced"]["maxMelTokens"],
                    "max_text_tokens_per_segment": normalized["segmentTokens"],
                },
            },
            prompt_audio=prompt_path,
            emo_audio=emotion_path,
        )
        return _preset_response(clean_name)
    finally:
        _remove_temporary_uploads(prompt_path, emotion_path)


@app.post("/api/reference-quality")
async def reference_quality(
    reference_start: float = Form(0.0),
    prompt_audio: UploadFile | None = File(None),
) -> dict[str, Any]:
    if not math.isfinite(reference_start):
        reference_start = 0.0
    reference_start = max(0.0, min(reference_start, MAX_REFERENCE_START_SECONDS))
    prompt_path = None
    try:
        prompt_path = _save_upload(
            prompt_audio,
            "quality",
            start_seconds=reference_start,
        )
        if not prompt_path:
            raise HTTPException(status_code=400, detail="请先添加参考声音")
        return _analyze_reference_audio(prompt_path)
    finally:
        _remove_temporary_uploads(prompt_path)


@app.post("/api/segments")
async def segments(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", "")).strip()
    if not text:
        return {"segments": []}
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"台词不能超过 {MAX_TEXT_CHARS} 个字符",
        )
    normalized = _normalize_settings(payload)
    try:
        model = await _run_in_daemon_thread(_load_model)
    except asyncio.CancelledError as error:
        raise HTTPException(
            status_code=503, detail="服务正在停止，请稍后重新启动"
        ) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    language = normalized["language"].lower()
    max_tokens = max(
        20, min(normalized["segmentTokens"], model.cfg.gpt.max_text_tokens)
    )
    prefix = f"<|{language}|> "
    if normalized["naturalPacing"]:
        values_with_pauses: list[tuple[str, int]] = []
        for value, pause_ms in _split_narration_units(
            text,
            normalized["sentencePauseMs"],
            normalized["paragraphPauseMs"],
        ):
            pieces = model.split_text_by_tokens(value, max_tokens, prefix)
            for index, piece in enumerate(pieces):
                values_with_pauses.append(
                    (
                        piece,
                        pause_ms
                        if index == len(pieces) - 1
                        else min(normalized["sentencePauseMs"], 200),
                    )
                )
    else:
        values_with_pauses = [
            (value, 200)
            for value in model.split_text_by_tokens(text, max_tokens, prefix)
        ]
        if values_with_pauses:
            values_with_pauses[-1] = (values_with_pauses[-1][0], 0)
    return {
        "segments": [
            {
                "index": index + 1,
                "text": value,
                "tokens": len(
                    model.tokenizer.encode(prefix + value, allowed_special="all")
                ),
                "pauseMs": pause_ms,
            }
            for index, (value, pause_ms) in enumerate(values_with_pauses)
        ]
    }


@app.post("/api/generate")
async def generate(
    text: str = Form(...),
    settings: str = Form(...),
    prompt_audio: UploadFile | None = File(None),
    emotion_audio: UploadFile | None = File(None),
) -> dict[str, Any]:
    clean_text = text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="请输入台词")
    if len(clean_text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"台词不能超过 {MAX_TEXT_CHARS} 个字符",
        )
    job_id = uuid.uuid4().hex[:8]
    cancel_event = _claim_generation_job(job_id)
    if cancel_event is None:
        raise HTTPException(
            status_code=409,
            detail="已有生成任务正在运行，请等待完成或先取消当前任务",
        )
    prompt_path = None
    emotion_path = None
    reference_analysis: dict[str, Any] | None = None
    try:
        normalized = _normalize_settings(_settings_payload(settings))
        prompt_path = _save_upload(
            prompt_audio,
            "voice",
            start_seconds=normalized["referenceStart"],
        )
        if not prompt_path:
            raise HTTPException(status_code=400, detail="请上传或录制参考声音")
        reference_analysis = _analyze_reference_audio(prompt_path)
        if reference_analysis["fatal"]:
            raise HTTPException(
                status_code=400,
                detail="参考声音中没有足够的清晰人声，请重新录制或选择片段",
            )
        emotion_path = _save_upload(emotion_audio, "emotion")
    except Exception:
        _remove_temporary_uploads(prompt_path, emotion_path)
        _release_generation_job(job_id)
        raise
    output_path = GENERATIONS / f"voice-{int(time.time())}-{uuid.uuid4().hex[:8]}.wav"
    _set_generation_status(
        state="queued",
        message="已收到任务，正在等待模型",
        stage="queued",
        progress=0.0,
        startedAt=int(time.time()),
        filename=output_path.name,
        error=None,
        jobId=job_id,
        tokenProgress={
            "processed": 0,
            "total": 0,
            "currentSegment": 0,
            "totalSegments": 0,
            "activeTokens": 0,
            "message": "等待模型",
        },
    )

    def run() -> None:
        token_counts: list[int] = []
        try:
            _raise_if_cancelled(cancel_event)
            model = _load_model()
            _raise_if_cancelled(cancel_event)
            advanced = normalized["advanced"]
            vector = (
                model.normalize_emo_vec(normalized["emotionVector"], apply_bias=True)
                if normalized["emotionMode"] == 2
                else None
            )
            with _generation_lock:
                _raise_if_cancelled(cancel_event)
                prefix = f"<|{normalized['language'].lower()}|> "
                max_tokens = max(
                    20,
                    min(normalized["segmentTokens"], model.cfg.gpt.max_text_tokens),
                )
                if normalized["naturalPacing"]:
                    planned_units: list[tuple[str, int]] = []
                    for value, pause_ms in _split_narration_units(
                        text,
                        normalized["sentencePauseMs"],
                        normalized["paragraphPauseMs"],
                    ):
                        pieces = model.split_text_by_tokens(value, max_tokens, prefix)
                        for index, piece in enumerate(pieces):
                            planned_units.append(
                                (
                                    piece,
                                    pause_ms
                                    if index == len(pieces) - 1
                                    else min(normalized["sentencePauseMs"], 200),
                                )
                            )
                    if not planned_units:
                        raise RuntimeError("没有可生成的自然口播句子")
                    planned_units[-1] = (planned_units[-1][0], 0)
                    planned_segments = [value for value, _ in planned_units]
                else:
                    planned_units = []
                    planned_segments = model.split_text_by_tokens(
                        clean_text, max_tokens, prefix
                    )
                token_counts = [
                    len(model.tokenizer.encode(prefix + segment, allowed_special="all"))
                    for segment in planned_segments
                ]
                total_tokens = sum(token_counts)
                total_segments = len(token_counts)

                _set_generation_status(
                    state="generating",
                    message="正在计算输入 Token",
                    stage="text",
                    progress=0.05,
                    jobId=job_id,
                    tokenProgress={
                        "processed": 0,
                        "total": total_tokens,
                        "currentSegment": 0,
                        "totalSegments": total_segments,
                        "activeTokens": 0,
                        "message": "正在计算输入 Token",
                    },
                )

                def infer_to_path(
                    segment_text: str,
                    segment_output: Path,
                    progress_callback,
                    *,
                    interval_silence: int,
                ) -> None:
                    previous_progress = model.gr_progress
                    model.gr_progress = progress_callback
                    try:
                        _raise_if_cancelled(cancel_event)
                        model.infer(
                            spk_audio_prompt=prompt_path,
                            text=segment_text,
                            lang=normalized["language"],
                            output_path=str(segment_output),
                            emo_audio_prompt=emotion_path
                            if normalized["emotionMode"] == 1
                            else None,
                            emo_alpha=normalized["emotionWeight"],
                            emo_vector=vector,
                            use_emo_text=normalized["emotionMode"] == 3,
                            emo_text=normalized["emotionText"] or None,
                            use_random=normalized["emotionRandom"],
                            interval_silence=interval_silence,
                            max_text_tokens_per_segment=normalized["segmentTokens"],
                            duration_factor=normalized["duration"],
                            do_sample=advanced["doSample"],
                            top_p=advanced["topP"],
                            top_k=advanced["topK"] or None,
                            temperature=advanced["temperature"],
                            length_penalty=advanced["lengthPenalty"],
                            num_beams=advanced["numBeams"],
                            repetition_penalty=advanced["repetitionPenalty"],
                            max_mel_tokens=advanced["maxMelTokens"],
                            verbose=False,
                        )
                    finally:
                        model.gr_progress = previous_progress

                if normalized["naturalPacing"]:
                    temporary_directory = Path(
                        tempfile.mkdtemp(prefix="indextts-natural-pacing-")
                    )
                    parts: list[tuple[Path, int]] = []
                    try:
                        for unit_index, (segment_text, pause_ms) in enumerate(
                            planned_units
                        ):
                            _raise_if_cancelled(cancel_event)
                            current_segment = unit_index + 1
                            active_tokens = token_counts[unit_index]
                            processed_before = sum(token_counts[:unit_index])

                            def on_unit_progress(
                                value: float,
                                desc: str = "",
                                *,
                                current_segment=current_segment,
                                active_tokens=active_tokens,
                                processed_before=processed_before,
                                unit_index=unit_index,
                            ) -> None:
                                _raise_if_cancelled(cancel_event)
                                message = (
                                    f"第 {current_segment}/{total_segments} 段"
                                    f" · 当前 {active_tokens} Token"
                                )
                                stage = "tokens"
                                processed = processed_before
                                if "text processing" in (desc or ""):
                                    message = (
                                        f"第 {current_segment}/{total_segments} 段"
                                        " · 正在计算 Token"
                                    )
                                    stage = "text"
                                elif "saving audio" in (desc or ""):
                                    processed += active_tokens
                                    message = (
                                        f"第 {current_segment}/{total_segments} 段"
                                        " · 正在保存音频"
                                    )
                                    stage = "saving"
                                local_progress = max(0.0, min(float(value), 1.0))
                                _set_generation_status(
                                    state="generating",
                                    message=message,
                                    stage=stage,
                                    progress=(unit_index + local_progress)
                                    / total_segments,
                                    jobId=job_id,
                                    tokenProgress={
                                        "processed": processed,
                                        "total": total_tokens,
                                        "currentSegment": current_segment,
                                        "totalSegments": total_segments,
                                        "activeTokens": active_tokens,
                                        "message": message,
                                    },
                                )

                            segment_output = temporary_directory / (
                                f"segment-{current_segment:04d}.wav"
                            )
                            infer_to_path(
                                segment_text,
                                segment_output,
                                on_unit_progress,
                                interval_silence=0,
                            )
                            if (
                                not segment_output.is_file()
                                or segment_output.stat().st_size <= 44
                            ):
                                raise RuntimeError(
                                    f"第 {current_segment} 段没有生成有效音频"
                                )
                            parts.append((segment_output, pause_ms))
                        _concatenate_wavs_with_pauses(parts, output_path)
                    finally:
                        shutil.rmtree(temporary_directory, ignore_errors=True)
                else:

                    def on_progress(value: float, desc: str = "") -> None:
                        _raise_if_cancelled(cancel_event)
                        processed = 0
                        current_segment = 0
                        active_tokens = 0
                        message = "正在准备生成"
                        stage = "preparing"
                        segment_match = re.search(
                            r"speech synthesis (\d+)/(\d+)", desc or ""
                        )
                        if segment_match:
                            current_segment = int(segment_match.group(1))
                            processed = sum(token_counts[: current_segment - 1])
                            if 0 < current_segment <= total_segments:
                                active_tokens = token_counts[current_segment - 1]
                            message = (
                                f"第 {current_segment}/{total_segments} 段"
                                f" · 当前 {active_tokens} Token"
                            )
                            stage = "tokens"
                        elif "text processing" in (desc or ""):
                            message = "正在计算输入 Token"
                            stage = "text"
                        elif "saving audio" in (desc or ""):
                            processed = total_tokens
                            message = "输入 Token 已处理，正在保存音频"
                            stage = "saving"
                        _set_generation_status(
                            state="generating",
                            message=message,
                            stage=stage,
                            progress=max(0.0, min(float(value), 1.0)),
                            jobId=job_id,
                            tokenProgress={
                                "processed": processed,
                                "total": total_tokens,
                                "currentSegment": current_segment,
                                "totalSegments": total_segments,
                                "activeTokens": active_tokens,
                                "message": message,
                            },
                        )

                    infer_to_path(
                        clean_text,
                        output_path,
                        on_progress,
                        interval_silence=200,
                    )
                _raise_if_cancelled(cancel_event)
                if not output_path.is_file() or output_path.stat().st_size <= 44:
                    raise RuntimeError(
                        "模型没有生成有效音频，请调整台词或参考声音后重试"
                    )
                _set_generation_status(
                    state="generating",
                    message="正在调整播放音量",
                    stage="normalizing",
                    progress=0.95,
                    jobId=job_id,
                )
                _normalize_generated_audio(output_path)
                _raise_if_cancelled(cancel_event)
        except GenerationCancelled:
            output_path.unlink(missing_ok=True)
            _set_generation_status(
                state="cancelled",
                message="生成已取消",
                stage="cancelled",
                error=None,
                filename=None,
                jobId=job_id,
            )
            raise
        except Exception as error:
            output_path.unlink(missing_ok=True)
            _set_generation_status(
                state="failed",
                message="生成失败",
                stage="failed",
                error=str(error),
                jobId=job_id,
            )
            raise
        else:
            total_tokens = sum(token_counts)
            _prune_generation_history(protected=output_path)
            _set_generation_status(
                state="complete",
                message="音频已生成",
                stage="complete",
                progress=1.0,
                filename=output_path.name,
                error=None,
                jobId=job_id,
                tokenProgress={
                    "processed": total_tokens,
                    "total": total_tokens,
                    "currentSegment": len(token_counts),
                    "totalSegments": len(token_counts),
                    "activeTokens": 0,
                    "message": "输入 Token 已处理",
                },
            )
        finally:
            _remove_temporary_uploads(prompt_path, emotion_path)
            _release_generation_job(job_id)

    try:
        await _run_in_daemon_thread(run)
        return {
            "audioUrl": _url_for(output_path),
            "filename": output_path.name,
            "referenceQuality": reference_analysis,
        }
    except asyncio.CancelledError as error:
        cancel_event.set()
        raise HTTPException(
            status_code=503, detail="服务正在停止，请稍后重新启动"
        ) from error
    except GenerationCancelled as error:
        raise HTTPException(status_code=409, detail="生成已取消") from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"生成失败：{error}") from error


@app.get("/api/examples")
def examples() -> list[dict[str, Any]]:
    with _examples_lock:
        ensure_examples_available()
    cases: list[dict[str, Any]] = []
    with (EXAMPLES / "cases.jsonl").open(encoding="utf-8") as source:
        for index, line in enumerate(source, start=1):
            item = json.loads(line)
            prompt_name = item.get("prompt_audio", "voice_01.wav")
            emotion_name = item.get("emo_audio")
            available = (EXAMPLES / prompt_name).is_file() and (
                not emotion_name or (EXAMPLES / emotion_name).is_file()
            )
            cases.append(
                {
                    "name": f"案例 {index:02d} · {item.get('lang', 'ZH')}",
                    "audio": f"/examples/{quote(prompt_name)}",
                    "emotionAudio": f"/examples/{quote(emotion_name)}"
                    if emotion_name
                    else None,
                    "available": available,
                    "text": item.get("text", ""),
                    "language": item.get("lang", "ZH"),
                    "mode": int(item.get("emo_mode", 0)),
                    "emotionWeight": float(item.get("emo_weight", 0.65)),
                    "emotionText": item.get("emo_text", ""),
                    "emotionVector": [
                        float(item.get(f"emo_vec_{vector_index}", 0.0))
                        for vector_index in range(1, 9)
                    ],
                }
            )
    return cases


@app.get("/api/history")
def history() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    _prune_generation_history()
    for path in _generation_files():
        try:
            with wave.open(str(path), "rb") as source:
                duration = round(source.getnframes() / source.getframerate(), 2)
        except (wave.Error, OSError):
            duration = None
        records.append(
            {
                "name": path.name,
                "audioUrl": _url_for(path),
                "duration": duration,
                "createdAt": int(path.stat().st_mtime),
            }
        )
    return records


@app.get("/api/history-summary")
def history_summary() -> dict[str, int]:
    _prune_generation_history()
    return _history_summary()


@app.delete("/api/history/{filename}")
def remove_history_item(filename: str) -> dict[str, Any]:
    path = _generation_path(filename)
    snapshot = _generation_snapshot()
    if snapshot.get("filename") == path.name and snapshot.get("state") in {
        "queued",
        "generating",
        "cancelling",
    }:
        raise HTTPException(status_code=409, detail="当前任务仍在使用这个文件")
    deleted = path.is_file()
    path.unlink(missing_ok=True)
    if snapshot.get("filename") == path.name:
        _reset_generation_status()
    return {"deleted": deleted, "filename": path.name, **_history_summary()}


@app.delete("/api/history")
def clear_history() -> dict[str, Any]:
    snapshot = _generation_snapshot()
    protected_name = (
        snapshot.get("filename")
        if snapshot.get("state") in {"queued", "generating", "cancelling"}
        else None
    )
    removed = 0
    for path in _generation_files():
        if protected_name and path.name == protected_name:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    if not protected_name:
        _reset_generation_status()
    return {"deleted": removed, **_history_summary()}


if __name__ == "__main__":
    port = int(os.environ.get("INDEXTTS_STUDIO_PORT", "7860"))
    _shutdown_requested.clear()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
    try:
        StudioUvicornServer(config).run()
    except KeyboardInterrupt:
        pass
