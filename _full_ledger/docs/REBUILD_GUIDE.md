# Rebuild Guide

Use this guide when moving to a new server or creating a new bot from selected functions.

## Current Reference

Latest organized local snapshot: migrated working tree for `vvocao666/pubg-psn-`.

Primary repository:

```text
https://github.com/vvocao666/pubg-psn-.git
```

## New Server Restore

For restoring the full current bot:

```bash
git clone https://github.com/vvocao666/pubg-psn-.git /root/s07-bot
cd /root/s07-bot
bash scripts/bootstrap_server.sh
```

Then edit:

```text
/root/s07-bot/.env
```

Required values depend on enabled functions:

- `BOT_TOKEN`
- `OWNER_CHAT_ID`
- `OCR_SPACE_API_KEY`
- `AUDIT_BOT_TOKEN`
- `AUDIT_CHAT_ID`

Restart:

```bash
systemctl restart s07-bot
systemctl is-active s07-bot
```

## Creating A New Bot From Feature Packs

Tell Codex using this format:

```text
新建一个机器人项目，项目名：<name>
需要功能包：
1. <feature_backups folder>
2. <feature_backups folder>
不要其它功能。
主机器人 token：<token or leave env placeholder>
副机器人 token/chat_id：<if needed>
部署服务器：<ip or later>
```

Example:

```text
新建一个机器人项目，项目名：card-ledger-bot
需要功能包：
1. feature_backups/02-card-ocr-audit-forward
2. feature_backups/03-ledger
3. feature_backups/04-okx-price
不要 TRX 能量，不要防篡改图片。
先创建项目结构，token 留 .env 配置。
```

## Feature Pack Selection

- Card OCR only: `feature_backups/01-card-ocr-only`
- Card OCR plus audit forwarding: `feature_backups/02-card-ocr-audit-forward`
- Ledger: `feature_backups/03-ledger`
- OKX price query: `feature_backups/04-okx-price`
- TRC20 anti-tamper image: `feature_backups/05-trc20-anti-tamper`

## Recommended Clean Project Structure

For future projects, rebuild into this structure:

```text
bot.py
features/
  card_ocr/
  audit_forward/
  ledger/
  okx_price/
  trc20_verify/
tests/
.env.example
requirements.txt
README.md
```

Keep `bot.py` small. It should only load config, initialize storage, register handlers, and start polling.

## Notes

- Do not copy `outputs/`, `.env`, `.venv`, or `__pycache__`.
- Keep feature backup READMEs as the source of truth for each function.
- If the server changes, copy database files only when historical ledger/OCR learning data must be preserved.
- For small servers, keep `OCR_CONCURRENCY=1` and enable `s07-bot-backup.timer` plus the `s07-bot.service` resource limits.
