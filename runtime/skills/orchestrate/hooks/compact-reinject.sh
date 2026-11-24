#!/usr/bin/env bash
# SessionStart(startup|compact|clear) hook: when this session has a live mission
# handoff in this project dir, remind it to re-read the handoff from disk —
# context decays after compaction, the files do not.
set -euo pipefail

# A notes-dir mission*.md only counts as THIS session's live work if touched within
# this window; older ones are leftovers of earlier sessions (a long-lived project dir
# accumulates them) and reminding about them wastes tokens and misleads. Does not
# apply to the session-reflect resolved handoff, which counts by session id instead.
MISSION_FRESH_MINUTES=1440

# The SessionStart payload names THIS session. Empty or invalid stdin (or no jq)
# leaves it empty, which only means no resolved handoff can be proven ours.
HOOK_SESSION=""
if [[ ! -t 0 ]] && command -v jq >/dev/null 2>&1; then
  HOOK_SESSION="$(jq -r '.session_id // "" | strings' 2>/dev/null || true)"
fi

# Working notes live in userspace (`~/.local/state/claude-tasks/<repo>/`) per CLAUDE.md, because
# repo-relative notes get auto-committed and published. A project may symlink
# `<repo>/tasks` at that dir, in which case both spellings resolve to the same files.
# BOTH are checked, and the repo-relative one wins: a project that has not been
# migrated must not lose its reminder, and a reminder naming a path nothing writes
# to any more is worse than no reminder at all.
NOTES_USERSPACE="$HOME/.local/state/claude-tasks/${PWD##*/}"
NOTES=""
LIVE_NOTES=""  # first dir holding a RECENT mission*.md (see MISSION_FRESH_MINUTES)
for _d in "tasks" "$NOTES_USERSPACE"; do
  # EXISTENCE CHECK ONLY, and deliberately a GLOB: "does this project use the
  # mission convention at all". The files it matches are human-readable copies —
  # the authoritative handoff lives outside the repo — so this must never be read
  # as content, only counted.
  if [[ -z "$NOTES" ]] && compgen -G "$_d/mission*.md" >/dev/null 2>&1; then NOTES="$_d"; fi
  if [[ -z "$LIVE_NOTES" && -n "$(find -H "$_d" -maxdepth 1 -name 'mission*.md' -mmin "-$MISSION_FRESH_MINUTES" -print -quit 2>/dev/null)" ]]; then
    LIVE_NOTES="$_d"
  fi
done
# What the reminders NAME. With no notes anywhere yet, name userspace: that is where
# this project's notes are supposed to go.
NOTES_SHOWN="${NOTES:-$NOTES_USERSPACE}"

# Set below once the session-reflect handoff is resolved.
RESOLVED_LIVE=0
mission_exists() {
  [[ -n "$LIVE_NOTES" || "$RESOLVED_LIVE" == 1 ]]
}

# Name the AUTHORITATIVE handoff when session-reflect can resolve one
# ($REFLECT_ROOT/sessions/<sid>/handoff/mission.md, out of repo). Falls back to the
# human-facing `tasks/mission.md` convention when there is none — acceptable here
# and nowhere else: this hook only injects a "re-read your handoff" reminder into
# an interactive session whose human owns this cwd. It never drives an unattended
# fork, which is why idle-handoff.sh has no such fallback.
#
# The resolver is asked for THIS session's handoff for this project dir, any age:
# a /compact keeps the session id, so that is the handoff this context was working
# from. Filtering by session BEFORE selection matters — the unfiltered resolver
# returns the newest handoff across all sessions, so a newer foreign one would
# hide ours. The <sid> (…/<sid>/handoff/mission.md) is re-checked on the way out.
# Another session's handoff is never named. A cross-session
# continuation needs no reminder: the in-place reset's pickup message and the
# idle-handoff fork's prompt both name their handoff explicitly.
RESOLVE="$HOME/.claude/skills/session-reflect/reflect.sh"
MISSION="${LIVE_NOTES:-$NOTES_SHOWN}/mission.md"
if [[ -n "$HOOK_SESSION" && ( -x "$RESOLVE" || -f "$RESOLVE" ) ]]; then
  RESOLVED="$(bash "$RESOLVE" --mission-path "$PWD" "$HOOK_SESSION" </dev/null 2>/dev/null || true)"
  if [[ -n "$RESOLVED" && -f "$RESOLVED" ]]; then
    RESOLVED_SID="$(basename -- "$(dirname -- "$(dirname -- "$RESOLVED")")")"
    [[ "$RESOLVED_SID" == "$HOOK_SESSION" ]] && { MISSION="$RESOLVED"; RESOLVED_LIVE=1; }
  fi
fi

mission_exists || exit 0

# Any project with a live mission artifact gets a re-read nudge after
# compact/clear. Compaction is documented to invalidate the cached prefix that
# held CLAUDE.md and mission context; disk beats in-context memory once
# compaction has fired.
cat <<EOF
[context-lifecycle] A mission handoff exists in this project ($MISSION). Context was just compacted, cleared, or restarted — re-read that file now, plus $NOTES_SHOWN/session-constraints.md if present. Do not rely on your memory of the pre-compact conversation for state that lives on disk.
EOF
