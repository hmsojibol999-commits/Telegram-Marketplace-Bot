from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'bn',
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    reference_type TEXT,
    reference_id INTEGER,
    idempotency_key TEXT UNIQUE,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category_id, name)
);

CREATE TABLE IF NOT EXISTS inventory_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    option_id INTEGER NOT NULL REFERENCES options(id),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
    secret_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'sold')),
    order_id INTEGER,
    sold_at TEXT
);

CREATE TABLE IF NOT EXISTS market_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_user_id INTEGER NOT NULL REFERENCES users(id),
    option_id INTEGER NOT NULL REFERENCES options(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    total_cents INTEGER NOT NULL CHECK (total_cents > 0),
    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'cancelled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES market_orders(id),
    inventory_item_id INTEGER NOT NULL UNIQUE REFERENCES inventory_items(id),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0)
);

CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    method_key TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    txid TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    method_key TEXT NOT NULL DEFAULT 'bkash',
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    payout_details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS user_states (
    telegram_user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self._migrate_existing_database()
        self.connection.commit()

    def _migrate_existing_database(self) -> None:
        self._add_column_if_missing("users", "language", "TEXT NOT NULL DEFAULT 'bn'")
        self._add_column_if_missing("withdrawals", "method_key", "TEXT NOT NULL DEFAULT 'bkash'")
        self.connection.execute(
            "INSERT OR IGNORE INTO sqlite_sequence (name, seq) VALUES ('market_orders', 1000)"
        )

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in columns}:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def ensure_user(self, telegram_user_id: int, username: str | None, first_name: str) -> sqlite3.Row:
        self.connection.execute(
            """
            INSERT INTO users (telegram_user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET username=excluded.username,
              first_name=excluded.first_name
            """,
            (telegram_user_id, username, first_name),
        )
        self.connection.commit()
        return self.get_user(telegram_user_id)

    def get_user(self, telegram_user_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)
        ).fetchone()
        if row is None:
            raise ValueError("User is not registered.")
        return row

    def find_user(self, identifier: str) -> sqlite3.Row | None:
        clean = identifier.strip().lstrip("@")
        if clean.isdigit():
            return self.connection.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (int(clean),)
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM users WHERE lower(username)=lower(?)", (clean,)
        ).fetchone()

    def set_language(self, telegram_user_id: int, language: str) -> None:
        if language not in ("bn", "en"):
            raise ValueError("Unsupported language.")
        self.connection.execute(
            "UPDATE users SET language=? WHERE telegram_user_id=?",
            (language, telegram_user_id),
        )
        self.connection.commit()

    def set_state(self, telegram_user_id: int, state: str, data_json: str = "{}") -> None:
        self.connection.execute(
            """
            INSERT INTO user_states (telegram_user_id, state, data_json)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET state=excluded.state,
              data_json=excluded.data_json, updated_at=CURRENT_TIMESTAMP
            """,
            (telegram_user_id, state, data_json),
        )
        self.connection.commit()

    def get_state(self, telegram_user_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM user_states WHERE telegram_user_id=?", (telegram_user_id,)
        ).fetchone()

    def clear_state(self, telegram_user_id: int) -> None:
        self.connection.execute("DELETE FROM user_states WHERE telegram_user_id=?", (telegram_user_id,))
        self.connection.commit()

    def seed_defaults(self) -> None:
        self.connection.executemany(
            """
            INSERT INTO payment_methods (method_key, display_name, details)
            VALUES (?, ?, ?)
            ON CONFLICT(method_key) DO NOTHING
            """,
            [
                ("bkash", "bKash", "Admin has not configured bKash details yet."),
                ("nagad", "Nagad", "Admin has not configured Nagad details yet."),
                ("binance", "Binance", "Admin has not configured Binance details yet."),
            ],
        )
        self.connection.commit()

    def ledger_entry(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: int,
        amount_cents: int,
        entry_type: str,
        idempotency_key: str,
        reference_type: str | None = None,
        reference_id: int | None = None,
        note: str = "",
    ) -> None:
        user = connection.execute("SELECT balance_cents FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise ValueError("Ledger user does not exist.")
        new_balance = user["balance_cents"] + amount_cents
        if new_balance < 0:
            raise ValueError("Insufficient available balance.")
        connection.execute(
            """
            INSERT INTO ledger (
              user_id, amount_cents, balance_after_cents, entry_type,
              reference_type, reference_id, idempotency_key, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                amount_cents,
                new_balance,
                entry_type,
                reference_type,
                reference_id,
                idempotency_key,
                note,
            ),
        )
        connection.execute("UPDATE users SET balance_cents=? WHERE id=?", (new_balance, user_id))

    def create_deposit(self, telegram_user_id: int, method_key: str, amount_cents: int, txid: str) -> sqlite3.Row:
        with self.transaction() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO deposits (user_id, method_key, amount_cents, txid)
                    VALUES ((SELECT id FROM users WHERE telegram_user_id=?), ?, ?, ?)
                    """,
                    (telegram_user_id, method_key, amount_cents, txid),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("This TXID has already been submitted.") from error
            return connection.execute("SELECT * FROM deposits WHERE id=?", (cursor.lastrowid,)).fetchone()

    def approve_deposit(self, deposit_id: int) -> sqlite3.Row:
        with self.transaction() as connection:
            deposit = connection.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
            if deposit is None:
                raise ValueError("Deposit not found.")
            if deposit["status"] != "pending":
                raise ValueError("This deposit has already been processed.")
            self.ledger_entry(
                connection,
                user_id=deposit["user_id"],
                amount_cents=deposit["amount_cents"],
                entry_type="deposit_approved",
                reference_type="deposit",
                reference_id=deposit_id,
                idempotency_key=f"deposit-approved:{deposit_id}",
                note=f"{deposit['method_key']} / {deposit['txid']}",
            )
            connection.execute(
                "UPDATE deposits SET status='approved', processed_at=CURRENT_TIMESTAMP WHERE id=?",
                (deposit_id,),
            )
            return connection.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()

    def reject_deposit(self, deposit_id: int) -> sqlite3.Row:
        with self.transaction() as connection:
            deposit = connection.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
            if deposit is None:
                raise ValueError("Deposit not found.")
            if deposit["status"] != "pending":
                raise ValueError("This deposit has already been processed.")
            connection.execute(
                "UPDATE deposits SET status='rejected', processed_at=CURRENT_TIMESTAMP WHERE id=?",
                (deposit_id,),
            )
            return connection.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()

    def adjust_balance(self, telegram_user_id: int, amount_cents: int, note: str) -> sqlite3.Row:
        if amount_cents == 0:
            raise ValueError("Balance adjustment cannot be zero.")
        with self.transaction() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)
            ).fetchone()
            if user is None:
                raise ValueError("User not found. The user must send /start first.")
            self.ledger_entry(
                connection,
                user_id=user["id"],
                amount_cents=amount_cents,
                entry_type="admin_adjustment",
                idempotency_key=f"admin-adjustment:{telegram_user_id}:{uuid.uuid4().hex}",
                note=note,
            )
            return connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()

    def create_category(self, name: str) -> sqlite3.Row:
        with self.transaction() as connection:
            try:
                cursor = connection.execute("INSERT INTO categories (name) VALUES (?)", (name.strip(),))
            except sqlite3.IntegrityError as error:
                raise ValueError("That category already exists.") from error
            return connection.execute("SELECT * FROM categories WHERE id=?", (cursor.lastrowid,)).fetchone()

    def create_option(self, category_id: int, name: str) -> sqlite3.Row:
        with self.transaction() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO options (category_id, name) VALUES (?, ?)", (category_id, name.strip())
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("That option already exists in this category.") from error
            return connection.execute("SELECT * FROM options WHERE id=?", (cursor.lastrowid,)).fetchone()

    def add_inventory_batch(
        self,
        option_id: int,
        owner_telegram_user_id: int,
        price_cents: int,
        account_lines: list[str],
    ) -> sqlite3.Row:
        clean_lines = [line.strip() for line in account_lines if line.strip()]
        if not clean_lines:
            raise ValueError("At least one non-empty account line is required.")
        with self.transaction() as connection:
            owner = connection.execute(
                "SELECT id FROM users WHERE telegram_user_id=?", (owner_telegram_user_id,)
            ).fetchone()
            if owner is None:
                raise ValueError("Product owner must start the bot first.")
            cursor = connection.execute(
                """
                INSERT INTO inventory_batches (option_id, owner_user_id, price_cents, item_count)
                VALUES (?, ?, ?, ?)
                """,
                (option_id, owner["id"], price_cents, len(clean_lines)),
            )
            batch_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO inventory_items (batch_id, secret_text) VALUES (?, ?)",
                [(batch_id, line) for line in clean_lines],
            )
            return connection.execute(
                "SELECT * FROM inventory_batches WHERE id=?", (batch_id,)
            ).fetchone()

    def option(self, option_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT o.*, c.name category_name,
              COALESCE(SUM(CASE WHEN i.status='available' THEN 1 ELSE 0 END), 0) available_stock
            FROM options o
            JOIN categories c ON c.id=o.category_id
            LEFT JOIN inventory_batches b ON b.option_id=o.id
            LEFT JOIN inventory_items i ON i.batch_id=b.id
            WHERE o.id=?
            GROUP BY o.id
            """,
            (option_id,),
        ).fetchone()

    def quote_purchase(self, option_id: int, quantity: int, admin_telegram_user_id: int) -> dict[str, int]:
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        rows = self.connection.execute(
            """
            SELECT i.id, b.price_cents, b.owner_user_id
            FROM inventory_items i
            JOIN inventory_batches b ON b.id=i.batch_id
            JOIN users owner ON owner.id=b.owner_user_id
            WHERE b.option_id=? AND i.status='available'
            ORDER BY CASE WHEN owner.telegram_user_id=? THEN 0 ELSE 1 END,
              b.created_at, b.id, i.id
            LIMIT ?
            """,
            (option_id, admin_telegram_user_id, quantity),
        ).fetchall()
        if len(rows) < quantity:
            option = self.option(option_id)
            available = option["available_stock"] if option else 0
            raise ValueError(f"Available stock: {available}.")
        return {"quantity": quantity, "total_cents": sum(row["price_cents"] for row in rows)}

    def create_market_purchase(
        self,
        buyer_telegram_id: int,
        option_id: int,
        quantity: int,
        request_key: str,
        admin_telegram_user_id: int,
        quoted_total_cents: int,
    ) -> tuple[sqlite3.Row, list[str]]:
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM market_orders WHERE idempotency_key=?", (request_key,)
            ).fetchone()
            if duplicate is not None:
                items = connection.execute(
                    """
                    SELECT i.secret_text FROM market_order_items oi
                    JOIN inventory_items i ON i.id=oi.inventory_item_id
                    WHERE oi.order_id=? ORDER BY oi.id
                    """,
                    (duplicate["id"],),
                ).fetchall()
                return duplicate, [item["secret_text"] for item in items]
            buyer = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (buyer_telegram_id,)
            ).fetchone()
            if buyer is None:
                raise ValueError("Buyer not found.")
            rows = connection.execute(
                """
                SELECT i.id item_id, i.secret_text, b.price_cents, b.owner_user_id
                FROM inventory_items i
                JOIN inventory_batches b ON b.id=i.batch_id
                JOIN users owner ON owner.id=b.owner_user_id
                WHERE b.option_id=? AND i.status='available'
                ORDER BY CASE WHEN owner.telegram_user_id=? THEN 0 ELSE 1 END,
                  b.created_at, b.id, i.id
                LIMIT ?
                """,
                (option_id, admin_telegram_user_id, quantity),
            ).fetchall()
            if len(rows) < quantity:
                raise ValueError("Stock changed. Please review the available stock.")
            total = sum(row["price_cents"] for row in rows)
            if total != quoted_total_cents:
                raise ValueError("Price or stock changed. Please review the purchase again.")
            if buyer["balance_cents"] < total:
                raise ValueError(
                    f"Insufficient balance. Required {total} cents, current {buyer['balance_cents']} cents."
                )
            order_cursor = connection.execute(
                """
                INSERT INTO market_orders (buyer_user_id, option_id, quantity, total_cents, idempotency_key)
                VALUES (?, ?, ?, ?, ?)
                """,
                (buyer["id"], option_id, quantity, total, request_key),
            )
            order_id = order_cursor.lastrowid
            owners: dict[int, int] = {}
            for row in rows:
                changed = connection.execute(
                    "UPDATE inventory_items SET status='sold', order_id=?, sold_at=CURRENT_TIMESTAMP WHERE id=? AND status='available'",
                    (order_id, row["item_id"]),
                )
                if changed.rowcount != 1:
                    raise ValueError("Stock changed. Please try again.")
                connection.execute(
                    """
                    INSERT INTO market_order_items (order_id, inventory_item_id, owner_user_id, price_cents)
                    VALUES (?, ?, ?, ?)
                    """,
                    (order_id, row["item_id"], row["owner_user_id"], row["price_cents"]),
                )
                owners[row["owner_user_id"]] = owners.get(row["owner_user_id"], 0) + row["price_cents"]
            self.ledger_entry(
                connection,
                user_id=buyer["id"],
                amount_cents=-total,
                entry_type="purchase",
                reference_type="market_order",
                reference_id=order_id,
                idempotency_key=f"market-purchase-buyer:{request_key}",
                note=f"Option #{option_id} x {quantity}",
            )
            for owner_id, amount in owners.items():
                self.ledger_entry(
                    connection,
                    user_id=owner_id,
                    amount_cents=amount,
                    entry_type="owner_sale",
                    reference_type="market_order",
                    reference_id=order_id,
                    idempotency_key=f"market-purchase-owner:{request_key}:{owner_id}",
                    note=f"Option #{option_id} x {amount} cents",
                )
            order = connection.execute(
                "SELECT * FROM market_orders WHERE id=?", (order_id,)
            ).fetchone()
            return order, [row["secret_text"] for row in rows]

    def create_withdrawal(
        self, telegram_user_id: int, method_key: str, amount_cents: int, payout_details: str
    ) -> sqlite3.Row:
        with self.transaction() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)
            ).fetchone()
            if user is None:
                raise ValueError("User not found.")
            if amount_cents <= 0:
                raise ValueError("Amount must be positive.")
            if user["balance_cents"] < amount_cents:
                raise ValueError(
                    f"Insufficient balance. Current balance: {user['balance_cents']} cents."
                )
            cursor = connection.execute(
                """
                INSERT INTO withdrawals (user_id, method_key, amount_cents, payout_details)
                VALUES (?, ?, ?, ?)
                """,
                (user["id"], method_key, amount_cents, payout_details),
            )
            withdrawal_id = cursor.lastrowid
            self.ledger_entry(
                connection,
                user_id=user["id"],
                amount_cents=-amount_cents,
                entry_type="withdrawal_reserve",
                reference_type="withdrawal",
                reference_id=withdrawal_id,
                idempotency_key=f"withdrawal-reserve:{withdrawal_id}",
                note="Amount reserved until admin approval or rejection.",
            )
            connection.execute(
                "UPDATE users SET reserved_cents=reserved_cents+? WHERE id=?",
                (amount_cents, user["id"]),
            )
            return connection.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()

    def process_withdrawal(self, withdrawal_id: int, approve: bool) -> sqlite3.Row:
        with self.transaction() as connection:
            withdrawal = connection.execute(
                "SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)
            ).fetchone()
            if withdrawal is None:
                raise ValueError("Withdrawal not found.")
            if withdrawal["status"] != "pending":
                raise ValueError("This withdrawal has already been processed.")
            changed = connection.execute(
                "UPDATE users SET reserved_cents=reserved_cents-? WHERE id=? AND reserved_cents>=?",
                (withdrawal["amount_cents"], withdrawal["user_id"], withdrawal["amount_cents"]),
            )
            if changed.rowcount != 1:
                raise ValueError("Reserved balance is inconsistent.")
            if not approve:
                self.ledger_entry(
                    connection,
                    user_id=withdrawal["user_id"],
                    amount_cents=withdrawal["amount_cents"],
                    entry_type="withdrawal_refund",
                    reference_type="withdrawal",
                    reference_id=withdrawal_id,
                    idempotency_key=f"withdrawal-refund:{withdrawal_id}",
                    note="Withdrawal rejected; reserved amount returned.",
                )
            connection.execute(
                "UPDATE withdrawals SET status=?, processed_at=CURRENT_TIMESTAMP WHERE id=?",
                ("approved" if approve else "rejected", withdrawal_id),
            )
            return connection.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()