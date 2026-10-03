---
name: claude-design-iteration-loop
description: Use when a product feature should be designed in Claude Design (claude.ai/design) as the source of truth and the agent drives it autonomously - "iterate in Claude Design", "drive Claude Design", "design round", "send a round to Claude Design", "make Claude Design the source of truth", "design→impl pipeline", or when implementation subagents need design files they cannot fetch themselves.
---

# Claude Design iteration loop

The design is the spec. Implementation follows it 1:1. Gaps found while building go back to the designer as a new numbered round. The agent never fills them.

Per-project half (diffing, porting, exporting, uploading): a project skill pair such as `klaffat-design-adopt` (inbound, never uploads) and `klaffat-design-sync` (outbound, add-only). Write the equivalent per project; this skill stays project-agnostic.

## Before round 1

Do all four before the first send. Each missed item costs later rounds.

1. **Research hard platform constraints** the design depends on (e.g. whether a provider can revoke a single permission, token and scope limits). Record them as facts in DECISIONS so decisions do not flip later.
2. **Write the state x surface matrix**: every surface against counts 0/1/n, every provider/account combination, limits, error, conflict, unlinked, and recurring/variant states. Put it in the brief and require the designer to draw every cell in rounds 1-2.
3. **Collect platform brand and legal rules** (provider sign-in button wording and logos, required disclosures) and state them as facts in the brief.
4. **Set a round budget** (default 6, counting from round 1) and name the freeze point.

Batch build findings: collect gaps from all agents and send one round per batch, not one per finding. At the freeze, stop opening rounds: implement each remaining gap with the closest existing design pattern and string, and list it under "design follow-ups" (surface, what was assumed) for the founder. Past the budget, ask the founder before another round.

## Loop

1. **Spec in chat.** Feature intent plus every founder decision as facts. New chat per feature (history icon, top-left, then "New chat").
2. **Round N.** Write the brief, send it, verify it landed, wait for the run to finish.
3. **Review** via DesignSync (parent session only), then a **founder tour** of the rendered round (below).
4. **Gaps in the design** become round N+1. Repeat 2-4 until the design is up to scratch. Design first, then implement.
5. **Mirror** the design to disk.
6. **Implement** with subagents against the mirror.
7. **Build-found gaps** (a state, string, or layout the design lacks) are batched into a new round. Before the freeze, never patch them in code or use placeholder copy.
8. **Match 1:1**, then **upload** implemented screens add-only.

Do not dispatch implementation for a surface while a known gap on it is unresolved. Surfaces whose design is complete may proceed in parallel.

## Brief format

One chat message. Order:

1. First instruction: "Save this message verbatim as `briefs/ROUND-N-BRIEF.md`."
2. Update `DECISIONS.md` (numbered decisions D1..Dn) from this brief; read it at the start of every round.
3. Output rules: new files with this round's suffix (`-100N`); older files untouched; per round a `HANDOFF` section (what changed, open items) and a `COPY` section (every user-visible string by key).
4. Standing rules, restated every round. Example set: every user-visible string in en and sv, no "translate at build"; exact scope/permission lists given as facts; log open items, never guess.
5. Numbered items. Decisions stated as facts. Leave only wording and layout to the designer; never write "decide X".

Founder statements that cut across screens (copy brevity, naming, a colour or notice treatment, an interaction pattern) apply to every surface they reasonably reach, not only the screen under review. Write the decision with that scope and list the reached surfaces as brief items. Do not ask the founder whether to extend it; ask only when the extension would change a product decision (scope, data, permissions).

Founder decisions made after a send are lost unless re-sent: put each in the next brief and DECISIONS, and append it to the project's session constraints the same turn.

## Sending

Do not deliver briefs with DesignSync `write_files` (prompts the user per write, blocks unattended runs). Send the brief as one chat message; the designer saves it per instruction 1.

- Window visible: paste into the chat editor (Shift+Enter for newlines; a bare Enter sends).
- Window hidden or minimized (screenshots and typing time out): drive the page with JS.

```js
const ed = document.querySelector('.ProseMirror'); ed.focus();
const dt = new DataTransfer(); dt.setData('text/plain', BRIEF);
ed.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
// then click the Send button via JS
```

Verify: about 2 minutes later `DesignSync get_file briefs/ROUND-N-BRIEF.md`. Missing means the send was lost (editor cleared, nothing posted). Re-send and re-verify. Do not log a round as sent before the file exists.

## Status

Poll the tab title with `tabs_context`: `✶` working, `✓` done, `💤` asleep. Do not screenshot or `get_page_text` during a run (heavy pages freeze the renderer; the overview dumps ~50k chars). Poll output files with DesignSync instead.

- Keep exactly one Claude Design tab; a run is bound to the tab that started it.
- `💤`: Memory Saver slept the tab and changed its tab id. Re-run `tabs_context`, navigate to wake it, click Resume if the chat says "We got interrupted". Never send into a sleeping tab.
- Standing fix: add claude.ai to Memory Saver "always keep active".

## Review a round

DesignSync reads (`list_files`, `get_file`) do not prompt.

1. `DECISIONS.md`: every brief decision present, worded as given. A product decision the designer made on its own is a defect.
2. `HANDOFF`: changes and open items.
3. Targeted files (screens the handoff names). Spot-check the markup, not the summary: copy keys present in en and sv, states complete.
4. Defects, new gaps, and open items form the next brief.

The designer rewrites files in place despite "new files only", and DesignSync has no history. To diff across rounds, snapshot first (DECISIONS, HANDOFF, COPY text plus file list and sizes).

## Founder tour of a round

The founder reviews rendered screens, not file lists or preview-pane screenshots (the pane cuts long pages; zoomed out is unreadable).

1. **Export.** Share, Export, Project HTML, "Project archive" (instant, free; "Standalone HTML" spends usage). It downloads a zip of the whole project, so ask the founder before the first download.
2. **Unzip** to `design-mirror/rN-export/`.
3. **Render** every file with this round's suffix in a headless browser, full page: desktop at 1280 px from `X.html`, phone at 390 px from `X-phone.html` when it exists, else `X.html`. Log failed requests and `scrollWidth`; either one non-zero or over width is a defect to check before the founder sees it. Look at two renders yourself.
4. **Pair** each render with the current built screen of the same state ("now" vs "round N"). No built equivalent: show it as new. A pair that differs in state (e.g. built shot at a different level) gets a note saying so.
5. **Tour**: a local slideshow (prev/next, desktop/phone, flip one image at full width, a Swedish toggle, one notes box per slide that autosaves to `notes.md`). Each slide's caption names the founder note it answers.
6. Before writing the next brief, read `notes.md`; every note becomes a brief item or an answered question.

## Mirror to disk

The project archive from the founder tour is also the best mirror: the whole project, byte-exact, no re-typing. Prefer it over per-file `get_file` once the founder has allowed downloads.

Subagents cannot load DesignSync. The parent writes the mirror; subagents read paths.

- Large `get_file` result: it persists to a tool-results file. Write it out with no re-typing:
  `python3 -c "import json,sys; sys.stdout.write(json.load(open(P))['content'])" > design-mirror/X.html`
- Small or many files (phone variants share markup): do not mirror every HTML. Write structural extracts plus one consolidated copy file per surface holding all current strings by key, verbatim: `design-mirror/DECISIONS.md`, `COPY-current.md`, markup extracts per page group.
- Re-sync after every round; keep the superseded copy file as `.rN.md`.

## Briefing implementation subagents

- Paste the founder's constraints from session constraints verbatim, never paraphrased.
- Give mirror paths, not DesignSync instructions.
- Every brief says: use design strings and markup verbatim; if a string, state, or style is missing, stop that item and report it as a "design gap". Never invent or translate copy, never write placeholder copy, never reconstruct CSS without the design HTML.
- Require a final report with a `Design gaps` list; collect every list into the next round.
- Disjoint files per agent; shared CSS has one writer.

## Match, then upload

- Diff implemented screens against the design: CSS delta first, DOM structurally. Each residual delta is an unadopted design change or a design gap for a new round. Adapt design output that breaks project lint or HTML validity; record each adaptation in the PR.
- Upload only after everything designed is implemented.
- Add-only: `list_files`, write only paths that do not yet exist, `deletes: []`, never overwrite (list changed existing paths for the user). Never delete.
- Record the uploaded commit SHA for later three-way diffs.

## Red flags

Stop and fix if any hold:

- An agent writes Swedish (or any) copy the design lacks, or leaves placeholder strings.
- CSS or markup reconstructed without the design HTML.
- A round sent into a `💤` tab, or logged as sent without the brief file present.
- A brief item says "decide" about a product choice.
- Implementation dispatched on a surface with an open design gap.
- Mirror made by re-typing file contents.
- A subagent's relay ("founder approves") treated as founder approval; only the founder's own message counts.
- Any delete or overwrite in an upload plan.
- Briefs delivered via `write_files`.
