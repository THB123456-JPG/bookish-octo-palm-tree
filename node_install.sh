#!/usr/bin/env bash
set -euo pipefail
umask 077
if [ "$(id -u)" -ne 0 ]; then
  echo '请使用 sudo bash node_install.sh'; exit 1
fi
NODE_SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NODE_ROOT=/opt/tgpanel-node
if [ -e "$NODE_ROOT" ] || [ -L "$NODE_ROOT" ] || [ -e /etc/systemd/system/tgpanel-node.service ]; then
  echo '已存在节点目录或服务，停止安装，避免覆盖现有机器人。'; exit 1
fi
command -v apt-get >/dev/null || { echo '此安装脚本适用于 Ubuntu / Debian。'; exit 1; }
apt-get update
apt-get install -y python3-venv python3-pip openssl
id tgpanel-node >/dev/null 2>&1 || useradd --system --home-dir /var/lib/tgpanel-node --create-home --shell /usr/sbin/nologin tgpanel-node
install -d -m 700 "$NODE_ROOT"
python3 - "$NODE_SOURCE" "$NODE_ROOT" <<'PY'
import json,secrets,shutil,sys
from pathlib import Path
source,target=map(Path,sys.argv[1:])
sys.path.insert(0,str(source))
from tools.source_release import node_sources
names=node_sources(source)
for name in names:
    src=source/name;dst=target/name
    if not src.is_file() or src.is_symlink():raise ValueError('安装包文件缺失')
    dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
for name in ('data','codes'):(target/name).mkdir()
(target/'node.json').write_text(json.dumps(dict(id=secrets.token_hex(16),key=secrets.token_urlsafe(48),host='0.0.0.0',port=9443)),encoding='utf-8')
(target/'config.json').write_text('{}',encoding='utf-8')
(target/'bots.json').write_text('{"bots":[]}',encoding='utf-8')
PY
python3 -m venv "$NODE_ROOT/.venv"
"$NODE_ROOT/.venv/bin/python" -m pip install -r "$NODE_ROOT/requirements.txt"
openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
  -keyout "$NODE_ROOT/node-key.pem" -out "$NODE_ROOT/node-cert.pem" -subj '/CN=tgpanel-node'
chown -R tgpanel-node:tgpanel-node "$NODE_ROOT"
cat > /etc/systemd/system/tgpanel-node.service <<'UNIT'
[Unit]
Description=TGPanel execution node
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=tgpanel-node
Group=tgpanel-node
WorkingDirectory=/opt/tgpanel-node
ExecStart=/opt/tgpanel-node/.venv/bin/python -u /opt/tgpanel-node/node_worker.py
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/tgpanel-node
[Install]
WantedBy=multi-user.target
UNIT
chmod 644 /etc/systemd/system/tgpanel-node.service
systemctl daemon-reload
systemctl enable --now tgpanel-node
systemctl is-active --quiet tgpanel-node
python3 - "$NODE_ROOT" <<'PY'
import hashlib,json,ssl,sys
from pathlib import Path
root=Path(sys.argv[1]);config=json.loads((root/'node.json').read_text())
fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'node-cert.pem').read_text())).hexdigest()
print('安装完成。在主面板填写以下信息：')
print('地址：https://新服务器IP:9443')
print('连接密钥：'+config['key'])
print('证书指纹：'+fingerprint)
print('请在云安全组/防火墙仅允许主面板服务器 IP 访问 TCP 9443。')
PY
