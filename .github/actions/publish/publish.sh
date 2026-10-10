#!/usr/bin/env bash
# The publish step (see action.yml). Commit -> push (fetch+rebase retry) -> dispatch pages-deploy.yml.
# Never exits non-zero for an operational problem (a missed publish self-heals on the next run / the 4-hourly Pages fallback); a real problem is a ::warning::.
# Env (all optional): PUB_PATHS PUB_MESSAGE PUB_BRANCH PUB_ON_CONFLICT PUB_DISPATCH PUB_START_SHA PUB_WORKFLOW
#                     PUB_ATTEMPTS PUB_SLEEP_BASE PUB_GH (the gh command, for tests) GITHUB_OUTPUT.
set -u

BRANCH="${PUB_BRANCH:-main}"
MESSAGE="${PUB_MESSAGE:-chore: data refresh}"
ON_CONFLICT="${PUB_ON_CONFLICT:-skip}"
DISPATCH="${PUB_DISPATCH:-auto}"
WORKFLOW="${PUB_WORKFLOW:-pages-deploy.yml}"
ATTEMPTS="${PUB_ATTEMPTS:-4}"
SLEEP_BASE="${PUB_SLEEP_BASE:-4}"
GH="${PUB_GH:-gh}"
START="${PUB_START_SHA:-${GITHUB_SHA:-}}"
OUT="${GITHUB_OUTPUT:-/dev/null}"
ID=(-c user.name=clairvoyance-bot -c user.email=bot@clairvoyance.local)

# Commit-message tokens (the message is a workflow input, so it cannot run `date`): {MT_HHMM} -> 09:42, {MT_DATE} -> 2026-10-10, {UTC_STAMP} -> 2026-10-10 15:42 UTC
MESSAGE="${MESSAGE//\{MT_HHMM\}/$(TZ=America/Denver date +%H:%M)}"
MESSAGE="${MESSAGE//\{MT_DATE\}/$(TZ=America/Denver date +%Y-%m-%d)}"
MESSAGE="${MESSAGE//\{UTC_STAMP\}/$(date -u '+%Y-%m-%d %H:%M UTC')}"

committed=0
pushed=1
docs_changed=0
dispatched=0

emit() {
  { echo "committed=$committed"; echo "pushed=$pushed"; echo "docs_changed=$docs_changed"; echo "dispatched=$dispatched"; } >> "$OUT"
}

TOP="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$TOP" || exit 0

# ── 1. commit ────────────────────────────────────────────────────────────────────────────────────────────────────
if [ -n "${PUB_PATHS:-}" ]; then
  for p in $PUB_PATHS; do
    git add -- "$p" 2>/dev/null || true          # a path that does not exist (yet) must not stop the others
  done
  if ! git diff --cached --quiet; then
    if git "${ID[@]}" commit -q -m "$MESSAGE"; then
      committed=1
    else
      echo "::warning::publish: commit failed"
    fi
  else
    echo "publish: no change in: $PUB_PATHS"
  fi
fi

# ── 2. push (also anything an earlier step / Python helper committed locally but could not push) ───────────────────
git fetch --quiet origin "$BRANCH" 2>/dev/null || echo "::warning::publish: fetch origin/$BRANCH failed"

# actions/checkout is a depth-1 clone, and several steps (the lock workflows' marker reads) additionally run `git fetch --depth=1 origin main`: origin/<branch> and HEAD are then two
# unrelated shallow roots, `rev-list origin/main..HEAD` wrongly counts the checkout commit itself as "unpushed", and a rebase would try to replay that root commit (conflict).
# Deepen until the two histories join (a few small fetches; only done when HEAD moved off the commit this run started on).
connect_history() {
  git merge-base HEAD "origin/$BRANCH" >/dev/null 2>&1 && return 0
  [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ] || return 1
  local n
  for n in 1 2 3 4; do
    git fetch --quiet --deepen=64 origin "$BRANCH" 2>/dev/null || return 1
    git merge-base HEAD "origin/$BRANCH" >/dev/null 2>&1 && return 0
  done
  return 1
}

ahead=0
if [ -n "$START" ] && [ "$(git rev-parse HEAD 2>/dev/null)" = "$(git rev-parse --verify -q "$START^{commit}" 2>/dev/null)" ]; then
  :                                                 # HEAD is still the commit this run started on: nothing was committed or pulled, so there is nothing to push
elif connect_history; then
  ahead="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
else
  pushed=0
  echo "::warning::publish: could not relate HEAD to origin/$BRANCH (shallow checkout) -- not pushing; the next run will retry"
fi
if [ "${ahead:-0}" -gt 0 ]; then
  pushed=0
  rebase_args=(--autostash)
  [ "$ON_CONFLICT" = "prefer-mine" ] && rebase_args+=(-X theirs)
  attempt=1
  while [ "$attempt" -le "$ATTEMPTS" ]; do
    this=$attempt
    attempt=$((attempt + 1))
    if git push -q origin "HEAD:$BRANCH" 2>/dev/null; then
      pushed=1
      echo "publish: pushed $ahead commit(s) to $BRANCH (attempt $this)"
      break
    fi
    git fetch --quiet origin "$BRANCH" 2>/dev/null || true
    if git "${ID[@]}" rebase "${rebase_args[@]}" "origin/$BRANCH" >/dev/null 2>&1; then
      continue                                    # rebased cleanly: push again straight away (no sleep)
    fi
    git rebase --abort >/dev/null 2>&1 || true
    echo "::warning::publish: rebase onto origin/$BRANCH conflicted (attempt $this/$ATTEMPTS)"
    if [ "$ON_CONFLICT" != "prefer-mine" ]; then
      break                                        # a real conflict will not go away by waiting: skip, the next run catches up
    fi
    sleep $((this * SLEEP_BASE))
  done
  [ "$pushed" = "1" ] || echo "::warning::publish: could not push to $BRANCH -- the change stays local and the next run will retry"
fi

# ── 3. dispatch the deploy ────────────────────────────────────────────────────────────────────────────────────────
if [ "$pushed" = "1" ]; then
  if [ -z "$START" ] || ! git diff --quiet "$START" HEAD -- docs/ 2>/dev/null; then
    docs_changed=1                                 # also when START is unknown/unreadable: deploying once too often is harmless, missing one is not
  fi
fi
want=0
case "$DISPATCH" in
  never)  want=0 ;;
  always) want=1 ;;
  *)      [ "$docs_changed" = "1" ] && want=1 ;;
esac
if [ "$want" = "1" ] && [ "$pushed" = "1" ]; then
  # A job can have several publish steps (e.g. one after the ledger backup, a catch-all at the end): dispatch once per distinct docs/ state.
  tree="$(git rev-parse "HEAD:docs" 2>/dev/null || echo "")"
  stamp="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/publish_last_docs_tree"
  if [ "$DISPATCH" != "always" ] && [ -n "$tree" ] && [ -f "$stamp" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$tree" ]; then
    echo "publish: pages-deploy.yml was already dispatched for this docs/ state earlier in this job"
    want=0
  fi
fi
if [ "$want" = "1" ] && [ "$pushed" = "1" ]; then
  i=1
  while [ "$i" -le 3 ]; do
    if "$GH" workflow run "$WORKFLOW" --ref "$BRANCH"; then
      dispatched=1
      [ -n "${tree:-}" ] && echo "$tree" > "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/publish_last_docs_tree" 2>/dev/null
      echo "::notice::publish: dispatched $WORKFLOW (docs/ changed in this run)"
      break
    fi
    sleep $((i * SLEEP_BASE / 2))
    i=$((i + 1))
  done
  [ "$dispatched" = "1" ] || echo "::warning::publish: could not dispatch $WORKFLOW (needs 'actions: write'); the scheduled fallback will deploy within 4 h"
elif [ "$docs_changed" = "0" ]; then
  echo "publish: docs/ unchanged by this run -- no deploy needed"
fi

emit
exit 0
