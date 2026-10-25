#!/usr/bin/env bash
# No workspace needed: the target is a real file under ~/Repos, which the e2e
# sandbox mounts read-only, so a permitted edit fails with EROFS and changes nothing.
exit 0
