"""Contract for hooks/visual-verify-trigger.sh.

The hook nudges the agent to run visual-verify after a Write/Edit lands on a
rendered-view file. Its two failure modes pull in opposite directions:

  * silence on a real UI change -- the defect class the skill exists for goes
    unreviewed. This is the expensive one, so every doubt resolves to firing.
  * firing on an edit that cannot change what renders (comment, import or
    formatting churn in an HTML-returning handler) -- a full four-viewport
    capture run that, by construction, can find nothing.

The invariant under test: the hook is silent ONLY when the file before and
after the edit is equal once comments, unchanged import bindings and
whitespace layout are removed. Anything else on a rendered-view file fires.
Every case runs the real hook over its real stdin contract, with the file on
disk already holding the edited text, as it does at PostToolUse.
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

TEMPLATE = """{# old note #}
<h1 class="poll">{{ title }}</h1>
<p><span>a</span><span>b</span></p>
"""

TSX = """import { a } from './a';
import styles from './card.module.css';
import Button from './Button';

export function Card({ n }: { n: number }) {
  return <div className={styles.card}><b>{n}</b>votes<Button /></div>;
}
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def applied(path, before, old, new):
    """PostToolUse: the file already holds the edited text."""
    assert before.count(old) == 1
    write(path, before.replace(old, new, 1))
    return {"file_path": path, "old_string": old, "new_string": new}


@pytest.fixture
def handler(tmp_path):
    return str(tmp_path / "src" / "handlers" / "view.rs")


def h_edit(path, old, new, before=HANDLER):
    return applied(path, before, old, new)


def t_edit(tmp_path, old, new, before=TEMPLATE):
    return applied(str(tmp_path / "templates" / "poll.html"), before, old, new)


def x_edit(tmp_path, old, new, before=TSX):
    return applied(str(tmp_path / "client" / "src" / "poll.tsx"), before, old, new)


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


# --- render-neutral edits stay silent --------------------------------------

def test_doc_comment_only_edit_is_silent(handler):
    assert not fires("Edit", h_edit(handler, "/// Renders the poll page.\n",
                                    "/// Renders the poll page (spec 5.3).\n// see #12\n"))


def test_import_only_edit_is_silent(handler):
    assert not fires("Edit", h_edit(handler, "use axum::response::Html;",
                                    "use axum::response::Html;\nuse chrono::{\n    DateTime, Utc,\n};"))


def test_reformat_only_edit_is_silent(handler):
    assert not fires("Edit", h_edit(
        handler,
        "    let title = q.title.trim();",
        "    let title =\n        q.title\n            .trim();"))


def test_template_comment_only_edit_is_silent(tmp_path):
    assert not fires("Edit", t_edit(tmp_path, '{# old note #}\n<h1 class="poll">',
                                    '{# new note #}\n<h1 class="poll">'))


def test_ts_named_import_edit_is_silent(tmp_path):
    assert not fires("Edit", x_edit(tmp_path, "import { a } from './a';",
                                    "import { a, b } from './a';"))


def test_removing_an_unused_import_is_silent(handler):
    before = HANDLER.replace("use axum::response::Html;\n",
                             "use axum::response::Html;\nuse std::fmt;\n")
    assert not fires("Edit", h_edit(handler, "use axum::response::Html;\nuse std::fmt;\n",
                                    "use axum::response::Html;\n", before=before))


def test_doc_comment_with_quotes_is_silent(handler):
    assert not fires("Edit", h_edit(handler, "/// Renders the poll page.",
                                    "/// Renders the poll's \"title\" page."))


def test_multiedit_all_neutral_is_silent(handler):
    write(handler, HANDLER.replace("/// Renders", "/// Draws").replace(
        "use axum::response::Html;", "use axum::response::{Html, IntoResponse};"))
    assert not fires("MultiEdit", {"file_path": handler, "edits": [
        {"old_string": "/// Renders", "new_string": "/// Draws"},
        {"old_string": "use axum::response::Html;", "new_string": "use axum::response::{Html, IntoResponse};"},
    ]})


# --- anything that can change output fires ---------------------------------

def test_markup_change_fires(handler):
    assert fires("Edit", h_edit(handler, 'class=\\"poll\\"', 'class=\\"poll big\\"'))


def test_logic_change_in_html_handler_fires(handler):
    # Not provably render-neutral: logic feeds the rendered value.
    assert fires("Edit", h_edit(handler, "q.title.trim()", "q.title.trim().to_uppercase()"))


def test_string_with_slashes_is_not_treated_as_comment(handler):
    assert fires("Edit", h_edit(handler, "{title}</h1>", "{title}</h1>// shown to users"))


# Whitespace between inline elements renders as a visible space, so its
# presence or absence is never layout noise. (Found replaying a real edit:
# `</span><span>` -> `</span> <span>`.)
def test_space_between_inline_tags_in_template_fires(tmp_path):
    assert fires("Edit", t_edit(tmp_path, "<span>a</span><span>b</span>",
                                "<span>a</span> <span>b</span>"))


def test_space_inside_rust_string_fires(handler):
    assert fires("Edit", h_edit(handler, "{title}</h1>", " {title}</h1>"))


def test_space_in_jsx_text_fires(tmp_path):
    assert fires("Edit", x_edit(tmp_path, "<b>{n}</b>votes", "<b>{n}</b> votes"))


def test_side_effect_css_import_fires(tmp_path):
    assert fires("Edit", x_edit(tmp_path, "import { a } from './a';",
                                "import { a } from './a';\nimport './b.css';"))


def test_template_markup_change_fires(tmp_path):
    assert fires("Edit", t_edit(tmp_path, '<h1 class="poll">', '<h1 class="poll lead">'))


def test_multiedit_with_one_real_change_fires(handler):
    write(handler, HANDLER.replace("/// Renders", "/// Draws").replace("trim()", "trim_start()"))
    assert fires("MultiEdit", {"file_path": handler, "edits": [
        {"old_string": "/// Renders", "new_string": "/// Draws"},
        {"old_string": "trim()", "new_string": "trim_start()"},
    ]})


def test_write_always_fires(handler):
    # No prior content to compare against.
    write(handler, HANDLER)
    assert fires("Write", {"file_path": handler, "content": HANDLER})


# --- context the old_string fragment alone cannot show ---------------------
# Each of these was silenced by a fragment-only normaliser: the fragment looks
# like code, a comment or an import, but in its file it is rendered text or a
# binding the page depends on. So the hook judges the edit inside its file.

RAW_HANDLER = """use axum::response::Html;

pub async fn about() -> Html<&'static str> {
    Html(r#"<main>
  <p>Docs live at https://old.example.org/guide today.</p>
  <p><span>one</span><span>two</span></p>
</main>"#)
}
"""


def test_url_inside_multiline_raw_string_fires(handler):
    # A fragment wholly inside r#"..."# carries no quote to show it is a
    # string; `//` there is page text, not a comment.
    assert fires("Edit", h_edit(handler,
                                "<p>Docs live at https://old.example.org/guide today.</p>",
                                "<p>Docs live at https://new.example.org/guide today.</p>",
                                before=RAW_HANDLER))


def test_space_inside_multiline_raw_string_fires(handler):
    assert fires("Edit", h_edit(handler, "<p><span>one</span><span>two</span></p>",
                                "<p><span>one</span> <span>two</span></p>", before=RAW_HANDLER))


def test_space_inside_multiline_ts_template_literal_fires(tmp_path):
    before = ("export function render(el: HTMLElement, a: string, b: string) {\n"
              "  el.innerHTML = `\n    <div class=\"row\">\n"
              "      <span>${a}</span><span>${b}</span>\n    </div>`;\n}\n")
    assert fires("Edit", applied(str(tmp_path / "client" / "render.ts"), before,
                                 "<span>${a}</span><span>${b}</span>",
                                 "<span>${a}</span> <span>${b}</span>"))


def test_css_module_import_swap_fires(tmp_path):
    assert fires("Edit", x_edit(tmp_path, "import styles from './card.module.css';",
                                "import styles from './card-dense.module.css';"))


def test_component_import_swap_fires(tmp_path):
    assert fires("Edit", x_edit(tmp_path, "import Button from './Button';",
                                "import Button from './FancyButton';"))


def test_removing_an_import_still_in_use_fires(tmp_path):
    # First half of a two-edit swap (remove here, re-add from elsewhere next).
    assert fires("Edit", x_edit(tmp_path, "import styles from './card.module.css';\nimport Button from './Button';\n",
                                "import styles from './card.module.css';\n"))


def test_pure_deletion_fires(handler):
    # new_string "" has no unique place in the file to undo from.
    before = HANDLER.replace("use axum::response::Html;\n",
                             "use axum::response::Html;\nuse std::fmt;\n")
    assert fires("Edit", h_edit(handler, "use std::fmt;\n", "", before=before))


def test_rust_use_path_swap_fires(handler):
    before = HANDLER.replace("use axum::response::Html;\n",
                             "use axum::response::Html;\nuse crate::views::v1::render;\n")
    assert fires("Edit", h_edit(handler, "use crate::views::v1::render;",
                                "use crate::views::v2::render;", before=before))


def test_rust_trait_import_swap_fires(handler):
    before = HANDLER.replace("use axum::response::Html;\n",
                             "use axum::response::Html;\nuse tokio_stream::StreamExt as _;\n")
    assert fires("Edit", h_edit(handler, "use tokio_stream::StreamExt as _;",
                                "use futures::StreamExt as _;", before=before))


def test_whitespace_at_fragment_edge_fires(tmp_path):
    # `<span>a</span>` -> `<span>a</span>\n` puts a rendered space before the
    # next inline element; trimming the fragment's ends would hide it.
    assert fires("Edit", t_edit(tmp_path, "<span>a</span>", "<span>a</span>\n"))


def test_whitespace_inside_pre_fires(tmp_path):
    before = "<pre>\nline one\n  indented\n</pre>\n"
    assert fires("Edit", t_edit(tmp_path, "  indented", "      indented", before=before))


def test_comment_added_beside_a_textarea_is_silent(tmp_path):
    # Only the inside of a preformatted element keeps its spaces verbatim.
    before = '<form>\n  <textarea name="d">  keep  </textarea>\n  <div class="x">y</div>\n</form>\n'
    assert not fires("Edit", t_edit(tmp_path, '  <div class="x">',
                                    '  <!-- lint-ok: wrapper -->\n  <div class="x">', before=before))


def test_whitespace_inside_textarea_fires(tmp_path):
    before = '<form>\n  <textarea name="d">  keep  </textarea>\n</form>\n'
    assert fires("Edit", t_edit(tmp_path, ">  keep  <", "> keep <", before=before))


def test_conditional_comment_change_fires(tmp_path):
    before = '<!--[if lt IE 9]><p class="old">Upgrade</p><![endif]-->\n<h1>x</h1>\n'
    assert fires("Edit", t_edit(tmp_path, "Upgrade", "Please upgrade", before=before))


def test_edit_not_found_in_file_fires(handler):
    # The file no longer holds new_string (changed since, or never written),
    # so the prior file cannot be rebuilt and nothing is proven.
    write(handler, HANDLER)
    assert fires("Edit", {"file_path": handler, "old_string": "/// a", "new_string": "/// b"})


def test_replace_all_fires(handler):
    e = h_edit(handler, "/// Renders the poll page.", "/// Draws the poll page.")
    e["replace_all"] = True
    assert fires("Edit", e)


# --- unchanged path classification -----------------------------------------

def test_non_rendering_rust_file_is_silent(tmp_path):
    p = str(tmp_path / "lib.rs")
    before = "pub fn add(a: u8, b: u8) -> u8 { a + b }\n"
    assert not fires("Edit", applied(p, before, "a + b", "a.wrapping_add(b)"))


def test_docs_html_is_silent(tmp_path):
    p = str(tmp_path / "docs" / "x.html")
    assert not fires("Edit", applied(p, "<p>a</p>\n", "<p>a</p>", "<p>b</p>"))
