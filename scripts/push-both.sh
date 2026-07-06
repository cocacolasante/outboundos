#!/usr/bin/env bash
# push-both.sh — push the current branch to both origin (cocacolasante)
# and mirror (cocacolasante215).
#
# Two-account HTTPS authentication is annoying on this machine because
# ``gh auth git-credential`` serves the ACTIVE account's token regardless
# of which username git asks for.  Workaround: switch active accounts
# between the two pushes.  Idempotent — safe to re-run.
#
# Daily work: just ``git push origin <branch>`` as usual.  Run this
# script when you want the mirror caught up too.
set -euo pipefail

BRANCH="${1:-$(git symbolic-ref --short HEAD)}"

echo "→ pushing ${BRANCH} to origin (cocacolasante)…"
gh auth switch --user cocacolasante >/dev/null
git push origin "${BRANCH}"

echo "→ pushing ${BRANCH} to mirror (cocacolasante215)…"
gh auth switch --user cocacolasante215 >/dev/null
git push mirror "${BRANCH}"

# Restore the canonical default so subsequent `gh` and `git` commands
# behave as expected for daily work.
gh auth switch --user cocacolasante >/dev/null
echo "✓ both remotes updated; active account reset to cocacolasante"
