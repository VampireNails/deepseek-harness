#!/bin/bash
# Isolated plugin dependency installation and Linux regressions.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
while IFS= read -r -d '' file; do
  node --check "$file"
done < <(find intel-plugins -path '*/node_modules' -prune -o -type f -name '*.js' -print0)
while IFS= read -r -d '' dir; do
  if [ -f "$dir/package-lock.json" ]; then
    npm ci --prefix "$dir" --ignore-scripts
  elif [ -f "$dir/pnpm-lock.yaml" ]; then
    pnpm --dir "$dir" install --ignore-workspace --frozen-lockfile --ignore-scripts
  elif node -e 'const p=require(process.argv[1]); process.exit(Object.keys({...p.dependencies,...p.peerDependencies,...p.devDependencies}).length ? 1 : 0)' "$REPO_DIR/$dir/package.json"; then
    : # Dependency-free plugin.
  else
    echo "Missing dependency lockfile: $dir" >&2
    exit 1
  fi
  mapfile -d '' tests < <(find "$dir/tests" -type f -name '*.test.js' -print0)
  node --test "${tests[@]}"
done < <(find intel-plugins -mindepth 2 -maxdepth 2 -type d -name tests -printf '%h\0')
while IFS= read -r -d '' file; do bash -n "$file"; done < <(find intel -type f -name '*.sh' -print0)
node --test intel/task-runner/tests/*.test.mjs
