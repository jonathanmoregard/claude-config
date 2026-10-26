# Claude dispatch — host vs sandbox per-repo gate.
#
# Trust anchor is HOST-SIDE, keyed by git remote URL (or absolute path when no
# remote). A repo cannot ship a downgrade — mode is stored in
# ~/.config/claude-sandbox/trust/<sha256>. State lives outside ~/.claude
# because hashes are a behavioral fingerprint and mode is runtime, not config.
#
# Sourced from ~/.zshrc. Provides:
#   claude         clear + dispatch with --continue
#   claudee        clear + dispatch without --continue
#   claude-mode    show | sandbox | host | forget

_claude_trust_file() {
    local root key
    root=$(git rev-parse --show-toplevel 2>/dev/null) || root="$PWD"
    key=$(git -C "$root" remote get-url origin 2>/dev/null) || key="path:$root"
    local dir="$HOME/.config/claude-sandbox/trust"
    mkdir -p "$dir" && chmod 700 "$dir" 2>/dev/null
    local hash
    hash=$(print -r -- "$key" | sha256sum | cut -d' ' -f1)
    print -r -- "$dir/$hash"
}

_claude_repo_mode() {
    local f mode
    f=$(_claude_trust_file)
    if [[ -f "$f" ]]; then
        mode=$(head -n1 "$f" | tr -d '[:space:]')
        [[ "$mode" == sandbox || "$mode" == host ]] && { print -r -- "$mode"; return }
    fi
    local key root
    root=$(git rev-parse --show-toplevel 2>/dev/null) || root="$PWD"
    key=$(git -C "$root" remote get-url origin 2>/dev/null) || key="path:$root"
    print -u2 -r -- "[claude] first time for: $key"
    print -u2 -n "[claude] sandbox (s) or host (h)? [s/h] "
    local reply
    read reply < /dev/tty
    case "${reply:l}" in
        s|sandbox) print -r -- sandbox > "$f"; print -r -- sandbox ;;
        h|host)    print -r -- host    > "$f"; print -r -- host    ;;
        *) print -u2 -r -- "[claude] unknown '$reply' — running as host, not saved"; print -r -- host ;;
    esac
}

function claude-mode() {
    local action="${1:-show}"
    local f
    f=$(_claude_trust_file)
    case "$action" in
        show)    [[ -f "$f" ]] && { print "mode: $(cat "$f")"; print "file: $f"; } || print "(not set) file: $f" ;;
        sandbox) print -r -- sandbox > "$f"; print "set to sandbox" ;;
        host)    print -r -- host    > "$f"; print "set to host"    ;;
        forget)  rm -f "$f"; print "forgotten" ;;
        *) print "usage: claude-mode [show|sandbox|host|forget]" ;;
    esac
}

function claude() {
    clear
    case "$(_claude_repo_mode)" in
        sandbox) claude-sandbox "$PWD" >/dev/null || return 1
                 devcontainer exec --workspace-folder "$PWD" -- claude --continue "$@" ;;
        *)       command claude --continue "$@" ;;
    esac
}
function claudee() {
    clear
    case "$(_claude_repo_mode)" in
        sandbox) claude-sandbox "$PWD" >/dev/null || return 1
                 devcontainer exec --workspace-folder "$PWD" -- claude "$@" ;;
        *)       command claude "$@" ;;
    esac
}
