"""Tests for acpc.runner module."""

import os
import sys
from unittest.mock import patch

from acpc.runner import (
    EXIT_AGENT_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_SIGINT,
    EXIT_SIGPIPE,
    EXIT_SIGTERM,
    EXIT_TIMEOUT,
    EXIT_USAGE_ERROR,
    RunConfig,
    _get_preexec_fn,
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


class TestGetPreexecFn:
    def test_returns_setpgrp_on_unix(self) -> None:
        with patch.object(sys, "platform", "linux"):
            fn = _get_preexec_fn()
            assert fn is os.setpgrp

    def test_returns_none_on_windows(self) -> None:
        with patch.object(sys, "platform", "win32"):
            fn = _get_preexec_fn()
            assert fn is None

    def test_returns_setpgrp_on_darwin(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            fn = _get_preexec_fn()
            assert fn is os.setpgrp
