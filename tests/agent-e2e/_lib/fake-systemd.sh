#!/usr/bin/env bash
# A stand-in for `systemd-run --user` and `systemctl --user show`, for e2e runs
# whose sandbox has no user bus. Installed into $E2E_BIN under both names; the
# name it is called by picks the behaviour. State lives in $FAKE_SYSTEMD_DIR.
#
#   systemd-run --user --unit=<u> [-p K=V]... [--working-directory=D] -- cmd...
#       starts cmd as a real background process and records its pid
#   systemctl --user show <u> -p Prop...
#       a transient RemainAfterExit unit: active/running while the pid lives,
#       active/exited result=success afterwards; not-found when never started
set -uo pipefail
dir="${FAKE_SYSTEMD_DIR:?}"
mkdir -p "$dir"
echo "$(basename "$0") $*" >> "$dir/calls.log"

case $(basename "$0") in
  systemd-run)
    unit="" wd="$PWD"
    while [ $# -gt 0 ]; do
      case $1 in
        --) shift; break ;;
        --unit=*) unit=${1#--unit=}; shift ;;
        --unit) unit=$2; shift 2 ;;
        --working-directory=*) wd=${1#--working-directory=}; shift ;;
        -p|--property) shift 2 ;;
        *) shift ;;
      esac
    done
    [ -n "$unit" ] && [ $# -gt 0 ] || { echo "fake systemd-run: need --unit and a command" >&2; exit 1; }
    unit=${unit%.service}
    [ -e "$dir/$unit.pid" ] && { echo "Failed to start transient service unit: Unit $unit.service was already loaded" >&2; exit 1; }
    (cd "$wd" && setsid "$@" </dev/null >/dev/null 2>&1 & echo $! > "$dir/$unit.pid")
    date '+%a %Y-%m-%d %H:%M:%S %Z' > "$dir/$unit.started"
    echo "Running as unit: $unit.service"
    ;;
  systemctl)
    unit="" props=()
    while [ $# -gt 0 ]; do
      case $1 in
        --user|show|--no-pager) shift ;;
        -p|--property) props+=("$2"); shift 2 ;;
        --value) shift ;;
        -*) shift ;;
        *) unit=${1%.service}; shift ;;
      esac
    done
    if [ ! -e "$dir/$unit.pid" ]; then
      for p in "${props[@]}"; do
        case $p in LoadState) echo "LoadState=not-found" ;; ActiveState) echo "ActiveState=inactive" ;;
          SubState) echo "SubState=dead" ;; *) echo "$p=" ;; esac
      done
      exit 0
    fi
    pid=$(cat "$dir/$unit.pid")
    if kill -0 "$pid" 2>/dev/null; then sub=running; else sub=exited; fi
    for p in "${props[@]}"; do
      case $p in
        LoadState) echo "LoadState=loaded" ;;
        ActiveState) echo "ActiveState=active" ;;
        SubState) echo "SubState=$sub" ;;
        Transient) echo "Transient=yes" ;;
        Type) echo "Type=simple" ;;
        CollectMode) echo "CollectMode=inactive" ;;
        InvocationID) echo "InvocationID=fake$pid" ;;
        ExecMainStartTimestamp) echo "ExecMainStartTimestamp=$(cat "$dir/$unit.started")" ;;
        Result) echo "Result=success" ;;
        ExecMainStatus) echo "ExecMainStatus=0" ;;
        *) echo "$p=" ;;
      esac
    done
    ;;
esac
