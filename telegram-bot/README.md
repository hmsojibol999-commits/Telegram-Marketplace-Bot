# Telegram Marketplace Bot

এই botটি Telegram Bot API polling ব্যবহার করে এবং SQLite-এ persistent data রাখে।

## Required secrets

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_ID`

Optional:

- `MARKETPLACE_DB_PATH` — database file path, default `telegram_marketplace.sqlite3`

## Run

```bash
python3 telegram-bot/main.py
```

## Financial safety model

- Balance-এর প্রতিটি পরিবর্তন `ledger` row তৈরি করে।
- Purchase এক transaction-এ balance, stock, owner payout, এবং order commit করে।
- Withdrawal request available balance থেকে reserve করে; reject হলে ledger refund হয়।
- Deposit ও withdrawal status pending/approved/rejected হওয়ায় double processing আটকানো হয়।
- Admin authorization server-side `ADMIN_USER_ID` দিয়ে করা হয়।