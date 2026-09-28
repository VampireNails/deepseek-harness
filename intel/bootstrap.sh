#!/bin/bash
# dsh-muse bootstrap: 从零重建 web-intel / evolve / hmc-test 三个 profile
# 用法: DSH_HOME=~/.dsh bash intel/bootstrap.sh
# 注意: 不含任何 token/证书私钥；hmc-test 的 token/cert 需另行配置
set -e
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"

echo "=== dsh-muse bootstrap ==="
echo "REPO: $REPO_ROOT"
echo "DSH_HOME: $DSH_HOME"

# 1. 检查 dsh 版本
DSH_VER=$(dsh --version 2>/dev/null || echo "unknown")
echo "dsh 版本: $DSH_VER"
if ! grep -q "0.1.7-rc.1" <<< "$DSH_VER"; then
  echo "警告: 期望 dsh 0.1.7-rc.1，当前 $DSH_VER"
fi

# 2. 定义 profile 配置
declare -A PROFILE_BUNDLES
PROFILE_BUNDLES[web-intel]='@deepseek-ai/dsh-base @deepseek-ai/dsh-web-app hello-intel dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile'
PROFILE_BUNDLES[evolve]='@deepseek-ai/dsh-base @deepseek-ai/dsh-headless dsh-intelligence-memory dsh-intelligence-joblog dsh-intelligence-evolution dsh-intelligence-feed dsh-intelligence-profile'
PROFILE_BUNDLES[hmc-test]='@deepseek-ai/dsh-base @deepseek-ai/dsh-web-app dsh-intelligence-memory dsh-intelligence-quiet dsh-intelligence-feed dsh-intelligence-profile hmc-service-wrapper'

declare -A PROFILE_DEPS
PROFILE_DEPS[web-intel]='dsh-intelligence-feed dsh-intelligence-memory dsh-intelligence-profile dsh-intelligence-quiet hello-intel'
PROFILE_DEPS[evolve]='dsh-intelligence-evolution dsh-intelligence-feed dsh-intelligence-joblog dsh-intelligence-memory dsh-intelligence-profile'
PROFILE_DEPS[hmc-test]='dsh-intelligence-feed dsh-intelligence-memory dsh-intelligence-profile dsh-intelligence-quiet hmc-service-wrapper'

# 3. 为每个 profile 生成 package.json 并安装
for profile in web-intel evolve hmc-test; do
  echo ""
  echo "--- 配置 $profile ---"
  PDIR="$DSH_HOME/profiles/$profile"
  mkdir -p "$PDIR"
  
  # 生成 bundles JSON 数组
  BUNDLES_JSON=$(printf '"%s",' ${PROFILE_BUNDLES[$profile]} | sed 's/,$//')
  
  # 生成 dependencies（树外插件用 file: 指向仓库内 intel-plugins）
  DEPS_JSON=""
  for dep in ${PROFILE_DEPS[$profile]}; do
    if [[ "$dep" == dsh-intelligence-* ]]; then
      DEPS_JSON="$DEPS_JSON\"$dep\": \"file:$REPO_ROOT/intel-plugins/$dep\","
    elif [[ "$dep" == "hello-intel" ]]; then
      DEPS_JSON="$DEPS_JSON\"$dep\": \"file:$REPO_ROOT/intel-plugins/hello-intel\","
    elif [[ "$dep" == "hmc-service-wrapper" ]]; then
      DEPS_JSON="$DEPS_JSON\"$dep\": \"file:$REPO_ROOT/intel/hmc-service-wrapper\","
    fi
  done
  DEPS_JSON=$(echo "$DEPS_JSON" | sed 's/,$//')
  
  cat > "$PDIR/package.json" << EOF
{
  "name": "$profile",
  "private": true,
  "dsh": {
    "profile": {
      "bundles": [$BUNDLES_JSON]
    }
  },
  "dependencies": {
    $DEPS_JSON,
    "@deepseek-ai/dsh-base": "0.1.7-rc.1",
    "@deepseek-ai/dsh-web-app": "0.1.7-rc.1",
    "@deepseek-ai/dsh-headless": "0.1.7-rc.1"
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
# overrides, disables, and insert lists; `!!js` expressions allowed).
[]
YEOF
  
  echo "  安装插件..."
  (cd "$PDIR" && npm ci --silent 2>&1 | tail -2)
  echo "  ✓ $profile 就绪"
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
