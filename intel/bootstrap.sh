#!/bin/bash
# dsh-muse bootstrap: 从零重建 web-intel / evolve / hmc-test 三个 profile
#
# 用法（真正从零验收）:
#   DSH_HOME=$(mktemp -d) bash intel/bootstrap.sh
# 用法（重建到默认位置）:
#   DSH_HOME=~/.dsh bash intel/bootstrap.sh
#
# 注意: 不含任何 token/证书私钥；hmc-test 的 token/cert 需另行配置（见文末）。
#
# 修订历史:
#   2026-09-28: 修两处硬伤——
#     (1) hmc-service-wrapper 已入库为 intel/hmc-service-wrapper。
#         旧脚本引用的 intel/hmc-service-wrapper 在仓库中不存在，hmc-test profile
#         是三个 profile 里恰恰无法被重建的那一个。
#     (2) npm ci 改为 npm install。全新目录没有 package-lock.json，npm ci 必然
#         EUSAGE 失败；旧脚本的 "| tail -2" 还把失败吞掉，set -e 也拦不住，
#         导致"已验证"的结论实际跑在带旧状态的目录里。
#   另: 依赖改用 file:（npm 拷贝目录；线上实际为 pnpm link: symlink，npm 不支持 link: 协议）；
#       不再把 @deepseek-ai/* 写进 dependencies（dsh 自行解析 bundles，
#       线上 profile 的 dependencies 里也没有它们）。
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"

echo "=== dsh-muse bootstrap ==="
echo "REPO: $REPO_ROOT"
echo "DSH_HOME: $DSH_HOME"

# 0. 前置检查：wrapper 必须在仓库里存在，否则 hmc-test 无法重建（硬伤 A 的回归门）
if [[ ! -f "$REPO_ROOT/intel/hmc-service-wrapper/package.json" ]]; then
  echo "错误: $REPO_ROOT/intel/hmc-service-wrapper 不存在，hmc-test 无法重建" >&2
  exit 1
fi

# 1. 检查 dsh 版本
DSH_VER=$(dsh --version 2>/dev/null || echo "unknown")
echo "dsh 版本: $DSH_VER"
if ! grep -q "0.1.7-rc.1" <<< "$DSH_VER"; then
  echo "警告: 期望 dsh 0.1.7-rc.1，当前 $DSH_VER"
fi

# 2. 定义 profile 配置（bundles 必须与线上一致）
declare -A PROFILE_BUNDLES
PROFILE_BUNDLES[web-intel]='@deepseek-ai/dsh-base @deepseek-ai/dsh-web-app hello-intel dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile'
PROFILE_BUNDLES[evolve]='@deepseek-ai/dsh-base @deepseek-ai/dsh-headless dsh-intelligence-memory dsh-intelligence-joblog dsh-intelligence-evolution dsh-intelligence-feed dsh-intelligence-profile'
PROFILE_BUNDLES[hmc-test]='@deepseek-ai/dsh-base @deepseek-ai/dsh-web-app dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile hmc-service-wrapper'

# 树外插件 → 仓库内路径（file: 由 npm 拷贝，离线可装）
declare -A PLUGIN_SRC
PLUGIN_SRC[hello-intel]="$REPO_ROOT/intel-plugins/hello-intel"
for p in dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile dsh-intelligence-joblog dsh-intelligence-evolution; do
  PLUGIN_SRC[$p]="$REPO_ROOT/intel-plugins/$p"
done
PLUGIN_SRC[hmc-service-wrapper]="$REPO_ROOT/intel/hmc-service-wrapper"

declare -A PROFILE_PLUGINS
PROFILE_PLUGINS[web-intel]='hello-intel dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile'
PROFILE_PLUGINS[evolve]='dsh-intelligence-memory dsh-intelligence-joblog dsh-intelligence-evolution dsh-intelligence-feed dsh-intelligence-profile'
PROFILE_PLUGINS[hmc-test]='dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile hmc-service-wrapper'

# 3. 为每个 profile 生成 package.json 并安装
for profile in web-intel evolve hmc-test; do
  echo ""
  echo "--- 配置 $profile ---"
  PDIR="$DSH_HOME/profiles/$profile"
  mkdir -p "$PDIR"

  # bundles JSON 数组
  BUNDLES_JSON=$(printf '"%s",' ${PROFILE_BUNDLES[$profile]} | sed 's/,$//')

  # dependencies: file: 指向仓库内源码（npm 拷贝到 node_modules）
  DEPS_JSON=""
  for dep in ${PROFILE_PLUGINS[$profile]}; do
    src="${PLUGIN_SRC[$dep]}"
    if [[ ! -f "$src/package.json" ]]; then
      echo "错误: 插件源码 $src 不存在" >&2
      exit 1
    fi
    DEPS_JSON="$DEPS_JSON    \"$dep\": \"file:$src\","
  done
  DEPS_JSON=$(echo "$DEPS_JSON" | sed 's/,$//')

  cat > "$PDIR/package.json" << EOF
{
  "name": "dsh-profile-$profile",
  "private": true,
  "dsh": {
    "profile": {
      "bundles": [$BUNDLES_JSON]
    }
  },
  "dependencies": {
$DEPS_JSON
  }
}
EOF

  # cordis.yml (空，由 patch 层组装)
  cat > "$PDIR/cordis.yml" << 'YEOF'
# dsh profile root — an empty entry list. The tree is composed as patches:
# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any
# --patch overlays. Edit cordis.patch.yml, not this file.
[]
YEOF

  # cordis.patch.yml (空)
  cat > "$PDIR/cordis.patch.yml" << 'YEOF'
# Your patch layer for this dsh profile, applied after every bundle layer:
# a top-level YAML array of loader patch entries (id-targeted config
# overrides, disables, and insert lists; `!!js` expressions allowed.
[]
YEOF

  echo "  安装插件 (npm install)..."
  # 注意: 故意不用 npm ci——全新目录没有 package-lock.json，npm ci 必然失败。
  # 不吞 exit code: pipefail 已开，失败直接 abort，不再"假装就绪"。
  (cd "$PDIR" && npm install --no-audit --no-fund --loglevel=error)

  echo "  校验安装结果..."
  for dep in ${PROFILE_PLUGINS[$profile]}; do
    if [[ ! -f "$PDIR/node_modules/$dep/package.json" ]]; then
      echo "错误: $PDIR/node_modules/$dep 安装异常（无 package.json）" >&2
      exit 1
    fi
  done

  echo "  链接到 dsh..."
  (cd "$PDIR" && DSH_HOME="$DSH_HOME" dsh plugin --profile "$profile" install)
  echo "  ✓ $profile 就绪"
done

echo ""
echo "=== 验收: 三个 profile 的 bundles 可被 dsh 解析 ==="
for profile in web-intel evolve hmc-test; do
  n=$(python3 -c "import json;print(len(json.load(open('$DSH_HOME/profiles/$profile/package.json'))['dsh']['profile']['bundles']))")
  echo "  $profile: $n bundles, package.json + node_modules 就绪"
done

echo ""
echo "=== 端口与启动说明 ==="
echo "web-intel: dsh --profile web-intel --port 3080  (mock: DEEPSEEK_BASE_URL=http://127.0.0.1:18099/v1)"
echo "evolve:    dsh --profile evolve  (headless, 由 cron 触发)"
echo "hmc-test:  dsh --profile hmc-test --port 3095   (HMC TLS 43197)"
echo ""
echo "=== hmc-test 额外配置（需手动）==="
echo "1. 生成 token: openssl rand -base64 32 > /root/intel/hmc-test/token.txt"
echo "2. 生成证书: openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=hmc-test' -addext 'subjectAltName=IP:100.73.148.102,IP:127.0.0.1'"
echo "3. 写 hmc-config.json: {\"tokenFile\":..., \"certFile\":..., \"keyFile\":..., \"port\":43197, \"host\":\"100.73.148.102\"}"
echo "4. 手机导入配对文件（含 token + certificate）"
echo ""
echo "=== bootstrap 完成 ==="
