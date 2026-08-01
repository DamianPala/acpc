"""Tests for state-dir routing in the model cache."""

from pathlib import Path

from acpc.models_cache import load_cached_models, save_models


def test_model_cache_uses_acpc_state_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACPC_STATE_DIR", str(tmp_path))

    save_models("codex", [{"model_id": "gpt-test"}])
    cached = load_cached_models("codex")

    assert cached is not None
    assert cached["agent"] == "codex"
    assert cached["available_models"] == [{"model_id": "gpt-test"}]
    assert (tmp_path / "models" / "codex.json").exists()
