#!/usr/bin/env bash
# A Python comment-moderation service: a panel of three small models (one
# Anthropic, two OpenAI) is told to answer with exactly one word, keep or
# remove, and _parse_vote() maps the stripped, lowercased reply to an enum.
# Anything else is "unavailable", which fails closed and holds the comment.
# One OpenAI judge has started answering "Verdict: keep" and "**keep**", so
# clean comments pile up in the hold queue. The whole service is the repo's own.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/moderation" "$WORK/tests" "$WORK/logs"

cat > "$WORK/moderation/panel.py" <<'PY'
"""Second-opinion panel for comments the keyword filter flagged.

Three small models from two vendors each vote keep or remove. The comment is
published only when every judge votes keep; any remove, error or unreadable
answer holds it for a human (fail closed).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import anthropic
import openai

Vote = Literal["keep", "remove", "unavailable"]

JUDGES = (
    {"id": "haiku", "provider": "anthropic", "model": "claude-haiku-4-5"},
    {"id": "4o-mini", "provider": "openai", "model": "gpt-4o-mini"},
    {"id": "41-mini", "provider": "openai", "model": "gpt-4.1-mini"},
)

SYSTEM = (
    "You moderate comments on a cooking forum. The comment was flagged by a "
    "keyword filter. Decide whether it breaks the rules (spam, harassment, "
    "off-platform selling) or is fine to publish.\n\n"
    "Respond with exactly one word: keep or remove."
)


@dataclass
class PanelResult:
    publish: bool
    votes: dict[str, Vote]


def _parse_vote(text: str) -> Vote:
    cleaned = (text or "").strip().strip(".!\"'`").lower()
    if cleaned == "keep":
        return "keep"
    if cleaned == "remove":
        return "remove"
    return "unavailable"


async def _ask_anthropic(client: anthropic.AsyncAnthropic, model: str, comment: str) -> Vote:
    msg = await client.messages.create(
        model=model,
        max_tokens=8,
        system=SYSTEM,
        messages=[{"role": "user", "content": comment}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _parse_vote(text)


async def _ask_openai(client: openai.AsyncOpenAI, model: str, comment: str) -> Vote:
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=8,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": comment}],
    )
    return _parse_vote(resp.choices[0].message.content or "")


async def review(comment: str) -> PanelResult:
    a, o = anthropic.AsyncAnthropic(), openai.AsyncOpenAI()

    async def one(j: dict) -> Vote:
        try:
            if j["provider"] == "anthropic":
                return await _ask_anthropic(a, j["model"], comment)
            return await _ask_openai(o, j["model"], comment)
        except Exception:
            return "unavailable"

    votes = dict(zip((j["id"] for j in JUDGES), await asyncio.gather(*(one(j) for j in JUDGES))))
    return PanelResult(publish=all(v == "keep" for v in votes.values()), votes=votes)
PY

cat > "$WORK/tests/test_parse_vote.py" <<'PY'
from moderation.panel import _parse_vote


def test_plain_words():
    assert _parse_vote("keep") == "keep"
    assert _parse_vote("Remove.") == "remove"


def test_anything_else_is_unavailable():
    assert _parse_vote("keep or remove") == "unavailable"
    assert _parse_vote("") == "unavailable"
PY
touch "$WORK/moderation/__init__.py"

cat > "$WORK/logs/panel-debug-2026-11-25.log" <<'LOG'
2026-11-25T09:14:02 comment=88213 haiku raw='keep' vote=keep
2026-11-25T09:14:02 comment=88213 4o-mini raw='keep' vote=keep
2026-11-25T09:14:02 comment=88213 41-mini raw='Verdict: keep' vote=unavailable
2026-11-25T09:14:02 comment=88213 publish=False (held)
2026-11-25T09:31:47 comment=88240 haiku raw='keep' vote=keep
2026-11-25T09:31:47 comment=88240 4o-mini raw='Keep.' vote=keep
2026-11-25T09:31:47 comment=88240 41-mini raw='**keep**' vote=unavailable
2026-11-25T09:31:47 comment=88240 publish=False (held)
2026-11-25T10:02:11 comment=88251 haiku raw='remove' vote=remove
2026-11-25T10:02:11 comment=88251 4o-mini raw='remove' vote=remove
2026-11-25T10:02:11 comment=88251 41-mini raw='Remove — this is' vote=unavailable
2026-11-25T10:02:11 comment=88251 publish=False (held)
2026-11-25T10:40:55 comment=88266 haiku raw='keep' vote=keep
2026-11-25T10:40:55 comment=88266 4o-mini raw='keep' vote=keep
2026-11-25T10:40:55 comment=88266 41-mini raw='Keep\n\nThe comment' vote=unavailable
2026-11-25T10:40:55 comment=88266 publish=False (held)
LOG

git -C "$WORK" add -A
git -C "$WORK" commit -qm "moderation panel"
