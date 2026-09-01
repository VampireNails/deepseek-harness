#!/bin/bash
# 批量单包构建：为 exports 指向 lib/*.js 的包生成 tsdown 打包产物
set -u
ROOT="/d/tmp/deepseek-harness/my-deepseek-harness/deepseek-harness"
cd "$ROOT"
export NODE_OPTIONS=

build_one() {
  local pkg_dir="$1"
  local rel="${pkg_dir#$ROOT/}"
  local name
  name=$(python -c "import json;print(json.load(open('$pkg_dir/package.json',encoding='utf-8')).get('name',''))" 2>/dev/null)
  [ -z "$name" ] && return
  case "$name" in
    @deepseek-ai/dsh-*) ;;
    *) return ;;
  esac
  [ "$name" = "@deepseek-ai/dsh" ] && return  # apps/cli 已构建
  # 检查 exports 是否指向 lib/*.js（非 types）
  local needs
  needs=$(python -c "
import json,sys
d=json.load(open('$pkg_dir/package.json',encoding='utf-8'))
for sub,t in d.get('exports',{}).items():
    if sub in ('./src/*','./package.json'): continue
    de=t.get('default','') if isinstance(t,dict) else ''
    if de.startswith('./lib/') and not de.startswith('./lib/types/'):
        print('yes'); break
")
  [ "$needs" = "yes" ] || return
  # entry
  local entries=""
  for f in index.js invariant.js startup.js; do
    if [ -f "$pkg_dir/lib/types/$f" ]; then entries="$entries lib/types/$f"; fi
  done
  [ -n "$entries" ] || { echo "SKIP_NO_ENTRY: $rel"; return; }
  (cd "$pkg_dir" && node "$ROOT/node_modules/.pnpm/tsdown@0.22.2_oxc-resolver@_f113eb69000457c8d5954142d0822f52/node_modules/tsdown/dist/run.mjs" $entries -d lib --format esm --platform node --no-clean --no-config >/tmp/tsdown_one.log 2>&1)
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo "FAIL: $rel"
    tail -3 /tmp/tsdown_one.log | sed 's/^/    /'
    return
  fi
  # 复制 .mjs -> .js
  local copied=0
  for f in index invariant startup; do
    if [ -f "$pkg_dir/lib/$f.mjs" ]; then
      cp "$pkg_dir/lib/$f.mjs" "$pkg_dir/lib/$f.js" 2>/dev/null && copied=$((copied+1))
    fi
  done
  echo "OK ($copied): $rel"
}

find "$ROOT/packages" -name package.json -not -path "*/node_modules/*" -not -path "*/lib/*" | while read -r pj; do
  build_one "$(dirname "$pj")"
done
echo "DONE"
