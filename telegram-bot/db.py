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

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category_id INTEGER NOT NULL REFERENCES categories(id),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    payout_details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_user_id INTEGER NOT NULL REFERENCES users(id),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    total_cents INTEGER NOT NULL CHECK (total_cents > 0),
    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'cancelled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        self.connection.commit()

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
            "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
        if row is None:
            raise ValueError("User is not registered.")
        return row

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
            "SELECT * FROM user_states WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()

    def clear_state(self, telegram_user_id: int) -> None:
        self.connection.execute(
            "DELETE FROM user_states WHERE telegram_user_id = ?", (telegram_user_id,)
        )
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
        self.connection.execute(
            "INSERT OR IGNORE INTO sqlite_sequence (name, seq) VALUES ('orders', 1000)"
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

    def approve_deposit(self, deposit_id: int) -> sqlite3.Row:
        with self.transaction() as connection:
            deposit = connection.execute(
                "SELECT * FROM deposits WHERE id=?", (deposit_id,)
            ).fetchone()
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

    def create_purchase(self, buyer_telegram_id: int, product_id: int, quantity: int, request_key: str) -> sqlite3.Row:
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key=?", (request_key,)
            ).fetchone()
            if duplicate is not None:
                return duplicate
            buyer = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (buyer_telegram_id,)
            ).fetchone()
            product = connection.execute(
                """
                SELECT p.*, u.id AS owner_id, u.telegram_user_id AS owner_telegram_id
                FROM products p JOIN users u ON u.id=p.owner_user_id
                WHERE p.id=? AND p.status='active'
                """,
                (product_id,),
            ).fetchone()
            if buyer is None or product is None:
                raise ValueError("Product or buyer not found.")
            if quantity <= 0:
                raise ValueError("Quantity must be positive.")
            if product["stock"] < quantity:
                raise ValueError(f"Only {product['stock']} item(s) available.")
            total = product["price_cents"] * quantity
            if buyer["balance_cents"] < total:
                raise ValueError(
                    f"Insufficient balance. Required {total} cents, current {buyer['balance_cents']} cents."
                )
            connection.execute(
                "UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                (quantity, product_id, quantity),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("Stock changed. Please try again.")
            order = connection.execute(
                """
                INSERT INTO orders (
                  buyer_user_id, owner_user_id, product_id, quantity,
                  unit_price_cents, total_cents, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    buyer["id"],
                    product["owner_id"],
                    product_id,
                    quantity,
                    product["price_cents"],
                    total,
                    request_key,
                ),
            )
            order_id = order.lastrowid
            self.ledger_entry(
                connection,
                user_id=buyer["id"],
                amount_cents=-total,
                entry_type="purchase",
                reference_type="order",
                reference_id=order_id,
                idempotency_key=f"purchase-buyer:{request_key}",
                note=f"Product #{product_id} x {quantity}",
            )
            self.ledger_entry(
                connection,
                user_id=product["owner_id"],
                amount_cents=total,
                entry_type="owner_sale",
                reference_type="order",
                reference_id=order_id,
                idempotency_key=f"purchase-owner:{request_key}",
                note=f"Product #{product_id} x {quantity}",
            )
            return connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def create_withdrawal(self, telegram_user_id: int, amount_cents: int, payout_details: str) -> sqlite3.Row:
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
            withdrawal = connection.execute(
                """
                INSERT INTO withdrawals (user_id, amount_cents, payout_details)
                VALUES (?, ?, ?)
                """,
                (user["id"], amount_cents, payout_details),
            )
            withdrawal_id = withdrawal.lastrowid
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
            return connection.execute(
                "SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)
            ).fetchone()

    def process_withdrawal(self, withdrawal_id: int, approve: bool) -> sqlite3.Row:
        with self.transaction() as connection:
            withdrawal = connection.execute(
                "SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)
            ).fetchone()
            if withdrawal is None:
                raise ValueError("Withdrawal not found.")
            if withdrawal["status"] != "pending":
                raise ValueError("This withdrawal has already been processed.")
            connection.execute(
                "UPDATE users SET reserved_cents=reserved_cents-? WHERE id=? AND reserved_cents>=?",
                (withdrawal["amount_cents"], withdrawal["user_id"], withdrawal["amount_cents"]),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
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
            return connection.execute(
                "SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)
            ).fetchone()