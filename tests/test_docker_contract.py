from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from docker.prepare_model import prepare_model


ROOT = Path(__file__).resolve().parents[1]


def test_compose_is_local_by_default_and_persists_user_data() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "${INDEXTTS_BIND_ADDRESS:-127.0.0.1}" in compose
    assert "checkpoints:/app/checkpoints" in compose
    assert "outputs:/app/outputs" in compose
    assert "INDEXTTS_AUTO_DOWNLOAD" in compose
    assert "/api/status" in compose


def test_container_uses_locked_dependencies_and_non_root_user() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen --no-dev --extra studio" in dockerfile
    assert "USER indextts" in dockerfile
    assert 'ENTRYPOINT ["./docker/entrypoint.sh"]' in dockerfile
    assert 'CMD ["python", "-m", "docker.run_studio"]' in dockerfile


def test_container_binds_service_to_its_network_interface() -> None:
    runner = (ROOT / "docker" / "run_studio.py").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'os.environ.get("INDEXTTS_STUDIO_HOST", "0.0.0.0")' in runner
    assert "StudioUvicornServer(config).run()" in runner
    assert "INDEXTTS_STUDIO_HOST=0.0.0.0" in dockerfile


def test_linux_arm_uses_cpu_compatible_pytorch_source() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    cuda_marker = "sys_platform == 'linux' and platform_machine == 'x86_64'"
    assert project.count(cuda_marker) == 3


def test_linux_arm_uses_wetext_without_building_pynini() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    front = (ROOT / "indextts" / "utils" / "front.py").read_text(encoding="utf-8")
    assert "sys_platform != 'linux' or platform_machine == 'aarch64'" in project
    assert 'platform.machine().lower() in {' in front
    assert '"aarch64"' in front


def test_model_preparation_reuses_complete_primary_weights(tmp_path: Path) -> None:
    primary = {"ok": True, "missing": [], "invalid": []}
    complete = {
        "ok": True,
        "missing": [],
        "invalid": [],
        "requiredFiles": 13,
        "checkedHashes": 0,
        "hashVerification": False,
    }
    with (
        patch("docker.prepare_model.inspect_model_directory", side_effect=[primary, complete]),
        patch("docker.prepare_model.snapshot_download") as download,
        patch("docker.prepare_model.ensure_models_available") as auxiliary,
    ):
        result = prepare_model(tmp_path)
    download.assert_not_called()
    auxiliary.assert_called_once_with(str(tmp_path.resolve()))
    assert result == complete
