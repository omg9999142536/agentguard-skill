#!/usr/bin/env python3
"""AgentGuard —— agent 付费 API 硬熔断代理 (MVP v0.1)
原理: OpenAI 兼容反向代理。agent 把 BASE_URL 指到本代理,代理转发请求到真上游,
记账(按 usage 折钱),超预算直接 429 断掉。零 SDK 侵入。
"""
import json, sqlite3, time, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request, urllib.error
from dashboard import DASHBOARD

# ============ 配置(后续挪到 agentguard.yaml) ============
UPSTREAM = os.environ.get("AG_UPSTREAM", "https://api.deepseek.com")
LISTEN   = int(os.environ.get("AG_PORT", "53110"))
LISTEN_HOST = os.environ.get("AG_LISTEN", "127.0.0.1")  # 默认仅本机; 需要被同机容器反代时设 172.17.0.1
# 预算: 人民币/天, 分层熔断(借鉴 AIHOT 生产配置)
BUDGET_DAY   = float(os.environ.get("AG_BUDGET_DAY", "10.0"))   # 每天 ¥10
BUDGET_HOUR  = float(os.environ.get("AG_BUDGET_HOUR", "3.0"))   # 每小时 ¥3
BUDGET_MIN   = float(os.environ.get("AG_BUDGET_MIN", "1.0"))    # 每分钟 ¥1
# 价目表(¥/1M tokens), 只列常用, 未知模型按 1.0 记
PRICES = {
    "deepseek-chat":  {"in": 2.0, "out": 8.0},
    "deepseek-reasoner": {"in": 4.0, "out": 16.0},
    "glm-4-flash":    {"in": 0.0, "out": 0.0},
    "glm-4.5-flash":  {"in": 0.0, "out": 0.0},
    "default":        {"in": 1.0, "out": 2.0},
}
# 环境变量覆盖价目: AG_PRICE_<MODEL带横线转下划线>=in价,例 AG_PRICE_GLM_4_FLASH=100
for _k, _v in os.environ.items():
    if _k.startswith("AG_PRICE_"):
        PRICES[_k[9:].lower().replace("_", "-")] = {"in": float(_v), "out": float(_v)}
DB_PATH = os.environ.get("AG_DB", os.path.expanduser("~/.agentguard/usage.db"))
# 预警: 达日预算 AG_WARN_PCT% 触发; AG_WEBHOOK_URL 收 JSON POST(每档只发一次/天)
WARN_PCT = int(os.environ.get("AG_WARN_PCT", "80"))
WEBHOOK_URL = os.environ.get("AG_WEBHOOK_URL", "")
_warn_fired = {}  # pct -> day_str

_lock = threading.Lock()

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS usage(
        ts INTEGER, model TEXT, tin INTEGER, tout INTEGER, cost REAL, ok INTEGER)""")
    c.commit()
    return c

def cost_of(model, tin, tout):
    p = PRICES.get(model, PRICES["default"])
    return (tin * p["in"] + tout * p["out"]) / 1e6

def spent(window_s):
    """最近 window_s 秒内的花费"""
    c = db()
    since = int(time.time() - window_s)
    r = c.execute("SELECT COALESCE(SUM(cost),0) FROM usage WHERE ts>?", (since,)).fetchone()
    c.close()
    return r[0]

def warn_level():
    """返回当前应触发的预警档(95/WARN_PCT/50), 无则 None; webhook 每档每天只发一次。"""
    d = spent(86400)
    if d <= 0 or BUDGET_DAY <= 0: return None
    pct = int(d / BUDGET_DAY * 100)
    level = None
    for lp in (95, WARN_PCT, 50):
        if pct >= lp: level = lp; break
    if level and WEBHOOK_URL:
        today = time.strftime("%Y-%m-%d")
        if _warn_fired.get(level) != today:
            _warn_fired[level] = today
            try:
                msg = json.dumps({"text": "[AgentGuard] 今日已花 ¥%.2f / ¥%.2f (%d%%), 触达 %d%% 预警线" % (d, BUDGET_DAY, pct, level)}).encode()
                urllib.request.urlopen(urllib.request.Request(WEBHOOK_URL, data=msg,
                    headers={"Content-Type": "application/json"}), timeout=5)
            except Exception:
                pass
    return level

def check_budget():
    m, h, d = spent(60), spent(3600), spent(86400)
    if m >= BUDGET_MIN:  return f"分钟预算熔断(¥{m:.2f}/{BUDGET_MIN})"
    if h >= BUDGET_HOUR: return f"小时预算熔断(¥{h:.2f}/{BUDGET_HOUR})"
    if d >= BUDGET_DAY:  return f"今日预算熔断(¥{d:.2f}/{BUDGET_DAY})"
    return None

def record(model, tin, tout, cost, ok):
    with _lock:
        c = db()
        c.execute("INSERT INTO usage VALUES(?,?,?,?,?,?)",
                  (int(time.time()), model, tin, tout, cost, ok))
        c.commit(); c.close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        # 1. 请求前进先查预算
        reason = check_budget()
        if reason:
            record("!", 0, 0, 0.0, 0)  # 熔断也入账(ok=0),模型记为!
            body = json.dumps({"error": {"message": f"[AgentGuard] {reason}",
                              "type": "agentguard_budget_exceeded"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        # 2. 读请求体,转发上游
        n = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(n) if n else b""
        model = "?"
        try: model = json.loads(req_body).get("model", "?")
        except Exception: pass
        # 路径改写: 客户端OpenAI习惯发 /v1/chat/completions;
        # 上游base已含版本路径(/v4等)时去掉 /v1 前缀再拼
        path = self.path
        if path.startswith("/v1/"):
            path = path[len("/v1"):]
        url = UPSTREAM.rstrip("/") + path
        req = urllib.request.Request(url, data=req_body, method="POST")
        for h in ("Authorization", "Content-Type", "Accept"):
            v = self.headers.get(h)
            if v: req.add_header(h, v)
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            out = resp.read()
            status = resp.status
        except urllib.error.HTTPError as e:
            out = e.read(); status = e.code
        except Exception as e:
            out = json.dumps({"error": {"message": str(e)}}).encode(); status = 502
        # 3. 记账(从 usage 字段)
        try:
            u = json.loads(out).get("usage") or {}
            tin, tout = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
            cost = cost_of(model, tin, tout)
            record(model, tin, tout, cost, 1 if status == 200 else 0)
        except Exception:
            record(model, 0, 0, 0.0, 0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        wl = warn_level()
        if wl:
            d = spent(86400)
            self.send_header("X-AgentGuard-Warning", "%d%% of daily budget (¥%.2f/¥%.2f)" % (wl, d, BUDGET_DAY))
        self.end_headers(); self.wfile.write(out)

    def do_GET(self):
        # / = 内嵌面板
        if self.path in ("/", "/index.html"):
            body = DASHBOARD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        # /_guard/status = 本地看板
        if self.path in ("/_guard/status", "/status", "/guard/status"):
            c = db()
            day = c.execute("SELECT model, COUNT(*), SUM(cost) FROM usage WHERE ts>? AND ok=1 GROUP BY model",
                            (int(time.time()-86400),)).fetchall()
            blocked = c.execute("SELECT COUNT(*), COALESCE(SUM(cost),0) FROM usage WHERE ok=0 AND ts>?",
                            (int(time.time()-86400),)).fetchone()
            c.close()
            body = json.dumps({"budget_day": BUDGET_DAY, "today_spent": round(spent(86400),4),
                               "today_calls": sum(n for _,n,_ in day),
                               "blocked_today": blocked[0],
                               "warning_level": warn_level(),
                               "per_model": [{"model":m,"calls":n,"cost":round(s or 0,4)} for m,n,s in day]},
                              ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        # /txt = 复制文字版面板（可贴进微信/终端）
        if self.path in ("/txt", "/_guard/status.txt", "/guard/txt", "/guard"):
            c = db()
            day = c.execute("SELECT model, COUNT(*), SUM(cost) FROM usage WHERE ts>? AND ok=1 GROUP BY model",
                            (int(time.time()-86400),)).fetchall()
            blocked = c.execute("SELECT COUNT(*) FROM usage WHERE ok=0 AND ts>?",
                            (int(time.time()-86400),)).fetchone()[0]
            c.close()
            spent_today = round(spent(86400), 4)
            lines = ["📊 AgentGuard 花费面板",
                     f"今日已花: ¥{spent_today}",
                     f"今日调用: {sum(n for _,n,_ in day)} 次",
                     f"日预算: ¥{BUDGET_DAY}",
                     f"熔断: {blocked} 次"]
            _wl = warn_level()
            if _wl:
                lines.append(f"⚠ 预警: 已达日预算 {_wl}%")
            if day:
                lines.append("分模型:")
                for m, n, s in day:
                    lines.append(f"{m}: {n}次 ¥{round(s or 0,4)}")
            else:
                lines.append("分模型: 暂无记录")
            body = "\n".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        self.send_response(404); self.end_headers()

if __name__ == "__main__":
    print(f"[AgentGuard] listening :{LISTEN} → {UPSTREAM}")
    print(f"[AgentGuard] budgets: ¥{BUDGET_MIN}/min  ¥{BUDGET_HOUR}/h  ¥{BUDGET_DAY}/day")
    print(f"[AgentGuard] status: http://127.0.0.1:{LISTEN}/_guard/status")
    print(f"[AgentGuard] listening {LISTEN_HOST}:{LISTEN}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN), Handler).serve_forever()
