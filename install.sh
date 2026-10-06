#!/bin/sh
# AgentGuard 一键安装 (curl -fsSL <url>/agentguard-install.sh | sh)
set -e
REPO_RAW="${AG_REPO_RAW:-https://3d28.com/downloads}"
DIR="$HOME/.agentguard"
PORT="${AG_PORT:-53110}"

echo "▶ AgentGuard 安装中..."
mkdir -p "$DIR"
# 下载主程序
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$REPO_RAW/agentguard.py" -o "$DIR/agentguard.py"
  curl -fsSL "$REPO_RAW/dashboard.py"  -o "$DIR/dashboard.py"
else
  wget -q "$REPO_RAW/agentguard.py" -O "$DIR/agentguard.py"
  wget -q "$REPO_RAW/dashboard.py"  -O "$DIR/dashboard.py"
fi

# 默认配置(环境变量文件)
[ -f "$DIR/env" ] || cat > "$DIR/env" <<EOF
AG_UPSTREAM=https://open.bigmodel.cn/api/paas/v4
AG_PORT=$PORT
AG_BUDGET_MIN=0.2
AG_BUDGET_HOUR=0.5
AG_BUDGET_DAY=2
# 预警: 花到日预算的百分之多少时告警(响应头+/txt 面板); 可选 webhook 推送
AG_WARN_PCT=80
# AG_WEBHOOK_URL=https://your-webhook
# 价目覆盖示例: AG_PRICE_GLM_4_FLASH=0
EOF

# systemd 用户服务(Linux) / launchd 不在MVP,mac 提示手动跑
if command -v systemctl >/dev/null 2>&1 && [ -n "$HOME" ]; then
  UNIT="$HOME/.config/systemd/user/agentguard.service"
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=AgentGuard - budget circuit breaker for AI agents
After=network.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$DIR/env
ExecStart=/usr/bin/env python3 $DIR/agentguard.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now agentguard 2>/dev/null || true
  STARTED=1
  # systemd不可用(无用户总线/HOME被改)时兜底: 后台直启
  if ! curl -fsS "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
    (cd "$DIR" && nohup sh -c 'set -a; . ./env; set +a; python3 agentguard.py' >/dev/null 2>&1 &)
  fi
else
  STARTED=0
  (cd "$DIR" && nohup sh -c 'set -a; . ./env; set +a; python3 agentguard.py' >/dev/null 2>&1 &)
fi

sleep 2
if curl -fsS "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; then
  OK=1
else
  OK=0
fi

echo ""
echo "══════════════════════════════════════════"
echo "  AgentGuard 已安装"
echo "══════════════════════════════════════════"
if [ "$OK" = "1" ]; then
  echo ""
  # 直接打印文字账单面板(闭环最简: 装完即见账单)
  sleep 1
  curl -fsS "http://127.0.0.1:$PORT/txt" 2>/dev/null || echo "(账单稍后可用: curl 127.0.0.1:$PORT/txt)"
  echo ""
  echo "──────────────────────────────────────────"
  echo "  接入: API Base 改成 http://127.0.0.1:$PORT/v1"
  echo "  网页面板: http://127.0.0.1:$PORT/"
  echo "  再看账单: curl 127.0.0.1:$PORT/txt"
else
  echo "  启动: cd $DIR && sh -c 'set -a; . $DIR/env; set +a; python3 agentguard.py'"
  echo "  ⚠ 面板未响应,请手动启动看上方提示"
fi
echo "══════════════════════════════════════════"

echo ""
echo "📤 转给朋友一行安装:"
echo "   curl -fsSL https://3d28.com/downloads/agentguard-install.sh | sh"
echo "   网页: https://3d28.com/downloads/agentguard.html"
