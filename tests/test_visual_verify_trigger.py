"""Contract for hooks/visual-verify-trigger.sh.

The hook nudges the agent to run visual-verify after a Write/Edit lands on a
rendered-view file. Its two failure modes pull in opposite directions:

  * silence on a real UI change -- the defect class the skill exists for goes
    unreviewed. This is the expensive one, so every doubt resolves to firing.
  * firing on an edit that cannot change what renders (comment, import or
    formatting churn in an HTML-returning handler) -- a full four-viewport
    capture run that, by construction, can find nothing.

The invariant under test: the hook is silent ONLY when the edit's old and new
text are equal after removing comments, imports and whitespace layout. Anything
else on a rendered-view file fires. Every case runs the real hook over its real
stdin contract.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_ROOT, "hooks", "visual-verify-trigger.sh")

HANDLER = """use axum::response::Html;

/// Renders the poll page.
pub async fn view(q: Query) -> Html<String> {
    let title = q.title.trim();
    Html(format!("<h1 class=\\"poll\\">{title}</h1>"))
}
"""


@pytest.fixture
def handler(tmp_path):
    p = tmp_path / "src" / "handlers" / "view.rs"
    p.parent.mkdir(parents=True)
    p.write_text(HANDLER)
    return str(p)


def run(tool_name, tool_input):
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool_name,
               "tool_input": tool_input}
    r = subprocess.run(["bash", HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def fires(tool_name, tool_input):
    out = run(tool_name, tool_input)
    if not out:
        return False
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "visual-verify" in ctx
    return True


def edit(path, old, new):
    return {"file_path": path, "old_string": old, "new_string": new}


# --- render-neutral edits stay silent --------------------------------------

def test_doc_comment_only_edit_is_silent(handler):
    assert not fires("Edit", edit(handler, "/// Renders the poll page.\n",
                                  "/// Renders the poll page (spec 5.3).\n// see #12\n"))


def test_import_only_edit_is_silent(handler):
    assert not fires("Edit", edit(handler, "use axum::response::Html;",
                                  "use axum::response::Html;\nuse chrono::{\n    DateTime, Utc,\n};"))


def test_reformat_only_edit_is_silent(handler):
    assert not fires("Edit", edit(
        handler,
        "    let title = q.title.trim();",
        "    let title =\n        q.title\n            .trim();"))


def test_template_comment_only_edit_is_silent(tmp_path):
    f = str(tmp_path / "templates" / "poll.html")
    assert not fires("Edit", edit(f, '{# old note #}\n<h1 class="poll">', '{# new note #}\n<h1 class="poll">'))


def test_ts_named_import_edit_is_silent(tmp_path):
    f = str(tmp_path / "client" / "src" / "poll.tsx")
    assert not fires("Edit", edit(f, "import { a } from './a';", "import { a, b } from './a';"))


# --- anything that can change output fires ---------------------------------

def test_markup_change_fires(handler):
    assert fires("Edit", edit(handler, 'class=\\"poll\\"', 'class=\\"poll big\\"'))


def test_logic_change_in_html_handler_fires(handler):
    # Not provably render-neutral: logic feeds the rendered value.
    assert fires("Edit", edit(handler, "q.title.trim()", "q.title.trim().to_uppercase()"))


def test_string_with_slashes_is_not_treated_as_comment(handler):
    assert fires("Edit", edit(handler, '    let u = "a";', '    let u = "// shown to users";'))


def test_doc_comment_with_quotes_is_silent(handler):
    assert not fires("Edit", edit(handler, "/// Renders the poll page.",
                                  "/// Renders the poll's \"title\" page."))


# Whitespace between inline elements renders as a visible space, so its
# presence or absence is never layout noise. (Found replaying a real edit:
# `</span><span>` -> `</span> <span>`.)
def test_space_between_inline_tags_in_template_fires(tmp_path):
    f = str(tmp_path / "templates" / "poll.html")
    assert fires("Edit", edit(f, "<span>a</span><span>b</span>", "<span>a</span> <span>b</span>"))


def test_space_inside_rust_string_fires(handler):
    assert fires("Edit", edit(handler, '"<b>{title}</b><i>x</i>"', '"<b>{title}</b> <i>x</i>"'))


def test_space_in_jsx_text_fires(tmp_path):
    f = str(tmp_path / "client" / "src" / "poll.tsx")
    assert fires("Edit", edit(f, "<b>{n}</b>votes", "<b>{n}</b> votes"))


def test_side_effect_css_import_fires(tmp_path):
    f = str(tmp_path / "client" / "src" / "poll.tsx")
    assert fires("Edit", edit(f, "import './a.css';", "import './b.css';"))


def test_template_markup_change_fires(tmp_path):
    f = str(tmp_path / "templates" / "poll.html")
    assert fires("Edit", edit(f, '<h1 class="poll">', '<h1 class="poll lead">'))


def test_edit_starting_inside_block_comment_fires(handler):
    # The fragment cannot be proven to be comment-only; err toward firing.
    assert fires("Edit", edit(handler, " * old wording\n */", " * new wording\n */\nfn x() {}"))


def test_multiedit_with_one_real_change_fires(handler):
    assert fires("MultiEdit", {"file_path": handler, "edits": [
        {"old_string": "/// Renders", "new_string": "/// Draws"},
        {"old_string": "trim()", "new_string": "trim_start()"},
    ]})


def test_multiedit_all_neutral_is_silent(handler):
    assert not fires("MultiEdit", {"file_path": handler, "edits": [
        {"old_string": "/// Renders", "new_string": "/// Draws"},
        {"old_string": "use axum::response::Html;", "new_string": "use axum::response::{Html, IntoResponse};"},
    ]})


def test_write_always_fires(handler):
    # No prior content to compare against.
    assert fires("Write", {"file_path": handler, "content": HANDLER})


# --- unchanged path classification -----------------------------------------

def test_non_rendering_rust_file_is_silent(tmp_path):
    p = tmp_path / "lib.rs"
    p.write_text("pub fn add(a: u8, b: u8) -> u8 { a + b }\n")
    assert not fires("Edit", edit(str(p), "a + b", "a.wrapping_add(b)"))


def test_docs_html_is_silent(tmp_path):
    assert not fires("Edit", edit(str(tmp_path / "docs" / "x.html"), "<p>a</p>", "<p>b</p>"))
