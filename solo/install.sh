#!/usr/bin/env bash
# ============================================================
#  记账机器人 · 独立版 —— 一键装到 Linux 服务器
#
#  用法（在本目录里）：
#      sudo bash install.sh
#      sudo bash install.sh --name mybot      # 换个服务名（同时跑两个才用得上）
#      sudo bash install.sh --uninstall       # 卸载（停服务、删自启，不动数据）
#
#  干什么：
#      1. 检查 python3（★ 要 3.10 以上，对不上会直接告诉你怎么装）
#      2. 建虚拟环境 .venv 并装依赖
#      3. 检查 config.json 填对了没有（没填会停下来告诉你填什么）
#      4. 装成 systemd 服务，开机自启 + 崩了自动重启
#
#  ★ 你的数据（data/ 目录）不会被碰。
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="tgledger"
PY="$APP_DIR/.venv/bin/python"

# ★★ 参数要**先全部读完再动手**：
#   以前是边读边做，`--uninstall --name X` 会在读到 --name 之前就退出，
#   结果卸的是默认名 tgledger，而不是 X（2026-09-29 真机测出来的）。
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) SERVICE="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "不认识的参数：$1"; exit 1 ;;
  esac
done

if [ "$UNINSTALL" = "1" ]; then
  echo "停止并删除服务 $SERVICE …"
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  echo "已卸载。你的 data/ 目录没动。"
  exit 0
fi

step(){ echo; echo "── $* ──"; }

if [ "$(id -u)" != "0" ]; then
  echo "请用 sudo 跑：sudo bash install.sh"
  exit 1
fi

step "① 检查 Python"
if ! command -v python3 >/dev/null 2>&1; then
  echo "  ✗ 这台机器没装 python3"
  echo "    Ubuntu/Debian：sudo apt install -y python3 python3-venv"
  echo "    CentOS/RHEL  ：sudo dnf install -y python3"
  exit 1
fi
# ★ 要 3.10 以上。为什么不是 3.9：
#   代码里用了 `str | None` 这种类型标注（虽然配了 from __future__ 不会求值），
#   但**我们只在 3.10 上完整验证过**（服务器就是 3.10，全部测试都在那上面跑）。
#   远程装机装到一半失败最费时间，所以宁可在这儿就拦住、给个明确的指引。
if ! python3 - <<'PY'
import sys
v = sys.version_info
if v < (3, 10):
    raise SystemExit(
        '  ✗ Python 版本太低：%s（要 3.10 以上）\n'
        '    办法二选一：\n'
        '      ① 换一台新一点的服务器（Ubuntu 22.04/24.04 自带 3.10/3.12，最省事）\n'
        '      ② 在这台上装个新 Python：\n'
        '         Ubuntu 20.04：sudo add-apt-repository ppa:deadsnakes/ppa \\\n'
        '                        && sudo apt update && sudo apt install -y python3.10 python3.10-venv\n'
        '         然后改成用 python3.10 建环境（把 scripts 里的 python3 换掉）'
        % sys.version.split()[0])
print('  Python %s ✓' % sys.version.split()[0])
PY
then
  exit 1
fi

step "② 建虚拟环境 + 装依赖"
if [ ! -x "$PY" ]; then
  # ★ 有些发行版要单独装 venv 包（Ubuntu 的 python3-venv）——
  #   远程装机时这是最常缺的东西，**能自动装就自动装**，省得来回问客户。
  if ! python3 -m venv "$APP_DIR/.venv" 2>/dev/null; then
    echo "  缺 venv 模块，试着自动装一下…"
    if command -v apt-get >/dev/null 2>&1; then
      apt-get install -y python3-venv >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y python3-virtualenv >/dev/null 2>&1 || true
    fi
    python3 -m venv "$APP_DIR/.venv" || {
      echo "  ✗ 还是建不了虚拟环境"
      echo "    Ubuntu/Debian：sudo apt install -y python3-venv"
      exit 1
    }
  fi
fi
# ★★ pip 源要能自动换。为什么：
#   国内云（阿里云/腾讯云）的机器，pip 默认被配到**内网镜像源**
#   （mirrors.cloud.aliyuncs.com 之类），而那个源**只有国内机器连得上**。
#   海外实例（新加坡/香港…）会一直超时，**一个包都装不上** ——
#   2026-09-29 在阿里云新加坡的机器上实测踩到。
#   所以：默认源 → 官方 PyPI → 清华镜像，逐个试。
pip_try() {
  local idx="$1"
  if [ -n "$idx" ]; then
    "$PY" -m pip install --quiet --disable-pip-version-check \
        --timeout 20 -i "$idx" -r "$APP_DIR/requirements.txt"
  else
    "$PY" -m pip install --quiet --disable-pip-version-check \
        --timeout 20 -r "$APP_DIR/requirements.txt"
  fi
}
"$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip \
    >/dev/null 2>&1 || true          # 升级 pip 失败不影响，别为它中断
echo "  装依赖…"
if ! pip_try ""; then
  echo "  默认的 pip 源连不上，换官方 PyPI 再试…"
  if ! pip_try "https://pypi.org/simple"; then
    echo "  官方源也不行，换清华镜像再试…"
    pip_try "https://pypi.tuna.tsinghua.edu.cn/simple" || {
      echo "  ✗ 依赖装不上 —— 这台机器上不了外网？"
      echo "    自己验一下：curl -sI https://pypi.org | head -1"
      echo "    都不行的话改用国内机器，或者配个能用的 pip 源"
      exit 1
    }
  fi
fi
echo "  依赖装好了 ✓"

step "③ 检查 config.json"
if [ ! -f "$APP_DIR/config.json" ]; then
  echo "  ✗ 没有 config.json"
  echo "    照 config.example.json 填一个，把服务商给你的 id / token /"
  echo "    回传地址 / 密钥填进去，然后重新跑这个脚本。"
  exit 1
fi
"$PY" "$APP_DIR/独立版.py" --check || {
  echo "  ✗ 配置有问题（看上面的提示）"
  exit 1
}

step "④ 装成系统服务"
# ★ 服务用调用者的身份跑，**不用 root** ——
#   网络程序被攻破 = 整台机器沦陷，而且写出来的文件归 root 用户自己改不了
RUN_USER="${SUDO_USER:-root}"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=记账机器人（独立版）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$PY $APP_DIR/独立版.py
Restart=always
RestartSec=5
# ★★ 这两行是**故意不写** StandardOutput/StandardError 的。
#    原来写成 append 到 运行日志.txt，而程序自己的 log() 也往那个文件写 ——
#    结果是**每一行都被写两遍**，文件涨得飞快（客户那台硬盘不大）。
#    不写就走 journal（journalctl -u <服务名> 能看），各写一份、互不重复。

[Install]
WantedBy=multi-user.target
EOF
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR" 2>/dev/null || true
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
sleep 4

step "⑤ 结果"
if systemctl is-active --quiet "$SERVICE"; then
  echo "  ✅ 跑起来了"
else
  echo "  ✗ 没起来，看日志：journalctl -u $SERVICE -n 40 --no-pager"
  exit 1
fi
echo
echo "  看状态： systemctl status $SERVICE"
echo "  看日志： tail -f $APP_DIR/运行日志.txt"
echo "  重启：   systemctl restart $SERVICE"
echo "  卸载：   sudo bash install.sh --uninstall"
echo
echo "  下一步：把机器人拉进群，然后用你自己的 Telegram"
echo "          私聊它发  /admin 绑定码（绑定码在日志里，或者问服务商）"
