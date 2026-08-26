from __future__ import annotations

from array import array
import ast
import asyncio
from copy import deepcopy
from html.parser import HTMLParser
from io import BytesIO
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from fastapi import HTTPException, UploadFile
from fastapi.routing import APIRoute
from starlette.datastructures import Headers

import studio_server
from indextts.infer_v2_5 import IndexTTS2, apply_pronunciation_annotations
from indextts.utils.presets import safe_preset_name


ROOT = Path(__file__).resolve().parents[1]


def test_audio_dependency_imports_are_free_of_known_deprecation_warnings() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"librosa==0.11.0"' in project
    assert '"sentencepiece>=0.2.2"' in project
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import sentencepiece; import librosa.core.intervals",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _translation_rows(source: str) -> list[list[str]]:
    block = re.search(
        r"const rows = \[(.*?)\];\s*\n\s*const localeNames",
        source,
        re.DOTALL,
    )
    assert block
    rows = ast.literal_eval("[" + block.group(1) + "]")
    assert all(isinstance(row, list) and len(row) == 5 for row in rows)
    return rows


def _valid_settings() -> dict:
    return {
        "emotionMode": 0,
        "emotionWeight": 0.65,
        "emotionVector": [0.0] * 8,
        "emotionText": "",
        "emotionRandom": False,
        "language": "ZH",
        "duration": 1.0,
        "segmentTokens": 120,
        "advanced": {
            "doSample": True,
            "topP": 0.8,
            "topK": 30,
            "temperature": 0.8,
            "lengthPenalty": 0.0,
            "numBeams": 3,
            "repetitionPenalty": 10.0,
            "maxMelTokens": 1500,
        },
    }


def test_custom_ui_covers_upstream_webui_controls() -> None:
    ui = (ROOT / "studio" / "index.html").read_text(encoding="utf-8")
    i18n = (ROOT / "studio" / "i18n.js").read_text(encoding="utf-8")
    upstream = (ROOT / "webui.py").read_text(encoding="utf-8")
    server = (ROOT / "studio_server.py").read_text(encoding="utf-8")
    inference = (ROOT / "indextts" / "infer_v2_5.py").read_text(encoding="utf-8")
    logo = ROOT / "studio" / "index-voice-logo.svg"

    required_ids = {
        "voice-file",
        "reference-start",
        "record-dialog",
        "preset-confirm-dialog",
        "preset-confirm-title",
        "preset-confirm-description",
        "preset-confirm-name",
        "preset-confirm-warning",
        "preset-confirm-close",
        "preset-confirm-cancel",
        "preset-confirm-submit",
        "audio-input-device",
        "record-device-trigger",
        "record-device-menu",
        "refresh-audio-inputs",
        "record-start",
        "recording-session",
        "recording-device-name",
        "recording-time",
        "ui-locale-trigger",
        "ui-locale-menu",
        "record",
        "text",
        "token-count",
        "token-progress",
        "token-stage",
        "task-status-bar",
        "task-status-progress",
        "task-status-action",
        "result-panel",
        "language",
        "experimental-text",
        "emotion-file",
        "choose-emotion-file",
        "remove-emotion-file",
        "emotion-text",
        "emotion-weight",
        "emotion-random",
        "duration",
        "natural-pacing",
        "sentence-pause",
        "paragraph-pause",
        "do-sample",
        "temperature",
        "top-p",
        "top-k",
        "num-beams",
        "repetition",
        "length-penalty",
        "max-mel",
        "segment-tokens",
        "generate",
        "audio",
        "audio-volume",
        "download",
        "export-format",
        "export-format-trigger",
        "export-format-menu",
        "refresh-presets",
        "refresh-status",
    }
    assert not {item for item in required_ids if f'id="{item}"' not in ui}
    assert all(
        f'data-language="{language}"' in ui
        for language in ("ZH", "EN", "JA", "ES", "AR")
    )
    assert all(f'data-mode="{mode}"' in ui for mode in range(4))
    assert all(f'data-ui-locale="{locale}"' in ui for locale in ("zh", "en", "ja", "es", "ar"))
    assert 'src="/ui/i18n.js"' in ui
    assert ui.index('id="task-status-bar"') < ui.index('id="workspace"')
    assert 'id="generation-chip"' not in ui
    assert 'class="task-status-copy"\n              role="status"' in ui
    assert 'id="task-status-progress"\n              role="progressbar"' in ui
    assert 'stream.setAttribute("aria-valuenow", String(progress))' in ui
    assert '$("elapsed").hidden = false;\n              uiText("elapsed", "历史")' in ui
    assert logo.is_file()
    assert 'rel="icon" href="/ui/index-voice-logo.svg"' in ui
    assert 'class="brand-logo"' in ui
    assert 'class="brand-mark">声' not in ui
    assert "navigator.mediaDevices.enumerateDevices()" in ui
    assert "deviceId: { exact: deviceId }" in ui
    assert 'addEventListener?.("devicechange"' in ui
    assert "record-device-option" in ui
    assert "<select" not in ui
    assert re.search(r'id="emotion-file"[\s\S]{0,160}\bhidden\b', ui)
    assert '.advanced-check input[type="checkbox"]' in ui
    assert 'aria-controls="mode-menu"' in ui
    assert 'modeMenu.addEventListener("keydown"' in ui
    assert "if (!modePicker.contains(event.target)) closeModePicker()" in ui
    assert "new ResizeObserver" in ui
    assert "scrollbar-gutter: stable" in ui
    assert "(max-width:1150px)" in ui
    assert 'document.documentElement.dir = locale === "ar" ? "rtl" : "ltr"' in i18n
    assert 'localStorage.getItem("index-tts-ui-locale")' in i18n
    assert "normalizedSources[normalizeSource(source)]" in i18n
    assert "document.title = t(documentTitleSource)" in i18n
    assert "speechLanguageNames" in ui
    assert "&lt;文字|读音&gt;" in ui
    assert "data-export-format" in ui
    assert 'new EventSource("/api/status-stream")' in ui
    assert "preserveHistoryToken" in ui
    assert "viewingHistory" in ui and "remoteJobChanged" in ui
    assert 'uiText("elapsed", "历史")' in ui
    assert "stream.readyState === EventSource.CLOSED" in ui
    assert "设置项 {key} 必须在 {minimum} 到 {maximum} 之间" in i18n
    assert "serviceAvailable" in ui
    assert "本地服务不可用，请重新运行启动命令。" in i18n
    assert "time.monotonic() - started < 4.0" in server
    assert "timeout_graceful_shutdown=5" in server
    assert ".dropzone > span:last-child" in ui
    assert ".switch input:focus-visible + span" in ui
    assert "segmentRequestId" in ui and "new AbortController()" in ui
    assert "resourceLoadId" in ui
    assert "event?.target?.value ?? $(\"search\").value" in ui
    assert "setActiveNavigation" in ui
    assert all(
        re.search(rf'id="{element_id}"[^>]*dir="ltr"', ui)
        for element_id in (
            "recording-time",
            "source-time",
            "reference-start-value",
            "token-count",
            "audio-time",
        )
    )
    assert re.search(r'id="result-name"[^>]*dir="auto"', ui)
    assert 'c.dir = "ltr"' in ui
    assert "const uiRawText" in ui
    assert all(
        re.search(rf'uiRawText\(\s*"{element_id}"', ui)
        for element_id in (
            "source-label",
            "voice-file-name",
            "voice-name",
            "voice-meta",
            "emotion-file-label",
            "recording-device-name",
            "result-name",
        )
    )
    assert "暂停参考声音" in i18n and "取消静音" in i18n
    assert '$("emotion-mode").value = enabled ? "3" : "0"' in ui
    assert 'if (enabled) $("emotion-text").focus()' in ui
    assert "requestDeletePreset(name)" in ui
    assert "requestOverwritePreset(name)" in ui
    assert 'form.append("overwrite", String(overwrite))' in ui
    assert "error.status === 409" in ui
    assert "preset_exists(clean_name) and not overwrite" in server
    assert 'class="volume-icon-on"' in ui
    assert 'class="volume-icon-muted"' in ui
    assert 'volumeButton.classList.toggle("is-muted", muted)' in ui
    assert '$("audio-volume").textContent' not in ui
    assert "#reset-layout-rail" in ui
    assert "class StudioUvicornServer" in server
    assert "服务正在停止，请稍后重新启动" in i18n
    assert 'TOKENIZERS_PARALLELISM", "false"' in server
    assert "use_qwen_emo=True" in server
    assert "mediaFileSizeError" in ui
    assert 'shutil.which("ffmpeg")' in server
    assert '["ffmpeg"' not in server
    assert studio_server.MAX_AUDIO_UPLOAD_BYTES == 100 * 1024 * 1024
    assert studio_server.MAX_VIDEO_UPLOAD_BYTES == 1024 * 1024 * 1024
    assert studio_server.REFERENCE_WINDOW_SECONDS == 15.0
    assert "_normalize_generated_audio(output_path)" in server
    assert 'stage="normalizing"' in server
    assert '["dragenter", "dragover"]' in ui and '["dragleave", "drop"]' in ui
    assert 'state.generationState !== "complete"' in ui

    assert 'choices=["ZH", "EN", "JA", "AR", "ES"]' in upstream
    assert "EMO_CHOICES_ALL" in upstream
    assert "max_text_tokens_per_segment" in upstream
    assert "duration_factor" in upstream

    infer_mappings = (
        'lang=normalized["language"]',
        'emo_audio_prompt=emotion_path if normalized["emotionMode"] == 1 else None',
        'emo_alpha=normalized["emotionWeight"]',
        'emo_vector=vector',
        'use_emo_text=normalized["emotionMode"] == 3',
        'emo_text=normalized["emotionText"] or None',
        'use_random=normalized["emotionRandom"]',
        'max_text_tokens_per_segment=normalized["segmentTokens"]',
        'duration_factor=normalized["duration"]',
        'do_sample=advanced["doSample"]',
        'top_p=advanced["topP"]',
        'top_k=advanced["topK"] or None',
        'temperature=advanced["temperature"]',
        'length_penalty=advanced["lengthPenalty"]',
        'num_beams=advanced["numBeams"]',
        'repetition_penalty=advanced["repetitionPenalty"]',
        'max_mel_tokens=advanced["maxMelTokens"]',
    )
    assert not {item for item in infer_mappings if item not in server}
    assert '@app.get("/api/export-formats")' in server
    assert '@app.get("/api/export/{filename}")' in server
    assert '@app.get("/api/status-stream")' in server
    assert '"do_sample": do_sample' in inference
    assert "if do_sample:" in inference
    assert "if num_beams > 1:" in inference
    assert "torch.mps.empty_cache()" in inference
    assert inference.count("self._empty_device_cache()") == 2


def test_official_emotion_modes_and_pronunciation_paths() -> None:
    for mode in range(4):
        settings = _valid_settings()
        settings["emotionMode"] = mode
        settings["emotionVector"] = [index / 10 for index in range(8)]
        normalized = studio_server._normalize_settings(settings)
        assert normalized["emotionMode"] == mode
        assert normalized["emotionVector"] == [index / 10 for index in range(8)]

    assert apply_pronunciation_annotations("银<行|HANG2>") == (
        "银<|SPECIAL_TOKEN_2|>HANG2<|SPECIAL_TOKEN_2|>"
    )
    assert apply_pronunciation_annotations("<minute|M IH1 . N AH0 T>") == (
        "<|SPECIAL_TOKEN_1|>M IH1 . N AH0 T<|SPECIAL_TOKEN_1|>"
    )
    assert apply_pronunciation_annotations("<上手|じょうず>").strip() == "じょうず"


def test_settings_validation_matches_ui_ranges() -> None:
    normalized = studio_server._normalize_settings(_valid_settings())
    assert normalized["language"] == "ZH"
    assert normalized["advanced"]["topP"] == 0.8
    assert normalized["referenceStart"] == 0.0
    assert normalized["naturalPacing"] is False
    assert normalized["sentencePauseMs"] == 320
    assert normalized["paragraphPauseMs"] == 650

    invalid = deepcopy(_valid_settings())
    invalid["advanced"]["topP"] = 8
    try:
        studio_server._normalize_settings(invalid)
    except HTTPException as error:
        assert error.status_code == 400
    else:
        raise AssertionError("out-of-range topP must be rejected")


def test_reference_window_start_is_passed_to_ffmpeg() -> None:
    upload = UploadFile(
        file=BytesIO(b"test-media"),
        filename="sample.mov",
        headers=Headers({"content-type": "video/quicktime"}),
    )
    with patch("studio_server.subprocess.run") as run:
        path = studio_server._save_upload(
            upload,
            "window-test",
            start_seconds=12.5,
        )
    arguments = run.call_args.args[0]
    assert arguments[arguments.index("-ss") + 1] == "12.500"
    assert arguments[arguments.index("-t") + 1] == "15"
    assert path is not None
    Path(path).unlink(missing_ok=True)


def test_natural_pacing_splits_and_concatenates() -> None:
    assert studio_server._split_narration_units(
        "第一句。第二句？\n\n第三段！", 320, 650
    ) == [
        ("第一句。", 320),
        ("第二句？", 650),
        ("第三段！", 0),
    ]
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        first = directory / "first.wav"
        second = directory / "second.wav"
        output = directory / "output.wav"
        for path in (first, second):
            with wave.open(str(path), "wb") as destination:
                destination.setnchannels(1)
                destination.setsampwidth(2)
                destination.setframerate(1000)
                destination.writeframes(array("h", [100] * 100).tobytes())
        studio_server._concatenate_wavs_with_pauses(
            [(first, 250), (second, 0)], output
        )
        with wave.open(str(output), "rb") as source:
            assert source.getnframes() == 450


def test_status_and_file_urls_are_scoped() -> None:
    payload = studio_server.status()
    assert {"model", "modelReady", "modelAvailable", "modelState", "generation"} <= payload.keys()
    assert {"stage", "progress", "tokenProgress"} <= payload["generation"].keys()
    assert studio_server._url_for(studio_server.GENERATIONS / "sample.wav") == "/files/generations/sample.wav"
    assert studio_server._url_for(studio_server.PRESETS / "voice" / "prompt.wav") == "/files/presets/voice/prompt.wav"
    assert studio_server._url_for(ROOT / "README.md") is None
    formats = studio_server._available_export_formats()
    assert formats and formats[0]["id"] == "wav"


def test_mps_cache_cleanup_uses_the_active_device() -> None:
    model = IndexTTS2.__new__(IndexTTS2)
    model.device = "mps"
    with patch("indextts.infer_v2_5.torch.mps.empty_cache") as empty_cache:
        model._empty_device_cache()
    empty_cache.assert_called_once_with()


def test_interface_translation_catalog_is_complete() -> None:
    source = (ROOT / "studio" / "i18n.js").read_text(encoding="utf-8")
    rows = _translation_rows(source)
    assert len(rows) >= 280
    keys = [row[0] for row in rows]
    assert len(keys) == len(set(keys))
    assert all(all(value for value in row) for row in rows)


def test_static_interface_text_has_translation_coverage() -> None:
    ui = (ROOT / "studio" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "studio" / "i18n.js").read_text(encoding="utf-8")
    rows = _translation_rows(source)
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    keys = {normalize(row[0]) for row in rows}

    class InterfaceTextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.text: list[str] = []
            self.attributes: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.stack.append(tag)
            for name, value in attrs:
                if (
                    name in {"placeholder", "aria-label", "title"}
                    and value
                    and re.search(r"[\u3400-\u9fff]", value)
                ):
                    self.attributes.append(normalize(value))

        def handle_startendtag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            self.handle_starttag(tag, attrs)
            self.stack.pop()

        def handle_endtag(self, tag: str) -> None:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index] == tag:
                    self.stack = self.stack[:index]
                    return

        def handle_data(self, data: str) -> None:
            if any(tag in {"script", "style", "code"} for tag in self.stack):
                return
            value = normalize(data)
            if value and re.search(r"[\u3400-\u9fff]", value):
                self.text.append(value)

    parser = InterfaceTextParser()
    parser.feed(ui)
    runtime_text = {
        "0 字",
        "IndexTTS-2.5 · 检查中",
        "中文",
        "日本語",
        "导出 WAV",
    }
    missing_text = set(parser.text) - keys - runtime_text
    missing_attributes = set(parser.attributes) - keys
    assert not missing_text, missing_text
    assert not missing_attributes, missing_attributes


def test_preset_names_are_bounded_and_path_safe() -> None:
    ui = (ROOT / "studio" / "index.html").read_text(encoding="utf-8")
    assert len(safe_preset_name("x" * 200)) == 60
    assert safe_preset_name("../voice") == "voice"
    assert 'id="preset-name"' in ui and 'maxlength="60"' in ui
    assert studio_server.MAX_PRESET_NAME_CHARS == 60


def test_preset_overwrite_requires_explicit_confirmation() -> None:
    with patch("studio_server.preset_exists", return_value=True):
        try:
            asyncio.run(
                studio_server.create_preset(
                    name="existing-voice",
                    settings="{}",
                    overwrite=False,
                    prompt_audio=None,
                    emotion_audio=None,
                )
            )
        except HTTPException as error:
            assert error.status_code == 409
            assert error.detail == "已存在同名声音，请确认是否覆盖"
        else:
            raise AssertionError("existing presets must require overwrite confirmation")


def test_feature_inventory_counts_and_routes() -> None:
    ui = (ROOT / "studio" / "index.html").read_text(encoding="utf-8")
    assert len(re.findall(r'data-ui-locale="(?:zh|en|ja|es|ar)"', ui)) == 5
    assert len(re.findall(r'data-language="(?:ZH|EN|JA|ES|AR)"', ui)) == 5
    assert len(re.findall(r'<button\b[^>]*data-mode="[0-3]"', ui, re.DOTALL)) == 4
    vector_block = re.search(r"const vectorNames = \[(.*?)\];", ui, re.DOTALL)
    assert vector_block and len(re.findall(r'"[^"]+"', vector_block.group(1))) == 8
    assert {
        "do-sample",
        "temperature",
        "top-p",
        "top-k",
        "num-beams",
        "repetition",
        "length-penalty",
        "max-mel",
        "segment-tokens",
    } <= set(re.findall(r'id="([^"]+)"', ui))
    assert set(studio_server.EXPORT_FORMATS) == {"wav", "mp3", "m4a", "flac", "ogg"}
    assert studio_server.SUPPORTED_MEDIA_EXTENSIONS == {
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
    cases = [
        line
        for line in (ROOT / "examples" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(cases) == 14
    api_paths = {
        route.path
        for route in studio_server.app.routes
        if isinstance(route, APIRoute)
    }
    assert {
        "/",
        "/api/health",
        "/api/status",
        "/api/status-stream",
        "/api/export-formats",
        "/api/export/{filename}",
        "/api/presets",
        "/api/presets/{name}",
        "/api/segments",
        "/api/generate",
        "/api/examples",
        "/api/history",
    } <= api_paths


def test_model_work_uses_daemon_threads() -> None:
    daemon = asyncio.run(
        studio_server._run_in_daemon_thread(
            lambda: threading.current_thread().daemon
        )
    )
    assert daemon is True
    assert "asyncio.to_thread" not in (ROOT / "studio_server.py").read_text(
        encoding="utf-8"
    )


def test_generated_audio_is_normalized_for_playback() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        path = directory / "quiet.wav"
        sample_rate = 22050
        quiet = array(
            "h",
            (
                round(160 * math.sin(2 * math.pi * 440 * index / sample_rate))
                for index in range(sample_rate)
            ),
        )
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(quiet.tobytes())

        studio_server._normalize_generated_audio(path)

        with wave.open(str(path), "rb") as source:
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            assert source.getframerate() == sample_rate
            normalized = array("h")
            normalized.frombytes(source.readframes(source.getnframes()))
        assert max(map(abs, normalized)) > max(map(abs, quiet)) * 20
        assert max(map(abs, normalized)) < 32767
        assert not list(directory.glob(".*.normalized.wav"))


class StudioContractTests(unittest.TestCase):
    test_custom_ui_covers_upstream_webui_controls = staticmethod(
        test_custom_ui_covers_upstream_webui_controls
    )
    test_official_emotion_modes_and_pronunciation_paths = staticmethod(
        test_official_emotion_modes_and_pronunciation_paths
    )
    test_settings_validation_matches_ui_ranges = staticmethod(
        test_settings_validation_matches_ui_ranges
    )
    test_reference_window_start_is_passed_to_ffmpeg = staticmethod(
        test_reference_window_start_is_passed_to_ffmpeg
    )
    test_natural_pacing_splits_and_concatenates = staticmethod(
        test_natural_pacing_splits_and_concatenates
    )
    test_status_and_file_urls_are_scoped = staticmethod(
        test_status_and_file_urls_are_scoped
    )
    test_mps_cache_cleanup_uses_the_active_device = staticmethod(
        test_mps_cache_cleanup_uses_the_active_device
    )
    test_interface_translation_catalog_is_complete = staticmethod(
        test_interface_translation_catalog_is_complete
    )
    test_static_interface_text_has_translation_coverage = staticmethod(
        test_static_interface_text_has_translation_coverage
    )
    test_preset_names_are_bounded_and_path_safe = staticmethod(
        test_preset_names_are_bounded_and_path_safe
    )
    test_feature_inventory_counts_and_routes = staticmethod(
        test_feature_inventory_counts_and_routes
    )
    test_model_work_uses_daemon_threads = staticmethod(
        test_model_work_uses_daemon_threads
    )
    test_generated_audio_is_normalized_for_playback = staticmethod(
        test_generated_audio_is_normalized_for_playback
    )


if __name__ == "__main__":
    unittest.main()
