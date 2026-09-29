# OCR Correction Engine

The OCR correction engine is prepared for gray release on the production OCR flow. It keeps strict safety boundaries and does not use Qwen or PaddleOCR.

## Purpose

The engine generates correction candidates for repeated OCR mistakes, scores them, validates card formats, and returns only safe candidates.

## Pipeline

```text
OCR
-> Candidate Generator
-> Correction Engine
-> Learning Engine
-> Scoring Engine
-> Validator
-> Best Candidate
```

## Lightweight OCR Pipeline

The current low-memory production direction is:

```text
Original image
-> Pillow lightweight preprocessing
-> original / gray / binary / upscaled / roi variants
-> OCR.space
-> merge candidates
-> CandidateGenerator
-> FontTemplate
-> Validator
-> Scoring
-> final result
-> outputs/today_ocr_cache.json
```

Qwen and PaddleOCR are intentionally not used for the 2-core 1GB server profile.

Preprocessing module:

```text
services/ocr/image_preprocess.py
```

Variants:

- `original`
- `gray`
- `binary`
- `upscaled`
- `roi`

Debug images are written to:

```text
outputs/preprocess/
```

If preprocessing or ROI detection fails, the original image is returned.

## Safe Correction Boundary

Normal clear fonts are protected:

- valid card format
- high OCR confidence
- no candidate conflict
- no special font template hit

In this case, correction and learning are skipped.

Special font correction requires:

- template match score `>= 95`
- image quality score `>= 70`
- template enabled
- corrected candidate passes Validator
- rule count `>= 3` to participate in scoring
- rule count `>= 10` plus matching font hash for high-weight automatic correction

Blurred or uncertain images degrade safely:

- no forced correction
- no learning rule creation
- mark `needs_review`
- return the most credible OCR result for owner review

## Similar Duplicate Detection

Duplicate detection now uses:

```text
raw OCR
-> normalize
-> Validator
-> canonical_card
-> duplicate check
```

Same-day same-source cards with Hamming distance `<= 1` and similarity `>= 95%` produce an owner warning only. They are not deleted automatically.

## Default Rules

- `O -> 0`
- `I -> 1`
- `L -> 1`
- `S -> 5`
- `B -> 8`
- `Z -> 2`
- `RN -> M`

## Safety Rules

- Legal card formats are never force-changed.
- Invalid text is not forced into a card.
- Candidates must pass PUBG or PSN validation.
- The PUBG prefix must be `S07`.
- PUBG candidates use `S07XXX-XXXX-XXXX-XXXXX`.
- PSN candidates use `XXXX-XXXX-XXXX`.
- The production OCR flow is unchanged.

## Candidate Audit

The isolated audit module can write raw OCR candidate diagnostics to:

```text
outputs/ocr_candidates.json
```

Each record contains:

- `ocr_raw`
- `candidate_list`
- `validator_reject_reason`
- `best_score`
- `best_candidate`

This file is not connected to production until the command handlers or OCR flow explicitly call the audit module.

## Font Learning

The isolated font learning modules are:

- `services/ocr/font_fingerprint.py`
- `services/ocr/font_profile.py`
- `services/ocr/font_repository.py`
- `services/ocr/font_learning.py`
- `services/ocr/font_scoring.py`

The repository stores profiles in:

```text
outputs/font_profiles.json
```

Each profile contains:

- `font_hash`
- `card_type`
- `source_chat_id`
- `source_user_id`
- `sample_count`
- `error_pairs`
- `position_rules`
- `confidence`
- `last_seen`
- `enabled`

Font fingerprints use image features:

- character height
- character width
- line spacing
- stroke thickness
- grayscale distribution
- white background / black text ratio
- crop region ratio

Correction priority:

```text
Font-specific rules
-> Source-user rules
-> Card-type rules
-> Generic rules
```

Font scoring:

- font match: `+40`
- historical learned rule: `+30`
- position match: `+20`
- valid card format: `+20`
- single character change: `+10`
- invalid format: `-100`
- global untrusted replacement: `-50`

## OCR Report

The report module writes aggregate metrics to:

```text
outputs/ocr_report.json
```

Fields:

- `total_images`
- `total_cards`
- `fixed_count`
- `false_negative_count`
- `character_confusion_count`
- `font_profile_hits`
- `font_profile_misses`
- `top_error_pairs`
- `precision`
- `recall`
- `f1`

## Font Template Library

The font template library is stored in:

```text
outputs/font_templates.json
```

Template modules:

- `services/ocr/font_templates.py`
- `services/ocr/template_matcher.py`
- `services/ocr/template_learning.py`

Template fields:

- `name`
- `font_hash`
- `card_type`
- `sample_count`
- `errors`
- `positions`
- `confidence`
- `enabled`

Default template example:

```json
{
  "PUBG_FONT_A": {
    "font_hash": "3f9ab2",
    "card_type": "PUBG",
    "sample_count": 218,
    "confidence": 99.3,
    "enabled": true,
    "errors": {
      "2": "Z",
      "8": "B",
      "M": "N",
      "Q": "O"
    },
    "positions": {
      "19:2": "Z"
    }
  }
}
```

Template matching:

- `match_template(font_hash)` returns the template name only when similarity is greater than `95%`.
- Disabled templates are ignored.
- Position rules are applied before generic error rules.
- Generic error rules are not applied to already valid card strings unless position rules match, preventing over-correction.

Template learning:

- `/ocr_template_learn` service mode accepts image fingerprint plus manual correct result.
- It records font hash, wrong character, correct character, position, and count.
- After `100` samples, a reusable template is generated automatically.

## Daily Ground Truth Learning

Daily manual shipment cards are treated as Ground Truth and have higher priority than OCR output.

The learning entry point is:

```text
services/ocr/daily_learning.py::learn_today()
```

OCR cache lookup order:

1. `outputs/today_ocr_cache.json`
2. `outputs/ocr_report.json`
3. `outputs/ocr_candidates.json`
4. `today_ocr_cache`
5. `memory.today_results`

Manual text extraction ignores vendor names, platform names, prices, and notes. Only valid PUBG or PSN cards are learned.

Duplicate learning rule:

- Learning key is `(font_hash, wrong, correct, position)`.
- If the same key already exists, count is not increased.
- Only `last_seen` is updated.
- The same wrong/correct/position can be learned independently under a different font hash.

## Daily OCR Cache

Every recognized image appends OCR output before Telegram replies are sent:

```text
outputs/today_ocr_cache.json
```

Format:

```json
{
  "date": "2026-06-21",
  "images": 60,
  "ocr_cards": [
    "S07304-XXXX-XXXX-XXXXX"
  ],
  "raw_candidates": [],
  "time": "2026-06-21 21:15:00"
}
```

Rules:

- Same-day cache appends and deduplicates cards.
- Cross-day cache resets automatically.
- Missing cache sets `ocr_cache_found=false`; it never treats all human cards as missed OCR.
- `/ocr_cache_today` shows date, image count, OCR count, first 10 cards, and cache path.

## Debug Commands

Service-layer command helpers are available in:

```text
services/ocr/debug_commands.py
```

Supported command names:

- `/ocr_debug`
- `/ocr_candidates`
- `/ocr_font_stats`
- `/ocr_fonts`
- `/ocr_font_rules`
- `/ocr_font_disable`
- `/ocr_font_enable`
- `/ocr_template_learn`
- `/ocr_template_stats`
- `/ocr_template_list`
- `/ocr_template_disable`
- `/ocr_template_enable`
- `/learn_cards`
- `/learn_confirm`
- `/learn_cancel`
- `/ocr_learning_stats`

## Telegram Ground Truth Learning

Owner-only learning commands:

- `/learn_cards` accepts the daily manually confirmed cards.
- Owner plain text with at least 5 valid PUBG/PSN cards automatically opens the same confirmation flow.
- `/learn_confirm` writes the learning results.
- `/learn_cancel` discards the pending learning batch.
- `/ocr_learning_stats` shows cumulative samples, rules, top confusions, missing cards, and template accuracy.

Safety rules:

- Only `OWNER_CHAT_ID` can start or confirm learning.
- Human cards are parsed as Ground Truth.
- Chinese names, platform labels, prices, and notes are ignored.
- Learning requires `outputs/today_ocr_cache.json`.
- If today's OCR cache is missing, learning is blocked and no missing-card counts are created.
- Repeated rules are deduplicated by `(font_hash, wrong, correct, position)` and only update `last_seen`.
- Auto-detected learning text never writes immediately; `/learn_confirm` is required.
- Non-owner messages never trigger learning.

They are not registered in Telegram yet because this feature branch does not modify `bot.py` or handlers.

## Manual Rules

Future admin commands can read and write rules through `storage/repositories/correction_repository.py`.

## Rollback

This feature is isolated. To roll back before integration, remove the files added under:

- `services/ocr/candidate_generator.py`
- `services/ocr/correction_engine.py`
- `services/ocr/correction_rules.py`
- `services/ocr/learning_engine.py`
- `services/ocr/scoring_engine.py`
- `services/ocr/validator.py`
- `services/ocr/candidate_audit.py`
- `services/ocr/debug_commands.py`
- `services/ocr/font_fingerprint.py`
- `services/ocr/font_learning.py`
- `services/ocr/font_profile.py`
- `services/ocr/font_repository.py`
- `services/ocr/font_scoring.py`
- `services/ocr/ocr_report.py`
- `services/ocr/font_templates.py`
- `services/ocr/template_matcher.py`
- `services/ocr/template_learning.py`
- `storage/repositories/correction_repository.py`
- `tests/test_correction_engine.py`
- `tests/test_candidate_generator.py`
- `tests/test_scoring_engine.py`
- `tests/test_validator.py`
- `tests/test_candidate_audit.py`
- `tests/test_font_learning.py`
- `tests/test_font_learning_system.py`
- `tests/test_font_fingerprint.py`
- `tests/test_font_scoring.py`
- `tests/test_ocr_debug_commands.py`
- `tests/test_ocr_report.py`
- `tests/test_font_templates.py`
