#!/usr/bin/env bash
# Pre-commit regression check — runs before every commit
# Install: ln -sf ../../scripts/pre-commit-check.sh .git/hooks/pre-commit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Use git to locate the worktree root (works correctly in both worktrees and
# the main repo, unlike dirname-based detection which breaks for symlinked hooks).
PROJECT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

ERRORS=0

echo "🔍 Running pre-commit checks..."

# 1. Check for hardcoded credentials in staged files
STAGED=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(sh|py|json|yml)$' || true)
if [[ -n "$STAGED" ]]; then
    for f in $STAGED; do
        # Skip examples and tests
        [[ "$f" == *.example ]] && continue
        [[ "$f" == tests/* ]] && continue
        
        if grep -qE 'postgres://[^$]*:[^$]*@' "$f" 2>/dev/null; then
            echo "✗ Hardcoded postgres credentials in $f"
            ERRORS=1
        fi
    done
fi

# 2. Validate JSON
for json in $(git diff --cached --name-only --diff-filter=ACM | grep '\.json$' || true); do
    if ! jq empty "$json" 2>/dev/null; then
        echo "✗ Invalid JSON: $json"
        ERRORS=1
    fi
done

# 3. Bash syntax check
for sh in $(git diff --cached --name-only --diff-filter=ACM | grep '\.sh$' || true); do
    if ! bash -n "$sh" 2>/dev/null; then
        echo "✗ Bash syntax error: $sh"
        ERRORS=1
    fi
done

# 4. Python syntax check
for py in $(git diff --cached --name-only --diff-filter=ACM | grep '\.py$' || true); do
    if ! python3 -m py_compile "$py" 2>/dev/null; then
        echo "✗ Python syntax error: $py"
        ERRORS=1
    fi
done

if [[ "$ERRORS" -eq 1 ]]; then
    echo ""
    echo "❌ Pre-commit checks failed. Fix the issues above."
    exit 1
fi

echo "✅ All pre-commit checks passed"
