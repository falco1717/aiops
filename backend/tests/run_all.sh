#!/bin/sh
# Every suite, in one go, the way they are meant to be run: from backend/, each
# against a fresh database, and with neither agent CLI on PATH — several suites
# assert what happens when the binary is missing, and a real one on PATH turns
# those checks into live agent runs.
#
# Not a test itself: it exists so "all suites pass" is one command rather than
# fifteen, and so the per-suite check counts are printed together.
set -u
cd "$(dirname "$0")/.." || exit 1

# Dependencies that are not the application's, and that fail several suites
# apiece from the middle rather than at the start. Checked here so a clean image
# is told what to install in one line instead of one suite at a time.
missing=""
python -c "import httpx" 2>/dev/null || missing="$missing\n  pip install -r requirements-dev.txt   (httpx)"
command -v ssh-keygen >/dev/null 2>&1 || missing="$missing\n  apt-get install -y openssh-client     (ssh-keygen)"
# test_github.py clones a local bare repo through a real `git` to prove the
# workspace-from-github endpoint never leaves a token in .git/config — the
# same reason ssh-keygen is required above rather than fixtured.
command -v git >/dev/null 2>&1 || missing="$missing\n  apt-get install -y git                (git)"
if [ -n "$missing" ]; then
    printf 'Missing test prerequisites:%b\n' "$missing"
    printf 'See tests/README.md.\n'
    exit 1
fi

export AIOPS_JWT_SECRET=test
export AIOPS_ADMIN_PASSWORD=devpassword123
export AIOPS_COOKIE_SECURE=false
export AIOPS_SECRET_KEY="${AIOPS_SECRET_KEY:-ZmFrZS1zZWNyZXQta2V5LWZvci10ZXN0cy0wMDAwMDAwMD0=}"

total_pass=0
total_fail=0
failed_suites=""

for suite in tests/test_*.py; do
    name=$(basename "$suite" .py)
    rm -f ./*.db
    export AIOPS_DATABASE_URL="sqlite+aiosqlite:///./${name}.db"
    export AIOPS_WORKSPACE_ROOT="$(mktemp -d)"
    export AIOPS_ATTACHMENTS_ROOT="$(mktemp -d)"
    out=$(python "$suite" 2>&1)
    code=$?
    passed=$(printf '%s\n' "$out" | grep -c '^\[PASS\]')
    failed=$(printf '%s\n' "$out" | grep -c '^\[FAIL\]')
    total_pass=$((total_pass + passed))
    total_fail=$((total_fail + failed))
    if [ "$code" -ne 0 ]; then
        failed_suites="$failed_suites $name"
        printf '\n===== %s: EXIT %s (%s pass, %s fail) =====\n' "$name" "$code" "$passed" "$failed"
        printf '%s\n' "$out" | grep -E '^\[FAIL\]|Traceback|Error|error:' | head -40
        printf '%s\n' "$out" | tail -25
    else
        printf '%-28s ok  %4s checks\n' "$name" "$passed"
    fi
done

rm -f ./*.db
printf '\n%s checks passed, %s failed, across %s suites\n' \
    "$total_pass" "$total_fail" "$(ls tests/test_*.py | wc -l)"
if [ -n "$failed_suites" ]; then
    printf 'FAILING SUITES:%s\n' "$failed_suites"
    exit 1
fi
printf 'All suites passed.\n'
