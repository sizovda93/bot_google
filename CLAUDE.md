# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Telegram bot that automates payment receipt processing for a legal/bankruptcy firm. Managers send receipts (PDF/photos) to a TG group chat, the bot extracts debtor FIO + amount via LLM vision, finds the debtor in a Google Sheet, uploads the receipt to Yandex Disk, and updates the spreadsheet.

## Commands

```bash
# Run locally
python -m bot.main

# Run with venv (first time)
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python -m bot.main

# Deploy on server (systemd)
systemctl restart receipt-bot
systemctl status receipt-bot
journalctl -u receipt-bot -f          # tail logs
```

The bot runs as a long-polling Telegram process — no web server, no webhooks.

## Architecture

```
TG Group Chat → aiogram handlers → LLM (caption + receipt parsing)
                                         ↓
                              Google Sheets (fuzzy FIO match)
                                         ↓
                              Yandex Disk (upload + public link)
                                         ↓
                              Sheet update (amount, debt, link)
                                         ↓
                              TG reply with confirmation
```

**Processing flow** lives in `handlers.py`:
- `_process_receipt()` — downloads file, calls LLM twice (caption + receipt), validates data
- `_finish_multi()` — uploads to YaDisk, updates sheet for each client, sends confirmation
- `handle_reply_with_partner()` — catches replies when partner wasn't determined automatically

**Two LLM calls per receipt:**
1. `parse_caption()` — extracts client FIO(s) and partner name from TG message caption
2. `parse_receipt()` — extracts debtor FIO, amount, date from the receipt file itself

Caption FIO takes priority; receipt FIO is fallback. Partner always comes from Google Sheet lookup (caption partner is only a disambiguation hint for fuzzy matching).

## Key Design Decisions

- **OpenAI-compatible API** via custom proxy (`aspbllm.online`), model `gpt-5.4-mini`. Not official OpenAI — if proxy changes, update `OPENAI_BASE_URL` in `.env`.
- **Fuzzy matching** (thefuzz, threshold 75%) because caption may say "Иванов И.А." while sheet has "Иванов Иван Анатольевич". Uses three strategies: ratio, partial_ratio, token_sort_ratio.
- **Partner from sheet, not from manager** — the Google Sheet is the source of truth for client-partner mapping. Caption partner is only used to disambiguate when multiple clients have the same FIO.
- **Multi-client receipts** — one receipt can cover multiple clients. Amount is split equally. All clients get the same YaDisk link.
- **Pending receipts** — when partner can't be determined (client not in sheet, no caption partner), bot asks via TG reply. State stored in `pending_receipts` dict (in-memory, lost on restart).

## Google Sheet Structure

Sheet ID: `14bZiqurDD9_tMJ6OiScWf4-TL2JQ2Wo6MeXyc_ti8KA`, GID: `1701314176`

| Col | Index | Field |
|-----|-------|-------|
| B | 1 | ФИО (debtor name) |
| D | 3 | Партнер |
| F | 5 | Обязательные план (required amount) |
| G | 6 | Обязательные факт (paid so far) |
| H | 7 | Долг (debt = plan - fact) |
| I | 8 | Чек об оплате (YaDisk link) |

Money format in sheet: `р.21 000`. Bot reads, parses, and writes in this format.

## Yandex Disk Structure

Base path: `/Публикации Чеки /Чеки за публикации/` (note trailing space after "Чеки"). Partner subfolders already exist — bot creates them only if missing.

## Deployment

Production server: `72.56.237.53:2222` (SSH), systemd service `receipt-bot`, venv at `/root/receipt-bot/.venv/`. No Docker (rate-limited on this server).

## Caveats

- Python 3.9 compatibility required on Mac (use `Optional[X]` not `X | None`, `Tuple` from typing not `tuple[...]`). Server has 3.12.
- `pending_receipts` is in-memory — pending partner-reply state is lost if bot restarts.
- Google Sheets API has rate limits (~60 reads/min). Heavy batch processing may hit this.
