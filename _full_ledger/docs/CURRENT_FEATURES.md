# Current Bot Feature Inventory

Snapshot: migrated working tree for `vvocao666/pubg-psn-`.

This document is the reference for rebuilding the current bot or creating a new bot from selected functions.

## 1. Card OCR

Files:

- `bot.py`
- `ledger_storage.py` for duplicate and correction persistence
- `tests/test_bot.py`

Current behavior:

- Recognizes PUBG card codes.
- Recognizes PSN card codes.
- Handles forwarded images and image batches.
- Uses strict card length rules.
- Ignores images without PUBG/PSN card information.
- Formats card output for easy copy.
- Reports duplicate cards from the same day.
- Shows first-seen time and source for duplicates.
- Learns OCR corrections when the user replies with the correct card.
- Keeps learned corrections persistently, not only for the current day.
- Applies learned corrections across private chats and groups.
- Supports fuzzy/conflict output when OCR is uncertain.
- Owner images bypass the normal image rate limit and are processed first.

Reusable packs:

- `feature_backups/01-card-ocr-only`
- `feature_backups/02-card-ocr-audit-forward`

## 2. Secondary Audit Forwarding

Files:

- `bot.py`
- `services/forward/forward_service.py`

Current behavior:

- Main bot replies normally in private chats and groups.
- Recognition results can be sent to a secondary receiver bot.
- Audit message includes private/group source and sender metadata.
- Owner usage can be excluded from audit forwarding when configured.
- Photo forwarding falls back to text when photo upload fails.

Config:

- `AUDIT_BOT_TOKEN`
- `AUDIT_CHAT_ID`
- `OWNER_CHAT_ID`

Reusable pack:

- `feature_backups/02-card-ocr-audit-forward`

## 3. Ledger

Files:

- `ledger_commands.py`
- `ledger_storage.py`
- `services/ledger/ledger_service.py`
- `services/ledger/report_service.py`
- `bot.py` handler glue

Current behavior:

- Income is RMB.
- Payout is USDT.
- Exchange rate converts income RMB into expected payout USDT.
- Supports income, payout, bill, today bill, yesterday bill, full bill, clear, pause/open, daily cutover, and rate settings.
- The user who invited the bot to a group controls sensitive actions.
- Ledger buttons include yesterday, today, full bill, and usage help.
- Group metadata is recorded so the owner can select groups for broadcast.

Reusable pack:

- `feature_backups/03-ledger`

## 4. OKX Price Query

Files:

- `bot.py`
- `services/price/price_service.py`

Current behavior:

- Commands include `币价`, `bj`, `BJ`, `z0`, and `Z0`.
- Fetches OKX USDT/CNY latest C2C sell prices.
- Falls back to OKX USD/CNY exchange rate when needed.

Reusable pack:

- `feature_backups/04-okx-price`

## 5. TRC20 Verification

Files:

- `bot.py`
- `services/trc20/verify_service.py`

Current behavior:

- Detects USDT-TRC20 address text.
- Generates a verification image.
- Adds generation time.
- Sends the address text with the generated image.
- `services/trc20/verify_service.py` also provides a lightweight TRON address format checker for future chain verification.

Reusable pack:

- `feature_backups/05-trc20-anti-tamper`

## 6. Owner Broadcast

Files:

- `bot.py`
- `services/broadcast/broadcast_service.py`

Current behavior:

- Owner can private-message `广播` to start the broadcast flow.
- Bot lists recorded groups.
- Owner selects target groups with inline buttons.
- Owner sends the broadcast text.
- Bot sends the message to selected groups and reports success/failure counts.
- Non-owner users cannot use this flow.

## 7. Server Hygiene

Files:

- `bot.py`
- `scripts/backup_data.sh`
- `scripts/bootstrap_server.sh`
- `systemd/s07-bot-backup.service`
- `systemd/s07-bot-backup.timer`
- `systemd/s07-bot.service`

Current behavior:

- Server cleanup removes old temporary OCR/image records.
- OCR is protected by queue/concurrency controls, per-chat and per-user image rate limits, and OCR.space 429 cooldown.
- Systemd service runs the bot with CPU, memory, task, and stop-time limits so SSH/system services keep resources.
- Daily local data backups archive `outputs/` into `/root/s07-bot/backups/data` and keep recent backups.
- Bootstrap script prepares a new Debian/Ubuntu server.

## Backup Policy

- Full restore backups are kept locally under `backups/` and are ignored by Git.
- Reusable function packs are kept under `feature_backups/`.
- Do not commit `.env`, database files, logs, runtime outputs, OCR images, virtual environments, or cache folders.
