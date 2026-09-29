#!/usr/bin/env bash
# ============================================================
#  TG 机器人管理面板 —— 一键部署到 Linux 服务器
#
#  用法（在项目目录里）：
#      sudo bash deploy.sh                  # 默认端口 8080
#      sudo bash deploy.sh --port 9000      # 换端口
#      sudo bash deploy.sh --no-service     # 只装依赖，不配开机自启
#      sudo bash deploy.sh --uninstall      # 卸载（停服务、删自启，不动数据）
#      sudo bash deploy.sh --name xxx       # 换服务名（同一台机器想再装一个，或演练）
#
#  干什么：
#      1. 检查 python3 版本（要 3.10 以上）
#      2. 建虚拟环境 .venv 并装 requirements.txt
#      3. 生成 config.json（监听 0.0.0.0，密码已设好）
#         已经有 config.json 的**不动密码**，只补缺的字段
#      4. 装成 systemd 服务 tgpanel，开机自启 + 崩了自动重启
#      5. 启动，并把面板地址和密码打出来
#
#  ★ 你的数据（bots.json / merchants.json / data/）不会被碰。
# ============================================================
set -euo pipefail

SERVICE="tgpanel"
HOST="0.0.0.0"      # ★ 服务器上必须监听所有网卡，不然外面访问不了
PORT="8080"
PORT_GIVEN=0        # ★ 只有显式传了 --port 才去覆盖 config.json 里的值
DO_SERVICE=1
DO_UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host)       HOST="${2:-0.0.0.0}"; shift 2 ;;
    --port)       PORT="${2:-8080}"; PORT_GIVEN=1; shift 2 ;;
    --name)       SERVICE="${2:-tgpanel}"; shift 2 ;;
    --no-service) DO_SERVICE=0; shift ;;
    --uninstall)  DO_UNINSTALL=1; shift ;;
    -h|--help)    sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "不认识的参数：$1（用 --help 看用法）"; exit 1 ;;
  esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
UNIT="/etc/systemd/system/$SERVICE.service"

# ★★ 服务用哪个用户跑。
#   **不能图省事直接用 root** —— 面板是对公网开放的网络服务，
#   拿 root 跑等于「面板被攻破 = 整台服务器沦陷」，而且它写出来的
#   文件（bots.json / 日志）都归 root，用户自己反而改不了。
#   sudo 进来的话 SUDO_USER 就是那个人（比如 ubuntu），用它。
RUN_USER="${SUDO_USER:-root}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "要用 root 跑：sudo bash deploy.sh"
fi

# ---------------- 卸载 ----------------
if [ "$DO_UNINSTALL" -eq 1 ]; then
  step "卸载 $SERVICE"
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload 2>/dev/null || true
  say "  服务已停、开机自启已删。"
  say "  ★ 数据没动：bots.json / merchants.json / data/ 都还在。"
  say "    要彻底删除就把整个目录删掉。"
  exit 0
fi

# ---------------- 0. 什么系统 ----------------
# ★ 这个脚本靠 systemd 装服务，所以**只支持 Linux**（Ubuntu / Debian /
#   CentOS / Rocky / AlmaLinux…都行，只要用 systemd）。
#   Windows 上不用它 —— 直接跑 TG客服多开版.exe 就行。
#   ★ 提示语要按发行版给命令：在 CentOS 上教人敲 apt 只会让人更懵。
step "检查系统"
if [ -f /etc/os-release ]; then
  OS_NAME="$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-$ID}")"
else
  OS_NAME="$(uname -s)"
fi
if command -v apt-get >/dev/null 2>&1; then
  PKG_HINT="apt install -y"
elif command -v dnf >/dev/null 2>&1; then
  PKG_HINT="dnf install -y"
elif command -v yum >/dev/null 2>&1; then
  PKG_HINT="yum install -y"
elif command -v apk >/dev/null 2>&1; then
  PKG_HINT="apk add"
else
  PKG_HINT=""
fi
say "  $OS_NAME"
if ! command -v systemctl >/dev/null 2>&1; then
  warn "这台机器没有 systemd —— 装不了开机自启的服务。"
  warn "用「bash deploy.sh --no-service」只装依赖，然后自己起："
  warn "    .venv/bin/python 主程序.py"
fi

# ---------------- 1. 检查 python ----------------
step "检查 Python"
command -v python3 >/dev/null 2>&1 || die "没装 python3。先装：${PKG_HINT:-装个} python3 python3-venv"
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || die "Python 版本太低（$PYV），要 3.10 以上"
say "  python3 $PYV ✓"

# ---------------- 2. 虚拟环境 + 依赖 ----------------
step "建虚拟环境并装依赖"
if [ ! -x "$PY" ]; then
  python3 -m venv "$VENV" 2>/dev/null || die \
"建虚拟环境失败。一般是缺 venv 模块。
      Ubuntu/Debian：apt install -y python3-venv
      CentOS/Rocky ：dnf install -y python3
  （本机对应的是：${PKG_HINT:-自己搜一下} python3-venv）
  装完再跑一次这个脚本。"
fi
say "  虚拟环境：$VENV"
"$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip \
    >/dev/null 2>&1 || true          # 升级 pip 失败不影响，别为它中断

# ★★ pip 源要能自动换。为什么（2026-09-29 在阿里云新加坡的机器上实测踩到）：
#   国内云（阿里云/腾讯云）的机器，pip 默认被配到**内网镜像源**
#   （mirrors.cloud.aliyuncs.com 之类），而那个源**只有国内机器连得上**。
#   海外实例会一直超时，**一个包都装不上** —— 表现是「卡在装依赖那一步，
#   半天没动静」，最后超时失败。换个新服务器（尤其海外的）就会撞上。
#   所以：默认源 → 官方 PyPI → 清华镜像，逐个试。
#   （`solo/install.sh` 早就这么干了，这里一直漏着。）
# ★ `--retries 2` 很关键：pip 默认对每个包重试 5 次、每次等 20 秒 ——
#   源不通的时候光「等它放弃」就要好几分钟。改成 2 次，几秒就能判定，
#   然后立刻换下一个源。
pip_try() {
  if [ -n "$1" ]; then
    "$PY" -m pip install --quiet --disable-pip-version-check \
        --timeout 20 --retries 2 -i "$1" -r "$APP_DIR/requirements.txt"
  else
    "$PY" -m pip install --quiet --disable-pip-version-check \
        --timeout 20 --retries 2 -r "$APP_DIR/requirements.txt"
  fi
}
# ★★ 先看默认源是不是**已知的内网地址**，是就直接跳过它。
#    为什么（2026-09-29 在阿里云新加坡的机器上实测）：
#      它**不是「失败」而是「挂着不走」** —— pip 会在那儿耗好几分钟，
#      把一台 2 核的小机器拖到 ssh 都开不了会话（`Timeout opening channel`）。
#      光靠 `--timeout` 治不了：那是**连上之后**读超时，连不上时它在等 TCP。
#    认识的这几个都是「只有国内机器连得上」的内网源：
#      mirrors.cloud.aliyuncs.com（阿里云内网）
#      mirrors.tencentyun.com   （腾讯云内网）
#    ★★ 末尾那个 `|| true` **不能省**：机器上没有任何 pip.conf 时 grep 返回 1，
#       而脚本开头是 `set -euo pipefail` —— 管道里任何一个失败都算整条失败，
#       于是 `VAR=$(...)` 这句会把**整个脚本静默杀掉**（什么错都不报，
#       就停在「虚拟环境：...」那一行）。2026-09-29 演练时实测踩到。
DEFAULT_IDX="$(grep -rhs 'index-url' /etc/pip.conf "$HOME/.pip/pip.conf" \
    "$HOME/.config/pip/pip.conf" 2>/dev/null | head -1 | cut -d= -f2- \
    | tr -d ' ' || true)"
case "$DEFAULT_IDX" in
  *cloud.aliyuncs.com*|*mirrors.tencentyun.com*)
    say "  默认 pip 源是内网地址（$DEFAULT_IDX），海外机器连不上，跳过"
    SKIP_DEFAULT=1 ;;
  *) SKIP_DEFAULT=0 ;;
esac

say "  装依赖…"
if [ "$SKIP_DEFAULT" -eq 1 ] || ! pip_try ""; then
  say "  默认的 pip 源连不上，换官方 PyPI 再试…"
  if ! pip_try "https://pypi.org/simple"; then
    say "  官方源也不行，换清华镜像再试…"
    pip_try "https://pypi.tuna.tsinghua.edu.cn/simple" || die \
"依赖装不上 —— 这台机器上不了外网？
    自己验一下：curl -sI https://pypi.org | head -1
    都不行的话配个能用的 pip 源再来。"
  fi
fi
say "  依赖装好了 ✓"

# ---------------- 3. config.json ----------------
step "写 config.json"
CONF="$APP_DIR/config.json"
# ★ 交给 python 写，保证 JSON 格式合法、也不用手拼引号
"$PY" - "$CONF" "$HOST" "$PORT" "$PORT_GIVEN" <<'PYEOF'
import json, os, secrets, string, sys

conf, host = sys.argv[1], sys.argv[2]
port, port_given = int(sys.argv[3]), sys.argv[4] == '1'
cfg = {}
if os.path.exists(conf):
    try:
        cfg = json.load(open(conf, encoding='utf-8')) or {}
    except ValueError:
        print('  ! 原来的 config.json 读不了，会重新生成一份')
        cfg = {}

# ★★ host 必须**强制**写成 0.0.0.0，不能 setdefault！
#    仓库里的 config.json 模板写的是 127.0.0.1（本机开发用），
#    用 setdefault 的话这个已存在的值不会被覆盖 ——
#    结果服务器上只监听本机，外面根本连不上，而且不报任何错。
#    （和 --port 那个是同一类坑，踩过。）
if cfg.get('host') not in (host, None) and cfg.get('host') != host:
    print('  原来 host=%s → 改成 %s（服务器上要监听所有网卡）'
          % (cfg.get('host'), host))
cfg['host'] = host
# ★ 显式传了 --port 就必须生效；没传才保留原来的（别把用户手改的端口冲掉）
if port_given or 'port' not in cfg:
    cfg['port'] = port
cfg.setdefault('welcome', '您好！请直接留言，我们会尽快回复您。')
# 密码：有就留着（别把人家原来设的覆盖掉），没有才生成
pw = cfg.get('password') or ''
if (not pw) or any(ord(c) > 127 for c in pw):
    pw = ''.join(secrets.choice(string.ascii_letters + string.digits)
                 for _ in range(12))
    cfg['password'] = pw
    print('  面板密码：%s' % pw)
else:
    print('  面板密码：（沿用 config.json 里原来的）%s' % pw)
# 签名密钥：跟密码分开，没有就生成
if not (cfg.get('token_secret') or '').strip():
    cfg['token_secret'] = ''.join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

json.dump(cfg, open(conf, 'w', encoding='utf-8'),
          ensure_ascii=False, indent=2)
print('  host=%s  port=%s' % (cfg['host'], cfg['port']))
PYEOF
say "  写好：$CONF"

# ---------------- 4. systemd ----------------
if [ "$DO_SERVICE" -eq 1 ]; then
  # ★ 上面那些步骤是 root 跑的，会把文件写成 root 所有 ——
  #   服务用普通用户跑就写不动了。统一改成 RUN_USER 的。
  if [ "$RUN_USER" != "root" ]; then
    chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR" 2>/dev/null || true
  fi
  step "装成 systemd 服务（用 $RUN_USER 跑）"
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=TG 机器人管理面板
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# ★ 用普通用户跑，别用 root（面板对公网开放，root 跑等于把整台机器押上去）
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
ExecStart=$PY $APP_DIR/主程序.py
Restart=always
RestartSec=5
# 崩了自动重启；日志用 journalctl -u $SERVICE -f 看
StandardOutput=append:$APP_DIR/运行日志.txt
StandardError=append:$APP_DIR/运行日志.txt

[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
  sleep 3
  say "  服务状态："
  systemctl --no-pager --lines=0 status "$SERVICE" 2>/dev/null | head -5 || true
else
  step "跳过 systemd（--no-service）"
  say "  手动启动：cd $APP_DIR && $PY 主程序.py"
fi

# ---------------- 5. 收尾 ----------------
step "搞定"
# ★ 优先显示公网 IP —— `hostname -I` 给的是云服务器的**内网**地址
#   （比如 10.x.x.x），拿去浏览器里打根本连不上，白折腾。
#   取不到公网就退回内网，并说明一下。
PUB_IP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null \
          || curl -fsS --max-time 6 https://ifconfig.me 2>/dev/null || true)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${PUB_IP:-$LAN_IP}"
say "  面板地址：http://${IP:-服务器IP}:$PORT"
say "  商户入口：http://${IP:-服务器IP}:$PORT/m"
if [ -z "$PUB_IP" ] && [ -n "$LAN_IP" ]; then
  warn "上面这个 $LAN_IP 是**内网**地址，从你电脑打不开。"
  warn "从云服务商后台找「外网IP」，用那个：http://外网IP:$PORT"
fi
say "  面板密码：见上面「写 config.json」那一段（也在 config.json 里）"
say ""
say "  看日志：journalctl -u $SERVICE -f     （或 tail -f 运行日志.txt）"
say "  重启：  systemctl restart $SERVICE"
say "  停止：  systemctl stop $SERVICE"
say ""
warn "上公网前记得：面板没有 CSRF 防护、密码走明文头，"
warn "一定要套 Nginx + HTTPS 反代，别把 $PORT 直接暴露出去。"
warn "防火墙只放行 80/443，$PORT 只监听内网。"
say ""
say "  还得去 @BotFather 关掉 Group Privacy（记账/群记录功能要用），"
say "  否则群里发 +100 机器人收不到。详见 README 第十节。"
