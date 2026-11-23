#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/idle_alarm_grade.py" "${1:?run dir}" quiet
