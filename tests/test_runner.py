"""Tests for acpc.runner module."""

import asyncio
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call, patch

from acpc.runner import (
    EXIT_AGENT_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_SIGINT,
    EXIT_SIGPIPE,
    EXIT_SIGTERM,
    EXIT_TIMEOUT,
    EXIT_USAGE_ERROR,
    RunConfig,
    _cache_available_models,
    _process_group_kwargs,
    _try_set_model,
)


class TestRunConfig:
    def test_defaults(self) -> None:
        config = RunConfig(
            agent_identity="codex",
            prompt_text="hello",
        )
        assert config.agent_identity == "codex"
        assert config.prompt_text == "hello"
        assert config.model is None
        assert config.mode is None
        assert config.permission_level == "prompt"
        assert config.cwd is None
        assert config.session_id is None
        assert config.use_last is False
        assert config.output_mode == "text"
        assert config.output_file is None
        assert config.timeout is None
        assert config.is_tty is True
        assert config.env == {}

    def test_custom_values(self) -> None:
        config = RunConfig(
            agent_identity="claude",
            prompt_text="analyze",
            model="sonnet",
            mode="plan",
            permission_level="all",
            cwd="/tmp",
            session_id="sess-1",
            use_last=True,
            output_mode="json",
            output_file="out.md",
            timeout=60,
            is_tty=False,
            env={"KEY": "val"},
        )
        assert config.model == "sonnet"
        assert config.mode == "plan"
        assert config.permission_level == "all"
        assert config.cwd == "/tmp"
        assert config.session_id == "sess-1"
        assert config.use_last is True
        assert config.output_mode == "json"
        assert config.output_file == "out.md"
        assert config.timeout == 60
        assert config.is_tty is False
        assert config.env == {"KEY": "val"}


class TestExitCodes:
    def test_values(self) -> None:
        assert EXIT_AGENT_ERROR == 1
        assert EXIT_USAGE_ERROR == 2
        assert EXIT_PERMISSION_DENIED == 3
        assert EXIT_TIMEOUT == 124
        assert EXIT_SIGINT == 130
        assert EXIT_SIGPIPE == 141
        assert EXIT_SIGTERM == 143


class TestProcessGroupKwargs:
    def test_unix_uses_start_new_session(self) -> None:
        with patch.object(sys, "platform", "linux"):
            kwargs = _process_group_kwargs()
            assert kwargs == {"start_new_session": True}

    def test_windows_uses_create_new_process_group(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True),
        ):
            kwargs = _process_group_kwargs()
            assert kwargs == {"creationflags": 0x00000200}

    def test_darwin_uses_start_new_session(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            kwargs = _process_group_kwargs()
            assert kwargs == {"start_new_session": True}


class TestCacheAvailableModels:
    @patch("acpc.models_cache.save_models")
    def test_caches_models_from_legacy_models_state(self, save_models) -> None:
        response = SimpleNamespace(
            models=SimpleNamespace(
                available_models=[
                    SimpleNamespace(
                        model_id="gpt-5.5/high",
                        display_name="GPT-5.5 High",
                    )
                ]
            ),
            config_options=None,
        )

        cached = _cache_available_models("codex", response)

        assert cached is True
        save_models.assert_called_once_with(
            "codex",
            [{"model_id": "gpt-5.5/high", "display_name": "GPT-5.5 High"}],
        )

    @patch("acpc.models_cache.save_models")
    def test_caches_models_from_config_options(self, save_models) -> None:
        response = SimpleNamespace(
            models=None,
            config_options=[
                SimpleNamespace(
                    root=SimpleNamespace(
                        id="model",
                        category="model",
                        options=[
                            SimpleNamespace(
                                value="gpt-5.6-sol",
                                name="GPT-5.6-Sol",
                            ),
                            SimpleNamespace(
                                value="gpt-5.6-terra",
                                name="GPT-5.6-Terra",
                            ),
                        ],
                    )
                )
            ],
        )

        cached = _cache_available_models("codex", response)

        assert cached is True
        save_models.assert_called_once_with(
            "codex",
            [
                {"model_id": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
                {"model_id": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra"},
            ],
        )

    @patch("acpc.models_cache.save_models")
    def test_reports_when_response_has_no_models(self, save_models) -> None:
        response = SimpleNamespace(models=None, config_options=[])

        cached = _cache_available_models("codex", response)

        assert cached is False
        save_models.assert_not_called()


class TestTrySetModel:
    def test_uses_current_config_option_api_when_advertised(self) -> None:
        connection = SimpleNamespace(
            set_config_option=AsyncMock(),
            set_session_model=AsyncMock(),
        )
        response = SimpleNamespace(
            models=None,
            config_options=[
                SimpleNamespace(
                    root=SimpleNamespace(
                        id="model",
                        category="model",
                        options=[SimpleNamespace(value="gpt-5.6-sol")],
                    )
                )
            ],
        )

        was_set = asyncio.run(
            _try_set_model(
                cast(Any, connection),
                "session-1",
                "gpt-5.6-luna/medium",
                response,
                lambda message: None,
            )
        )

        assert was_set is True
        assert connection.set_config_option.await_args_list == [
            call(
                config_id="model",
                session_id="session-1",
                value="gpt-5.6-luna",
            ),
            call(
                config_id="reasoning_effort",
                session_id="session-1",
                value="medium",
            ),
        ]
        connection.set_session_model.assert_not_awaited()

