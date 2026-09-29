# Telegram Card Platform

## 推荐版本

普通云服务器部署唯一标准版：

```text
Cloud Deploy / 当前 main
```

作者本人专用增强版：

```text
owner-hybrid = Cloud Deploy + RTX5070 / Tailscale / Remote OCR
```

旧稳定版：

```text
v1.3.0-ocr-learning-plus
```

`v2.8.0-cloud-deploy` 保留为历史里程碑 Release，不代表后续最新代码。

`v1.3.0-ocr-learning-plus` 已归档，不再作为最新推荐部署版本。以后所有通用功能、Bug 修复、OCR 规则、管理员功能、学习流程、排序去重、广播通知、状态面板都必须先进入 Cloud Deploy。

## 版本定义

Cloud Deploy 永远代表：

- 当前最新功能。
- 当前所有 Bug 修复。
- 当前所有 OCR 规则优化。
- 当前所有管理员功能。
- 当前所有状态、广播、学习、去重、排序功能。
- 新服务器可直接完整部署。
- 不需要部署后手工补丁。

Cloud Deploy 默认不依赖：

- Windows RTX5070 OCR Worker。
- Tailscale。
- `REMOTE_OCR_URL` 本地地址。
- 本地 GPU 混合识别。
- Windows 本地 OCR 服务部署。

owner-hybrid 只是在 Cloud Deploy 基础上通过 `.env` 开启本地 GPU 增强：

```env
REMOTE_OCR_ENABLED=true
REMOTE_OCR_URL=http://100.81.208.104:8000
```

普通云服务器默认保持：

```env
REMOTE_OCR_ENABLED=false
REMOTE_OCR_URL=
```

## 当前功能

- Telegram 图片卡密识别。
- PUBG / PSN 图片级互斥分类。
- 任意 `S07xxxx` 前缀 PUBG 识别。
- PUBG 换行卡密按 OCR 行顺序、相邻行、坐标顺序拼接。
- 禁止从 PUBG 子串派生 PSN。
- OCR.space 云端 OCR 与 fallback。
- 可选 Remote OCR / RTX5070 Hybrid OCR。
- OpenCV 轻量预处理。
- 今日 OCR 缓存。
- OCR 学习、字体模板、纠错统计。
- 重复卡密提醒。
- 输出顺序保持图片顺序、图内从上到下、同行从左到右。
- 文本卡密消息默认不触发 OCR 回复。
- `/learn_cards` 和“学习卡密”人工真值学习。
- 双向私聊中继回复。
- 记账功能，支持设置汇率、设置费率、费率快照、手续费计算。
- 费率会从入款中扣除，再计算应下发人民币和应下发U；修改汇率或费率只影响后续新账单。
- 广播群组选择。
- 通知所有人。
- 状态面板。
- systemd 部署、备份、恢复。

## 目录结构

```text
bot.py                  # 启动入口与 handler 注册
config/                 # 配置、日志、常量
handlers/               # Telegram Update 处理层
services/               # OCR、记账、广播、转发、运行服务
storage/                # 数据库与仓储
utils/                  # 通用工具
tests/                  # 回归测试与 benchmark
scripts/                # 备份、部署、恢复脚本
systemd/                # systemd 服务模板
docs/                   # 文档
feature_backups/        # 历史备份
```

## 快速部署

```bash
export APP_DIR=/opt/telegram-card-platform
sudo git clone https://github.com/vvocao666/telegram-card-platform.git "$APP_DIR"
cd "$APP_DIR"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git tesseract-ocr
python3 -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

普通云服务器确认：

```env
REMOTE_OCR_ENABLED=false
REMOTE_OCR_URL=
```

启动 systemd：

```bash
sudo mkdir -p /etc/telegram-card-platform
printf 'APP_DIR=%s\nPYTHON_BIN=.venv/bin/python3\n' "$APP_DIR" | sudo tee /etc/telegram-card-platform/service.env
sudo cp systemd/telegram-card-platform.service /etc/systemd/system/telegram-card-platform.service
sudo systemctl daemon-reload
sudo systemctl enable telegram-card-platform
sudo systemctl start telegram-card-platform
sudo systemctl is-active telegram-card-platform
```

## 更新

```bash
cd /opt/telegram-card-platform
git pull --ff-only origin main
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python3 -m compileall -q bot.py config handlers services storage utils tests
sudo systemctl restart telegram-card-platform
```

## 日志

```bash
journalctl -u telegram-card-platform -f
journalctl -u telegram-card-platform --since "1 hour ago"
```

## 回滚

优先回滚到最近一次服务器备份目录。只有需要回到旧归档版本时才使用 tag。

普通云服务器不再把 v120 或 v1.3 当作最新稳定版；它们只是历史归档。

## 发布规则

小 Bug、OCR 规则修复、性能优化、文档修改、管理员功能修复：

```text
只 commit 并 push origin main
不创建 Tag
不创建 Release
```

只有 OCR 引擎更换、数据库结构变化、部署方式变化、重大架构升级、长期稳定版封版时，才创建正式 Release。
