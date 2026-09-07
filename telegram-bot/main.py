from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from config import Settings, load_settings
from db import Database
from telegram_api import TelegramApi


BACK = "🔙 Back"
CANCEL = "❌ Cancel"


def money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class MarketplaceBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = TelegramApi(settings.bot_token)
        self.db = Database(settings.db_path)
        self.db.seed_defaults()

    def run(self) -> None:
        print("Telegram Marketplace Bot is running.")
        offset: int | None = None
        while True:
            for update in self.api.get_updates(offset, self.settings.polling_timeout):
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception as error:
                    print(f"Update handling error: {error}")
                    chat_id = self.chat_id(update)
                    if chat_id:
                        self.api.send_message(chat_id, "❌ একটি server error হয়েছে। আবার চেষ্টা করুন।")

    @staticmethod
    def chat_id(update: dict[str, Any]) -> int | None:
        message = update.get("message") or update.get("callback_query", {}).get("message")
        return message.get("chat", {}).get("id") if message else None

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if not message or "from" not in message:
            return
        user = message["from"]
        self.db.ensure_user(user["id"], user.get("username"), user.get("first_name", ""))
        text = message.get("text", "").strip()
        if text in ("/start", "/menu"):
            self.db.clear_state(user["id"])
            self.send_main_menu(user["id"])
            return
        if text in ("/cancel", CANCEL):
            self.cancel(user["id"])
            return
        state = self.db.get_state(user["id"])
        if state:
            self.handle_input(user["id"], text, state["state"], json.loads(state["data_json"]))
        else:
            self.send_main_menu(user["id"])

    def handle_callback(self, callback: dict[str, Any]) -> None:
        user = callback["from"]
        chat_id = callback["message"]["chat"]["id"]
        data = callback.get("data", "")
        self.db.ensure_user(user["id"], user.get("username"), user.get("first_name", ""))
        self.api.answer_callback(callback["id"])
        if data in ("back", "cancel"):
            self.cancel(user["id"])
        elif data == "menu":
            self.db.clear_state(user["id"])
            self.send_main_menu(chat_id)
        elif data == "balance":
            self.show_balance(chat_id)
        elif data == "deposit":
            self.start_deposit(chat_id)
        elif data == "withdraw":
            self.start_withdrawal(chat_id)
        elif data == "market":
            self.show_categories(chat_id)
        elif data.startswith("category:"):
            self.show_products(chat_id, int(data.split(":")[1]))
        elif data.startswith("product:"):
            self.show_product(chat_id, int(data.split(":")[1]))
        elif data.startswith("buy:"):
            self.start_purchase(chat_id, int(data.split(":")[1]))
        elif data.startswith("payment_method:"):
            self.select_deposit_method(chat_id, data.split(":", 1)[1])
        elif data in ("deposit_confirm", "withdraw_confirm", "purchase_confirm", "product_confirm", "broadcast_confirm", "balance_confirm"):
            self.confirm_from_callback(chat_id, data)
        elif data == "support":
            self.api.send_message(chat_id, "Support-এর জন্য admin-কে আপনার Telegram username ও সমস্যার বিবরণ পাঠান।", keyboard([[("🏠 Main Menu", "menu")]]))
        elif data == "admin":
            self.show_admin_menu(chat_id, user["id"])
        elif data == "admin:deposits":
            self.show_pending_deposits(chat_id, user["id"])
        elif data.startswith("deposit:approve:") or data.startswith("deposit:reject:"):
            self.process_deposit(chat_id, user["id"], data)
        elif data == "admin:withdrawals":
            self.show_pending_withdrawals(chat_id, user["id"])
        elif data.startswith("withdrawal:approve:") or data.startswith("withdrawal:reject:"):
            self.process_withdrawal(chat_id, user["id"], data)
        elif data == "admin:products":
            self.admin_products(chat_id, user["id"])
        elif data == "admin:add_product":
            self.start_add_product(chat_id, user["id"])
        elif data == "admin:payment":
            self.show_payment_setup(chat_id, user["id"])
        elif data.startswith("payment:"):
            self.start_payment_setup(chat_id, user["id"], data.split(":")[1])
        elif data == "admin:broadcast":
            self.start_broadcast(chat_id, user["id"])
        elif data == "admin:orders":
            self.show_orders(chat_id, user["id"])
        elif data == "admin:sales":
            self.show_sales(chat_id, user["id"])
        elif data == "admin:balance":
            self.start_balance_adjustment(chat_id, user["id"])

    def send_main_menu(self, chat_id: int) -> None:
        rows = [
            [("💳 Balance", "balance"), ("🛍 Marketplace", "market")],
            [("➕ Deposit", "deposit"), ("↗️ Withdrawal", "withdraw")],
            [("🆘 Support", "support")],
        ]
        if chat_id == self.settings.admin_user_id:
            rows.append([("⚙️ Admin Panel", "admin")])
        self.api.send_message(chat_id, "Telegram Marketplace Bot-এ স্বাগতম। একটি option নির্বাচন করুন:", keyboard(rows))

    def show_balance(self, chat_id: int) -> None:
        user = self.db.get_user(chat_id)
        self.api.send_message(
            chat_id,
            f"💳 Available balance: {money(user['balance_cents'])}\n🔒 Reserved for withdrawals: {money(user['reserved_cents'])}",
            keyboard([[("➕ Deposit", "deposit"), ("↗️ Withdrawal", "withdraw")], [("🏠 Main Menu", "menu")]]),
        )

    def show_categories(self, chat_id: int) -> None:
        categories = self.db.connection.execute(
            """
            SELECT c.id, c.name, COALESCE(SUM(CASE WHEN p.status='active' THEN p.stock ELSE 0 END), 0) stock
            FROM categories c LEFT JOIN products p ON p.category_id=c.id
            GROUP BY c.id ORDER BY c.name
            """
        ).fetchall()
        if not categories:
            self.api.send_message(chat_id, "📦 Marketplace এখনো খালি।", keyboard([[("🏠 Main Menu", "menu")]]))
            return
        rows = [[(f"{row['name']} — Stock: {row['stock']}", f"category:{row['id']}")] for row in categories]
        rows.append([("🏠 Main Menu", "menu")])
        self.api.send_message(chat_id, "একটি category নির্বাচন করুন:", keyboard(rows))

    def show_products(self, chat_id: int, category_id: int) -> None:
        products = self.db.connection.execute(
            "SELECT * FROM products WHERE category_id=? AND status='active' ORDER BY id", (category_id,)
        ).fetchall()
        category = self.db.connection.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
        if not products:
            self.api.send_message(chat_id, "📦 Stock: 0\nবর্তমানে unavailable।", keyboard([[("🔙 Back", "market"), ("🏠 Main Menu", "menu")]]))
            return
        rows = []
        for product in products:
            availability = f"Stock: {product['stock']}" if product["stock"] else "Stock: 0 — unavailable"
            rows.append([(f"{product['name']} — {money(product['price_cents'])} | {availability}", f"product:{product['id']}")])
        rows.append([("🔙 Back", "market"), ("🏠 Main Menu", "menu")])
        self.api.send_message(chat_id, f"Category: {category['name']}", keyboard(rows))

    def show_product(self, chat_id: int, product_id: int) -> None:
        product = self.db.connection.execute(
            """
            SELECT p.*, c.name category_name FROM products p
            JOIN categories c ON c.id=p.category_id WHERE p.id=? AND p.status='active'
            """,
            (product_id,),
        ).fetchone()
        if not product:
            self.api.send_message(chat_id, "Product পাওয়া যায়নি।", keyboard([[("🔙 Back", "market")]]))
            return
        text = (
            f"🛍 {product['name']}\nCategory: {product['category_name']}\n"
            f"{product['description']}\nPrice: {money(product['price_cents'])}\n"
            f"Stock: {product['stock']}"
        )
        rows = [[("🛒 Buy", f"buy:{product_id}")]] if product["stock"] > 0 else []
        rows.append([("🔙 Back", f"category:{product['category_id']}"), ("🏠 Main Menu", "menu")])
        self.api.send_message(chat_id, text, keyboard(rows))

    def start_deposit(self, chat_id: int) -> None:
        methods = self.db.connection.execute(
            "SELECT * FROM payment_methods WHERE is_active=1 ORDER BY id"
        ).fetchall()
        rows = [[(method["display_name"], f"payment_method:{method['method_key']}")] for method in methods]
        rows.append([(CANCEL, "cancel")])
        self.db.set_state(chat_id, "deposit_method")
        self.api.send_message(chat_id, "Deposit method নির্বাচন করুন:", keyboard(rows))

    def select_deposit_method(self, chat_id: int, method_key: str) -> None:
        method = self.db.connection.execute(
            "SELECT * FROM payment_methods WHERE method_key=? AND is_active=1", (method_key,)
        ).fetchone()
        if not method:
            self.api.send_message(chat_id, "❌ Payment method unavailable।", keyboard([[("🏠 Main Menu", "menu")]]))
            return
        self.db.set_state(chat_id, "deposit_amount", json.dumps({"method_key": method_key}))
        self.api.send_message(
            chat_id,
            f"{method['display_name']} payment details:\n{method['details']}\n\nকত টাকা deposit করবেন? শুধু number দিন।",
            keyboard([[(CANCEL, "cancel")]]),
        )

    def start_withdrawal(self, chat_id: int) -> None:
        self.db.set_state(chat_id, "withdraw_amount")
        self.api.send_message(chat_id, "কত টাকা withdraw করবেন? শুধু number দিন।", keyboard([[(CANCEL, "cancel")]]))

    def start_purchase(self, chat_id: int, product_id: int) -> None:
        product = self.db.connection.execute("SELECT * FROM products WHERE id=? AND status='active'", (product_id,)).fetchone()
        if not product:
            self.api.send_message(chat_id, "Product unavailable।", keyboard([[("🏠 Main Menu", "menu")]]))
            return
        self.db.set_state(chat_id, "purchase_quantity", json.dumps({"product_id": product_id}))
        self.api.send_message(chat_id, f"কতটি {product['name']} কিনবেন? Available: {product['stock']}", keyboard([[(CANCEL, "cancel")]]))

    def handle_input(self, chat_id: int, text: str, state: str, data: dict[str, Any]) -> None:
        if not text:
            self.api.send_message(chat_id, "❌ Input খালি রাখা যাবে না। আবার দিন।")
            return
        if state == "deposit_amount":
            amount = self.parse_amount(text)
            if amount is None:
                self.api.send_message(chat_id, "❌ সঠিক সংখ্যা দিন।")
                return
            data["amount_cents"] = amount
            self.db.set_state(chat_id, "deposit_txid", json.dumps(data))
            self.api.send_message(chat_id, "Payment TXID দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "deposit_txid":
            data["txid"] = text
            self.db.set_state(chat_id, "deposit_confirm", json.dumps(data))
            self.api.send_message(
                chat_id,
                f"Deposit preview:\nMethod: {data['method_key']}\nAmount: {money(data['amount_cents'])}\nTXID: {text}\n\nConfirm-এর আগে কোনো deposit order তৈরি হবে না।",
                keyboard([[("✅ Confirm", "deposit_confirm"), (CANCEL, "cancel")]]),
            )
        elif state == "withdraw_amount":
            amount = self.parse_amount(text)
            if amount is None:
                self.api.send_message(chat_id, "❌ সঠিক সংখ্যা দিন।")
                return
            user = self.db.get_user(chat_id)
            if amount > user["balance_cents"]:
                self.api.send_message(chat_id, f"❌ Required: {money(amount)}\nCurrent balance: {money(user['balance_cents'])}\nআবার amount দিন।")
                return
            data["amount_cents"] = amount
            self.db.set_state(chat_id, "withdraw_details", json.dumps(data))
            self.api.send_message(chat_id, "bKash/Nagad/Binance payout details দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "withdraw_details":
            self.db.set_state(chat_id, "withdraw_confirm", json.dumps({**data, "payout_details": text}))
            self.api.send_message(chat_id, f"Withdrawal preview:\nAmount: {money(data['amount_cents'])}\nDetails: {text}\n\nConfirm করলে amount reserve হবে।", keyboard([[("✅ Confirm", "withdraw_confirm"), (CANCEL, "cancel")]]))
        elif state == "purchase_quantity":
            if not text.isdigit() or int(text) <= 0:
                self.api.send_message(chat_id, "❌ সঠিক quantity দিন।")
                return
            quantity = int(text)
            product = self.db.connection.execute("SELECT * FROM products WHERE id=?", (data["product_id"],)).fetchone()
            if not product:
                self.cancel(chat_id)
                return
            if quantity > product["stock"]:
                self.api.send_message(chat_id, f"❌ Available: {product['stock']}\nআবার quantity দিন।")
                return
            total = product["price_cents"] * quantity
            user = self.db.get_user(chat_id)
            if total > user["balance_cents"]:
                self.api.send_message(chat_id, f"❌ Required: {money(total)}\nCurrent balance: {money(user['balance_cents'])}\n")
                return
            preview = {"product_id": product["id"], "quantity": quantity, "total": total}
            self.db.set_state(chat_id, "purchase_confirm", json.dumps(preview))
            self.api.send_message(chat_id, f"Purchase preview:\n{product['name']} x {quantity}\nTotal: {money(total)}\n\nConfirm-এর আগে কোনো পরিবর্তন হবে না।", keyboard([[("✅ Confirm", "purchase_confirm"), (CANCEL, "cancel")]]))
        elif state == "product_name":
            data["name"] = text
            self.db.set_state(chat_id, "product_description", json.dumps(data))
            self.api.send_message(chat_id, "Product description দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "product_description":
            data["description"] = text
            self.db.set_state(chat_id, "product_category", json.dumps(data))
            self.api.send_message(chat_id, "Category name দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "product_category":
            data["category"] = text
            self.db.set_state(chat_id, "product_owner", json.dumps(data))
            self.api.send_message(chat_id, "Owner Telegram user ID দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "product_owner":
            if not text.isdigit():
                self.api.send_message(chat_id, "❌ সঠিক numeric user ID দিন।")
                return
            owner = self.db.connection.execute("SELECT id FROM users WHERE telegram_user_id=?", (int(text),)).fetchone()
            if not owner:
                self.api.send_message(chat_id, "❌ এই user আগে bot-এ /start করেনি। সঠিক UID আবার দিন।")
                return
            data["owner_telegram_id"] = int(text)
            self.db.set_state(chat_id, "product_price", json.dumps(data))
            self.api.send_message(chat_id, "Product price দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "product_price":
            amount = self.parse_amount(text)
            if amount is None or amount <= 0:
                self.api.send_message(chat_id, "❌ সঠিক positive price দিন।")
                return
            data["price_cents"] = amount
            self.db.set_state(chat_id, "product_stock", json.dumps(data))
            self.api.send_message(chat_id, "Stock quantity দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "product_stock":
            if not text.isdigit() or int(text) < 0:
                self.api.send_message(chat_id, "❌ সঠিক stock number দিন।")
                return
            data["stock"] = int(text)
            self.db.set_state(chat_id, "product_confirm", json.dumps(data))
            self.api.send_message(chat_id, f"Product preview:\n{data['name']}\nOwner UID: {data['owner_telegram_id']}\nPrice: {money(data['price_cents'])}\nStock: {data['stock']}", keyboard([[("✅ Confirm", "product_confirm"), (CANCEL, "cancel")]]))
        elif state == "payment_details":
            self.db.connection.execute(
                "UPDATE payment_methods SET details=?, updated_at=CURRENT_TIMESTAMP WHERE method_key=?",
                (text, data["method_key"]),
            )
            self.db.connection.commit()
            self.db.clear_state(chat_id)
            self.show_payment_setup(chat_id, chat_id)
        elif state == "broadcast":
            self.db.set_state(chat_id, "broadcast_confirm", json.dumps({"message": text}))
            self.api.send_message(chat_id, f"Broadcast preview:\n\n{text}", keyboard([[("✅ Send Broadcast", "broadcast_confirm"), (CANCEL, "cancel")]]))
        elif state == "balance_user":
            if not text.isdigit():
                self.api.send_message(chat_id, "❌ সঠিক numeric user ID দিন।")
                return
            data["telegram_user_id"] = int(text)
            self.db.set_state(chat_id, "balance_amount", json.dumps(data))
            self.api.send_message(chat_id, "Amount দিন। Add করতে positive, deduct করতে negative number লিখুন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "balance_amount":
            try:
                amount = round(float(text.replace(",", "")) * 100)
            except ValueError:
                self.api.send_message(chat_id, "❌ সঠিক সংখ্যা দিন।")
                return
            if amount == 0:
                self.api.send_message(chat_id, "❌ Amount zero হতে পারবে না।")
                return
            data["amount_cents"] = amount
            self.db.set_state(chat_id, "balance_note", json.dumps(data))
            self.api.send_message(chat_id, "Adjustment note দিন:", keyboard([[(CANCEL, "cancel")]]))
        elif state == "balance_note":
            data["note"] = text
            self.db.set_state(chat_id, "balance_confirm", json.dumps(data))
            direction = "Add" if data["amount_cents"] > 0 else "Deduct"
            self.api.send_message(
                chat_id,
                f"Balance adjustment preview:\nUser: {data['telegram_user_id']}\n"
                f"{direction}: {money(abs(data['amount_cents']))}\nNote: {data['note']}",
                keyboard([[("✅ Confirm", "balance_confirm"), (CANCEL, "cancel")]]),
            )

    @staticmethod
    def parse_amount(text: str) -> int | None:
        try:
            value = float(text.replace(",", ""))
            cents = round(value * 100)
            return cents if cents > 0 else None
        except ValueError:
            return None

    def handle_confirm(self, chat_id: int, state: str, data: dict[str, Any]) -> None:
        if state == "deposit_confirm":
            try:
                self.db.connection.execute(
                    "INSERT INTO deposits (user_id, method_key, amount_cents, txid) VALUES ((SELECT id FROM users WHERE telegram_user_id=?), ?, ?, ?)",
                    (chat_id, data["method_key"], data["amount_cents"], data["txid"]),
                )
                self.db.connection.commit()
            except sqlite3.IntegrityError:
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, "❌ এই TXID আগে ব্যবহার হয়েছে। Deposit order তৈরি হয়নি।", keyboard([[("💳 Balance", "balance"), ("🏠 Main Menu", "menu")]]))
                return
            self.db.clear_state(chat_id)
            self.api.send_message(chat_id, "✅ Deposit order তৈরি হয়েছে। Admin approval-এর পর balance যোগ হবে।", keyboard([[("💳 Balance", "balance"), ("🏠 Main Menu", "menu")]]))
        elif state == "purchase_confirm":
            request_key = f"purchase:{chat_id}:{data['product_id']}:{data['quantity']}:{int(time.time()) // 60}"
            try:
                order = self.db.create_purchase(chat_id, data["product_id"], data["quantity"], request_key)
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, f"✅ Purchase successful!\nOrder: #{order['id']}\nTotal: {money(order['total_cents'])}", keyboard([[("🛍 Marketplace", "market"), ("🏠 Main Menu", "menu")]]))
            except ValueError as error:
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, f"❌ Purchase failed: {error}", keyboard([[("🛍 Marketplace", "market"), ("🏠 Main Menu", "menu")]]))
        elif state == "withdraw_confirm":
            try:
                withdrawal = self.db.create_withdrawal(chat_id, data["amount_cents"], data["payout_details"])
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, f"✅ Withdrawal request #{withdrawal['id']} created.\nAmount reserved until admin processing.", keyboard([[("💳 Balance", "balance"), ("🏠 Main Menu", "menu")]]))
            except ValueError as error:
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, f"❌ Withdrawal failed: {error}", keyboard([[("💳 Balance", "balance"), ("🏠 Main Menu", "menu")]]))
        elif state == "product_confirm":
            try:
                with self.db.transaction() as connection:
                    category = connection.execute("SELECT id FROM categories WHERE name=?", (data["category"],)).fetchone()
                    if category is None:
                        category = connection.execute("INSERT INTO categories (name) VALUES (?) RETURNING id", (data["category"],)).fetchone()
                    owner = connection.execute("SELECT id FROM users WHERE telegram_user_id=?", (data["owner_telegram_id"],)).fetchone()
                    connection.execute(
                        """
                        INSERT INTO products (name, description, category_id, owner_user_id, price_cents, stock)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (data["name"], data["description"], category["id"], owner["id"], data["price_cents"], data["stock"]),
                    )
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, "✅ Product marketplace-এ যোগ হয়েছে।", keyboard([[("⚙️ Admin Panel", "admin"), ("🏠 Main Menu", "menu")]]))
            except (ValueError, sqlite3.IntegrityError) as error:
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, f"❌ Product save failed: {error}", keyboard([[("⚙️ Admin Panel", "admin")]]))
        elif state == "broadcast_confirm":
            users = self.db.connection.execute("SELECT telegram_user_id FROM users").fetchall()
            sent = 0
            for row in users:
                try:
                    self.api.send_message(row["telegram_user_id"], data["message"])
                    sent += 1
                except Exception as error:
                    print(f"Broadcast failed for {row['telegram_user_id']}: {error}")
            self.db.clear_state(chat_id)
            self.api.send_message(chat_id, f"✅ Broadcast complete. Sent: {sent}", keyboard([[("⚙️ Admin Panel", "admin")]]))
        elif state == "balance_confirm":
            try:
                user = self.db.adjust_balance(data["telegram_user_id"], data["amount_cents"], data["note"])
                self.db.clear_state(chat_id)
                self.api.send_message(
                    chat_id,
                    f"✅ Balance updated for {user['telegram_user_id']}.\n"
                    f"Available balance: {money(user['balance_cents'])}",
                    keyboard([[("⚙️ Admin Panel", "admin")]]),
                )
                self.api.send_message(
                    user["telegram_user_id"],
                    f"Your balance was adjusted by admin.\nCurrent balance: {money(user['balance_cents'])}",
                    keyboard([[("💳 Balance", "balance")]]),
                )
            except ValueError as error:
                self.db.clear_state(chat_id)
                self.api.send_message(chat_id, f"❌ Balance adjustment failed: {error}", keyboard([[("⚙️ Admin Panel", "admin")]]))

    def confirm_from_callback(self, chat_id: int, action: str) -> None:
        state = self.db.get_state(chat_id)
        if not state:
            self.api.send_message(chat_id, "এই action ইতিমধ্যে process হয়েছে বা expired।", keyboard([[("🏠 Main Menu", "menu")]]))
            return
        expected = {
            "deposit_confirm": "deposit_confirm",
            "withdraw_confirm": "withdraw_confirm",
            "purchase_confirm": "purchase_confirm",
            "product_confirm": "product_confirm",
            "broadcast_confirm": "broadcast_confirm",
            "balance_confirm": "balance_confirm",
        }.get(action)
        if state["state"] != expected:
            self.api.send_message(chat_id, "❌ এই action আর valid নেই। আবার শুরু করুন।", keyboard([[("🏠 Main Menu", "menu")]]))
            return
        self.handle_confirm(chat_id, state["state"], json.loads(state["data_json"]))

    def cancel(self, chat_id: int) -> None:
        self.db.clear_state(chat_id)
        self.api.send_message(chat_id, "Cancelled। কোনো balance, stock বা order পরিবর্তন হয়নি।", keyboard([[("🏠 Main Menu", "menu")]]))

    def show_admin_menu(self, chat_id: int, user_id: int) -> None:
        if user_id != self.settings.admin_user_id:
            self.api.send_message(chat_id, "❌ Unauthorized.")
            return
        self.api.send_message(
            chat_id,
            "Admin Panel",
            keyboard([
                [("Pending Deposits", "admin:deposits"), ("Pending Withdrawals", "admin:withdrawals")],
                [("Users & Balance", "admin:balance"), ("Marketplace Handle", "admin:products")],
                [("Payment Setup", "admin:payment")],
                [("Sales", "admin:sales"), ("Orders", "admin:orders")],
                [("Broadcast", "admin:broadcast")],
                [("🏠 Main Menu", "menu")],
            ]),
        )

    def start_balance_adjustment(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "balance_user")
        self.api.send_message(chat_id, "যে user-এর balance বদলাবেন তার Telegram user ID দিন:", keyboard([[(CANCEL, "cancel")]]))

    def show_sales(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        sales = self.db.connection.execute(
            """
            SELECT COUNT(*) order_count, COALESCE(SUM(total_cents), 0) total_cents,
              COALESCE(SUM(quantity), 0) total_items
            FROM orders WHERE status='completed'
            """
        ).fetchone()
        approved_deposits = self.db.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) total FROM deposits WHERE status='approved'"
        ).fetchone()["total"]
        pending = self.db.connection.execute(
            "SELECT COUNT(*) total FROM deposits WHERE status='pending'"
        ).fetchone()["total"]
        self.api.send_message(
            chat_id,
            f"Sales overview\nCompleted orders: {sales['order_count']}\n"
            f"Items sold: {sales['total_items']}\nGross marketplace volume: {money(sales['total_cents'])}\n"
            f"Approved deposits: {money(approved_deposits)}\nPending deposits: {pending}",
            keyboard([[("⚙️ Admin Panel", "admin"), ("🏠 Main Menu", "menu")]]),
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.settings.admin_user_id

    def show_pending_deposits(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        rows = self.db.connection.execute(
            """
            SELECT d.*, u.telegram_user_id, u.username FROM deposits d
            JOIN users u ON u.id=d.user_id WHERE d.status='pending' ORDER BY d.id
            """
        ).fetchall()
        if not rows:
            self.api.send_message(chat_id, "Pending deposit নেই।", keyboard([[("⚙️ Admin Panel", "admin")]]))
            return
        for deposit in rows:
            text = f"Deposit #{deposit['id']}\nUser: {deposit['telegram_user_id']} @{deposit['username'] or '-'}\nAmount: {money(deposit['amount_cents'])}\nMethod: {deposit['method_key']}\nTXID: {deposit['txid']}"
            self.api.send_message(chat_id, text, keyboard([[("✅ Approve", f"deposit:approve:{deposit['id']}"), ("❌ Reject", f"deposit:reject:{deposit['id']}")]]))

    def process_deposit(self, chat_id: int, user_id: int, data: str) -> None:
        if not self.is_admin(user_id):
            self.api.send_message(chat_id, "❌ Unauthorized.")
            return
        action, deposit_id = data.split(":")[1:]
        try:
            deposit = self.db.approve_deposit(int(deposit_id)) if action == "approve" else self.db.reject_deposit(int(deposit_id))
            self.api.send_message(chat_id, f"✅ Deposit #{deposit['id']} {deposit['status']}.", keyboard([[("⚙️ Admin Panel", "admin")]]))
            if action == "approve":
                user = self.db.connection.execute("SELECT telegram_user_id FROM users WHERE id=?", (deposit["user_id"],)).fetchone()
                self.api.send_message(user["telegram_user_id"], f"✅ Deposit approved. Added: {money(deposit['amount_cents'])}", keyboard([[("💳 Balance", "balance")]]))
        except ValueError as error:
            self.api.send_message(chat_id, f"❌ {error}", keyboard([[("⚙️ Admin Panel", "admin")]]))

    def show_pending_withdrawals(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        rows = self.db.connection.execute(
            """
            SELECT w.*, u.telegram_user_id, u.username FROM withdrawals w
            JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id
            """
        ).fetchall()
        if not rows:
            self.api.send_message(chat_id, "Pending withdrawal নেই।", keyboard([[("⚙️ Admin Panel", "admin")]]))
            return
        for item in rows:
            text = f"Withdrawal #{item['id']}\nUser: {item['telegram_user_id']} @{item['username'] or '-'}\nAmount: {money(item['amount_cents'])}\nPayout: {item['payout_details']}"
            self.api.send_message(chat_id, text, keyboard([[("✅ Paid & Approve", f"withdrawal:approve:{item['id']}"), ("❌ Reject", f"withdrawal:reject:{item['id']}")]]))

    def process_withdrawal(self, chat_id: int, user_id: int, data: str) -> None:
        if not self.is_admin(user_id):
            self.api.send_message(chat_id, "❌ Unauthorized.")
            return
        action, withdrawal_id = data.split(":")[1:]
        try:
            item = self.db.process_withdrawal(int(withdrawal_id), action == "approve")
            self.api.send_message(chat_id, f"✅ Withdrawal #{item['id']} {item['status']}.", keyboard([[("⚙️ Admin Panel", "admin")]]))
            user = self.db.connection.execute("SELECT telegram_user_id FROM users WHERE id=?", (item["user_id"],)).fetchone()
            self.api.send_message(user["telegram_user_id"], f"Withdrawal #{item['id']} {item['status']}.", keyboard([[("💳 Balance", "balance")]]))
        except ValueError as error:
            self.api.send_message(chat_id, f"❌ {error}", keyboard([[("⚙️ Admin Panel", "admin")]]))

    def admin_products(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        products = self.db.connection.execute("SELECT * FROM products ORDER BY id DESC LIMIT 10").fetchall()
        text = "Recent products:\n" + "\n".join(f"#{p['id']} {p['name']} | {money(p['price_cents'])} | Stock {p['stock']}" for p in products) if products else "No products yet."
        self.api.send_message(chat_id, text, keyboard([[("➕ Add Product", "admin:add_product")], [("🔙 Back", "admin"), ("🏠 Main Menu", "menu")]]))

    def start_add_product(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "product_name")
        self.api.send_message(chat_id, "Product name দিন:", keyboard([[(CANCEL, "cancel")]]))

    def show_payment_setup(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        methods = self.db.connection.execute("SELECT * FROM payment_methods ORDER BY id").fetchall()
        text = "Payment Methods:\n" + "\n".join(f"{m['display_name']}: {m['details']}" for m in methods)
        rows = [[(f"Edit {m['display_name']}", f"payment:{m['method_key']}")] for m in methods]
        rows.append([("🔙 Back", "admin"), ("🏠 Main Menu", "menu")])
        self.api.send_message(chat_id, text, keyboard(rows))

    def start_payment_setup(self, chat_id: int, user_id: int, method_key: str) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "payment_details", json.dumps({"method_key": method_key}))
        self.api.send_message(chat_id, "এই payment method-এর বর্তমান account/details লিখুন:", keyboard([[(CANCEL, "cancel")]]))

    def start_broadcast(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "broadcast")
        self.api.send_message(chat_id, "Broadcast message লিখুন:", keyboard([[(CANCEL, "cancel")]]))

    def show_orders(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        rows = self.db.connection.execute(
            """
            SELECT o.*, p.name product_name, b.telegram_user_id buyer_telegram_id
            FROM orders o JOIN products p ON p.id=o.product_id
            JOIN users b ON b.id=o.buyer_user_id ORDER BY o.id DESC LIMIT 20
            """
        ).fetchall()
        text = "Recent Orders:\n" + "\n".join(
            f"#{o['id']} {o['product_name']} x{o['quantity']} | buyer {o['buyer_telegram_id']} | {money(o['total_cents'])}"
            for o in rows
        ) if rows else "No orders yet."
        self.api.send_message(chat_id, text, keyboard([[("🔙 Back", "admin"), ("🏠 Main Menu", "menu")]]))


def main() -> None:
    settings = load_settings()
    bot = MarketplaceBot(settings)
    try:
        bot.run()
    finally:
        bot.db.close()


if __name__ == "__main__":
    main()