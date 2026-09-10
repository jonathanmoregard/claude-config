#!/usr/bin/env python3
"""Focused behavior tests for hooks/prompt-log.py."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HOOK_PATH = Path(__file__).parents[1] / "hooks" / "prompt-log.py"
SPEC = importlib.util.spec_from_file_location("prompt_log", HOOK_PATH)
assert SPEC is not None and SPEC.loader is not None
prompt_log = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prompt_log)


class PromptLogSourceTests(unittest.TestCase):
    def run_hook(self, payload: dict[str, object], output_dir: Path) -> None:
        prompt_log.OUT = output_dir / "prompts.jsonl"
        prompt_log.LOCK = output_dir / ".rotate.lock"
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            with self.assertRaisesRegex(SystemExit, "0"):
                prompt_log.main()

    def test_machine_generated_sources_are_not_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            for source in (
                "sdk",
                "system",
                "loop_wakeup",
                "schedule_wakeup",
                "poll_event",
            ):
                with self.subTest(source=source):
                    self.run_hook(
                        {
                            "session_id": "session-1",
                            "cwd": "/tmp/project",
                            "hook_event_name": "UserPromptSubmit",
                            "prompt": "synthetic turn",
                            "source": source,
                        },
                        output_dir,
                    )

            self.assertFalse(prompt_log.OUT.exists())

    def test_interactive_user_source_is_logged_without_text_heuristics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            self.run_hook(
                {
                    "session_id": "session-1",
                    "cwd": "/tmp/project",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "<task-notification>quoted by user</task-notification>",
                    "source": "user",
                },
                output_dir,
            )

            entry = json.loads(prompt_log.OUT.read_text(encoding="utf-8"))
            self.assertEqual(
                entry["prompt_head"],
                "<task-notification>quoted by user</task-notification>",
            )

    def test_source_less_legacy_payload_is_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            self.run_hook(
                {
                    "session_id": "session-1",
                    "cwd": "/tmp/project",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "legacy interactive prompt",
                },
                output_dir,
            )

            entry = json.loads(prompt_log.OUT.read_text(encoding="utf-8"))
            self.assertEqual(entry["prompt_head"], "legacy interactive prompt")


if __name__ == "__main__":
    unittest.main()
