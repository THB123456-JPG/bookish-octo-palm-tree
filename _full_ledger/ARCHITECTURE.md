# Architecture Plan

Baseline version: `strict-v120-owner-broadcast-no-trx`

This document is an architecture plan only. It does not move code, change imports, or alter bot behavior.

## Current Files

Core runtime:

- `bot.py`: Telegram application setup, OCR flow, card parsing, image processing, audit forwarding, ledger handler glue, price query, TRC20 verification image, owner broadcast, rate limits, cleanup jobs.
- `ledger_commands.py`: ledger command parsing, bill formatting, permission checks for ledger operations.
- `ledger_storage.py`: SQLite storage for ledger entries, chat owners, users, recognized cards, OCR corrections, bot group records.
- `requirements.txt`: Python dependencies.
- `.env.example`: environment variable template.
- `.gitignore`: runtime and secret exclusions.

Existing structure:

- `config/`: settings and logging helpers.
- `handlers/`: placeholder/thin handler boundary files.
- `services/`: placeholder/thin service boundary files for OCR, ledger, broadcast, forward, price, TRC20.
- `storage/`: storage boundary files and repository placeholders.
- `utils/`: small utility placeholders.
- `tests/`: current regression tests.
- `docs/`: feature and rebuild documentation.
- `scripts/`: server bootstrap and backup scripts.
- `systemd/`: service and backup timer templates.
- `feature_backups/`: historical reusable function snapshots.

## Current Functions

- PUBG card OCR from Telegram photos.
- PSN card OCR from Telegram photos.
- Forwarded image and batch image recognition.
- OCR.space primary OCR.
- Local Tesseract fallback/complement OCR.
- OCR image compression, resizing, enhancement, crop and rotation variants.
- Strict card extraction, correction, de-duplication, and uncertain-result handling.
- OCR correction learning from reply messages.
- OCR text correction persistence.
- Recognized-card history and duplicate reminders.
- Optional secondary audit bot forwarding with source metadata.
- Owner/private reply behavior controls.
- Ledger/accounting commands for RMB income and USDT payout.
- Ledger bill summaries for today, yesterday, and full history.
- Ledger clear, pause/open, daily cutover, exchange-rate settings, operator display.
- Group owner permission model for sensitive ledger operations.
- Bot group recording for broadcast target selection.
- Owner-only broadcast flow.
- OKX USDT/CNY price query.
- USDT-TRC20 address verification image generation.
- `/start`, `/id`, `/version`, help text, add-group keyboards.
- Background cleanup for runtime files.
- Photo rate limiting, with owner bypass.
- Systemd deployment and data backup scripts.

## Function Ownership

| Current Area | Current Location | Suggested Target |
|---|---|---|
| Bot startup and handler registration | `bot.py` | `bot.py` |
| `/start`, `/id`, `/version`, menus | `bot.py` | `handlers/start_handler.py` |
| Card OCR Telegram handlers | `bot.py` | `handlers/card_ocr_handler.py` |
| Card parsing and formatting | `bot.py` | `services/ocr/card_parser.py`, `utils/text_utils.py` |
| OCR.space integration | `bot.py` | `services/ocr/ocrspace_provider.py` |
| Local OCR preprocessing | `bot.py` | `utils/image_utils.py`, `services/ocr/` |
| OCR provider selection | `bot.py` | `services/ocr/base.py` |
| OCR correction learning | `bot.py`, `ledger_storage.py` | `services/ocr/`, `storage/repositories/card_repository.py` |
| Ledger command handling | `bot.py`, `ledger_commands.py` | `handlers/ledger_handler.py`, `services/ledger/ledger_service.py` |
| Ledger reports | `ledger_commands.py` | `services/ledger/report_service.py` |
| Ledger SQLite persistence | `ledger_storage.py` | `storage/database.py`, `storage/repositories/ledger_repository.py` |
| User/group persistence | `ledger_storage.py` | `storage/repositories/user_repository.py` |
| Broadcast flow | `bot.py` | `handlers/broadcast_handler.py`, `services/broadcast/broadcast_service.py` |
| Audit forwarding | `bot.py` | `services/forward/forward_service.py` |
| OKX price query | `bot.py` | `services/price/price_service.py` |
| TRC20 address verification image | `bot.py` | keep planned under `services/price/` only if needed later, otherwise new `services/trc20/` |
| Permission helpers | `bot.py`, `ledger_commands.py` | `utils/permission_utils.py` |
| Telegram formatting/splitting | `bot.py` | `utils/telegram_utils.py` |
| Settings and logging | `bot.py` | `config/settings.py`, `config/logging_config.py` |

## New Module Layout

```text
bot.py
config/
  settings.py
  logging_config.py
handlers/
  start_handler.py
  card_ocr_handler.py
  ledger_handler.py
  broadcast_handler.py
  admin_handler.py
services/
  ocr/
    base.py
    ocrspace_provider.py
    card_parser.py
  ledger/
    ledger_service.py
    report_service.py
  broadcast/
    broadcast_service.py
  forward/
    forward_service.py
  price/
    price_service.py
storage/
  database.py
  repositories/
    card_repository.py
    ledger_repository.py
    user_repository.py
utils/
  image_utils.py
  text_utils.py
  telegram_utils.py
  permission_utils.py
tests/
docs/
systemd/
scripts/
```

## Migration Risks

- `bot.py` contains many shared globals; moving code too early can change state sharing.
- Telegram handlers depend on registration order, especially ledger text handling and broadcast text interception.
- OCR behavior is sensitive to small formatting, correction, and merge-order changes.
- Tests cover many parsing cases but not every live Telegram callback path.
- Storage schema is centralized in `ledger_storage.py`; splitting repositories must preserve table creation and migrations exactly.
- Audit forwarding and source replies share the same recognition batch results.
- Broadcast relies on group records written by unrelated message/new-member flows.
- Unicode text in the existing code has encoding damage in places; moving it mechanically may preserve behavior better than rewriting it.

## Dependencies

- `bot.py` depends on `ledger_commands.py`, `ledger_storage.py`, `httpx`, `Pillow`, `pytesseract`, `python-dotenv`, and `python-telegram-bot`.
- `ledger_commands.py` depends on `LedgerStore` methods and SQLite-backed data models.
- OCR parsing depends on image preprocessing, OCR.space responses, local OCR output, correction storage, and duplicate history storage.
- Broadcast depends on `LedgerStore.list_active_bot_groups()`.
- Price query depends on OKX public HTTP endpoints through `httpx`.
- Deployment depends on `systemd/s07-bot.service`, `scripts/bootstrap_server.sh`, and `.env`.

## Expected Split Order

1. Freeze baseline with backup and tests.
2. Create empty target modules and import-safe wrappers.
3. Move pure helper functions first: text, Telegram formatting, permissions.
4. Move image helper functions without changing call sites.
5. Move OCR parsing and provider functions behind thin service wrappers.
6. Move ledger Telegram handler glue while keeping `ledger_commands.py` stable.
7. Move broadcast handler and service.
8. Move audit forwarding service.
9. Move price service.
10. Reduce `bot.py` to startup and handler registration only.
11. Run full tests after every phase.
