# Changelog

## telegram-card-platform-modular

- Refactored the stable v120 bot into the Telegram Card Platform framework structure.
- Reduced `bot.py` to application setup and handler registration.
- Moved ledger command logic into `services/ledger/ledger_commands.py`.
- Moved SQLite storage logic into `storage/repositories/ledger_storage.py`.
- Added service modules for OCR, PUBG parsing, PSN parsing, correction, history, broadcast, forward, ledger, reports, and price.
- Deleted Phase 3 `current_*snapshot.py` files after the new modules took over.
- Kept v120 rollback files in `feature_backups/v120_stable/`.
- Preserved all tested behavior from `strict-v120-owner-broadcast-no-trx`.

## strict-v120-owner-broadcast-no-trx

- Stable baseline for the Telegram Card Platform framework.
- Keeps the current online bot behavior unchanged.
- Includes card OCR, audit forwarding, ledger, price query, owner broadcast, TRC20 verification image, deployment scripts, and systemd templates.
- Phase 3 and Phase 4 added copy-only module snapshots and handler adapters without switching runtime entry points.
