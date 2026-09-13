from __future__ import annotations

import json
import html
import sqlite3
import uuid
from typing import Any

from config import Settings, load_settings
from db import Database
from telegram_api import TelegramApi


CANCEL = "🔴 Cancel"
FALLBACK = "Invalid input. Please use the correct buttons or commands provided."
BDT_PER_USDT = 125


def money(cents: int, language: str = "bn") -> str:
    if language == "en":
        return f"{cents / 100 / BDT_PER_USDT:.2f} USDT"
    return f"{cents / 100:.2f} Tk"


def copyable_details(details: str) -> str:
    clean = details.strip()
    return f"<code>{html.escape(clean)}</code>"


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


def url_keyboard(label: str, url: str, back_data: str = "menu") -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "url": url}],
            [{"text": "🔵 🏠 Main Menu", "callback_data": back_data}],
        ]
    }


def reply_keyboard(rows: list[list[str]]) -> dict[str, Any]:
    return {
        "keyboard": [[{"text": label} for label in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Choose an option or type a command",
    }


class MarketplaceBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = TelegramApi(settings.bot_token)
        self.db = Database(settings.db_path)
        self.db.seed_defaults()
        self.last_bot_messages: dict[int, int] = {}
        self.active_callback_messages: dict[int, int] = {}
        self.api.set_my_commands(
            [
                {"command": "start", "description": "Open the marketplace"},
                {"command": "language", "description": "Switch Bengali / English"},
            ]
        )

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
                        self.send(chat_id, FALLBACK)

    @staticmethod
    def chat_id(update: dict[str, Any]) -> int | None:
        message = update.get("message") or update.get("callback_query", {}).get("message")
        return message.get("chat", {}).get("id") if message else None

    def t(self, chat_id: int, bn: str, en: str) -> str:
        try:
            return bn if self.db.get_user(chat_id)["language"] == "bn" else en
        except ValueError:
            return bn

    def amount(self, chat_id: int, cents: int) -> str:
        return money(cents, self.db.get_user(chat_id)["language"])

    def currency_name(self, chat_id: int) -> str:
        return "USDT" if self.db.get_user(chat_id)["language"] == "en" else "Tk"

    def delete_previous(self, chat_id: int) -> None:
        message_id = self.last_bot_messages.pop(chat_id, None)
        if message_id:
            self.api.delete_message(chat_id, message_id)

    def send(
        self,
        chat_id: int,
        text: str,
        markup: dict[str, Any] | None = None,
        *,
        parse_mode: str | None = None,
        replace: bool = True,
        track: bool = True,
    ) -> int:
        active_message_id = self.active_callback_messages.get(chat_id) if replace else None
        if active_message_id:
            try:
                self.api.edit_message(chat_id, active_message_id, text, markup, parse_mode)
                self.last_bot_messages[chat_id] = active_message_id
                self.active_callback_messages.pop(chat_id, None)
                return active_message_id
            except Exception:
                self.active_callback_messages.pop(chat_id, None)
        if replace:
            self.delete_previous(chat_id)
        message_id = self.api.send_message(chat_id, text, markup, parse_mode)
        if track:
            self.last_bot_messages[chat_id] = message_id
        return message_id

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if not message or "from" not in message:
            return
        user = message["from"]
        chat_id = int(message["chat"]["id"])
        self.db.ensure_user(user["id"], user.get("username"), user.get("first_name", ""))
        text = message.get("text", "").strip()
        if text in ("/start", "/menu"):
            self.db.clear_state(chat_id)
            self.send_welcome(chat_id)
            return
        if text == "/language":
            self.start_language(chat_id)
            return
        if text.startswith("/getid"):
            self.handle_getid(chat_id, text)
            return
        if text in ("বাংলা", "English"):
            self.set_language(chat_id, text)
            return
        reply_action = self.reply_action(text, chat_id)
        if reply_action:
            self.db.clear_state(chat_id)
            self.handle_menu_action(chat_id, reply_action)
            return
        if text in ("/cancel", CANCEL, "Cancel", "❌ Cancel"):
            self.cancel(chat_id)
            return
        state = self.db.get_state(chat_id)
        if state:
            self.handle_input(chat_id, text, state["state"], json.loads(state["data_json"]))
        else:
            self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))

    def reply_action(self, text: str, chat_id: int) -> str | None:
        mapping = {
            "💳 Balance": "balance",
            "💳 ব্যালেন্স": "balance",
            "🛍 Marketplace": "market",
            "🛍 মার্কেটপ্লেস": "market",
            "➕ Deposit": "deposit",
            "➕ ডিপোজিট": "deposit",
            "↗️ Withdrawal": "withdraw",
            "↗️ উত্তোলন": "withdraw",
            "👤 Profile": "profile",
            "👤 প্রোফাইল": "profile",
            "📜 My Orders": "my_orders",
            "📜 আমার অর্ডার": "my_orders",
            "💰 History": "history",
            "💰 হিস্টোরি": "history",
            "🆘 Support": "support",
            "🆘 সাপোর্ট": "support",
            "⚙️ Admin Panel": "admin",
            "⚙️ অ্যাডমিন প্যানেল": "admin",
        }
        action = mapping.get(text)
        if action == "admin" and chat_id != self.settings.admin_user_id:
            return None
        return action

    def handle_menu_action(self, chat_id: int, action: str) -> None:
        if action == "balance":
            self.show_balance(chat_id)
        elif action == "market":
            self.show_categories(chat_id)
        elif action == "deposit":
            self.start_deposit(chat_id)
        elif action == "withdraw":
            self.start_withdrawal(chat_id)
        elif action == "profile":
            self.show_profile(chat_id)
        elif action == "my_orders":
            self.show_my_orders(chat_id)
        elif action == "history":
            self.show_my_transactions(chat_id)
        elif action == "support":
            self.show_support(chat_id)
        elif action == "admin":
            self.show_admin_menu(chat_id, chat_id)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        user = callback["from"]
        message = callback["message"]
        chat_id = int(message["chat"]["id"])
        data = callback.get("data", "")
        self.active_callback_messages[chat_id] = int(message["message_id"])
        self.db.ensure_user(user["id"], user.get("username"), user.get("first_name", ""))
        self.api.answer_callback(callback["id"])
        if data in ("cancel", "back"):
            self.cancel(chat_id)
        elif data == "menu":
            self.db.clear_state(chat_id)
            self.send_main_menu(chat_id)
        elif data == "balance":
            self.show_balance(chat_id)
        elif data == "market":
            self.show_categories(chat_id)
        elif data == "profile":
            self.show_profile(chat_id)
        elif data == "my_orders":
            self.show_my_orders(chat_id)
        elif data == "history":
            self.show_my_transactions(chat_id)
        elif data.startswith("category:"):
            self.show_options(chat_id, int(data.split(":")[1]))
        elif data.startswith("option:"):
            self.show_option(chat_id, int(data.split(":")[1]))
        elif data.startswith("variant:"):
            _, option_id, price_cents = data.split(":")
            self.start_purchase(chat_id, int(option_id), int(price_cents))
        elif data.startswith("buy:"):
            self.start_purchase(chat_id, int(data.split(":")[1]))
        elif data == "deposit":
            self.start_deposit(chat_id)
        elif data.startswith("deposit_method:"):
            self.select_deposit_method(chat_id, data.split(":", 1)[1])
        elif data == "withdraw":
            self.start_withdrawal(chat_id)
        elif data.startswith("withdraw_method:"):
            self.select_withdrawal_method(chat_id, data.split(":", 1)[1])
        elif data in {
            "deposit_confirm",
            "withdraw_confirm",
            "purchase_confirm",
            "stock_confirm",
            "broadcast_confirm",
            "balance_confirm",
        }:
            self.confirm_from_callback(chat_id, data)
        elif data == "support":
            self.show_support(chat_id)
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
        elif data == "admin:new_category":
            self.start_new_category(chat_id, user["id"])
        elif data.startswith("admin:category:rename:"):
            self.start_category_rename(chat_id, user["id"], int(data.split(":")[3]))
        elif data.startswith("admin:category:delete_confirm:"):
            self.delete_category(chat_id, user["id"], int(data.split(":")[3]))
        elif data.startswith("admin:category:delete:"):
            self.confirm_category_delete(chat_id, user["id"], int(data.split(":")[3]))
        elif data.startswith("admin:category:"):
            self.admin_category(chat_id, user["id"], int(data.split(":")[2]))
        elif data.startswith("admin:new_option:"):
            self.start_new_option(chat_id, user["id"], int(data.split(":")[2]))
        elif data.startswith("admin:option:rename:"):
            self.start_option_rename(chat_id, user["id"], int(data.split(":")[3]))
        elif data.startswith("admin:option:delete_confirm:"):
            self.delete_option(chat_id, user["id"], int(data.split(":")[3]))
        elif data.startswith("admin:option:delete:"):
            self.confirm_option_delete(chat_id, user["id"], int(data.split(":")[3]))
        elif data.startswith("admin:option:"):
            self.admin_option(chat_id, user["id"], int(data.split(":")[2]))
        elif data.startswith("admin:add_stock:"):
            self.start_stock_upload(chat_id, user["id"], int(data.split(":")[2]))
        elif data.startswith("admin:variant:edit:"):
            _, _, _, option_id, price_cents = data.split(":")
            self.start_variant_price_edit(chat_id, user["id"], int(option_id), int(price_cents))
        elif data.startswith("admin:variant:remove:"):
            _, _, _, option_id, price_cents = data.split(":")
            self.start_variant_stock_remove(chat_id, user["id"], int(option_id), int(price_cents))
        elif data.startswith("admin:variant:clear_confirm:"):
            _, _, _, option_id, price_cents = data.split(":")
            self.clear_variant_stock(chat_id, user["id"], int(option_id), int(price_cents))
        elif data.startswith("admin:variant:clear:"):
            _, _, _, option_id, price_cents = data.split(":")
            self.confirm_variant_clear(chat_id, user["id"], int(option_id), int(price_cents))
        elif data.startswith("admin:variant:"):
            _, _, option_id, price_cents = data.split(":")
            self.admin_price_variant(chat_id, user["id"], int(option_id), int(price_cents))
        elif data.startswith("stock_owner:"):
            self.select_stock_owner(chat_id, user["id"], data.split(":", 1)[1])
        elif data == "admin:payment":
            self.show_payment_setup(chat_id, user["id"])
        elif data == "admin:commission":
            self.show_commission_setup(chat_id, user["id"])
        elif data == "commission:edit":
            self.start_commission_setup(chat_id, user["id"])
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
        elif data.startswith("balance_mode:"):
            self.select_balance_mode(chat_id, user["id"], data)

    def main_reply_keyboard(self, chat_id: int) -> dict[str, Any]:
        english = self.db.get_user(chat_id)["language"] == "en"
        rows = [
            [
                "💳 Balance" if english else "💳 ব্যালেন্স",
                "🛍 Marketplace" if english else "🛍 মার্কেটপ্লেস",
            ],
            [
                "➕ Deposit" if english else "➕ ডিপোজিট",
                "↗️ Withdrawal" if english else "↗️ উত্তোলন",
            ],
            [
                "👤 Profile" if english else "👤 প্রোফাইল",
                "📜 My Orders" if english else "📜 আমার অর্ডার",
            ],
            [
                "💰 History" if english else "💰 হিস্টোরি",
                "🆘 Support" if english else "🆘 সাপোর্ট",
            ],
        ]
        if chat_id == self.settings.admin_user_id:
            rows.append(["⚙️ Admin Panel" if english else "⚙️ অ্যাডমিন প্যানেল"])
        return reply_keyboard(rows)

    def send_welcome(self, chat_id: int) -> None:
        message = self.t(
            chat_id,
            "<b>স্বাগতম Telegram Marketplace-এ</b>\n\n"
            "এখানে আপনি নিরাপদে account products দেখতে, balance যোগ করতে, "
            "কেনাকাটা করতে এবং withdrawal request দিতে পারবেন।\n\n"
            "শুরু করতে নিচের menu ব্যবহার করুন। ভাষা বদলাতে /language লিখুন।",
            "<b>Welcome to Telegram Marketplace</b>\n\n"
            "Browse account products, add balance, purchase stock, and request withdrawals securely.\n\n"
            "Use the menu below to get started. Use /language to switch language.",
        )
        self.send(chat_id, message, self.main_reply_keyboard(chat_id), parse_mode="HTML")

    def send_main_menu(self, chat_id: int) -> None:
        self.send(
            chat_id,
            self.t(chat_id, "একটি option নির্বাচন করুন।", "Choose an option."),
            self.main_reply_keyboard(chat_id),
        )

    def start_language(self, chat_id: int) -> None:
        self.send(
            chat_id,
            "ভাষা নির্বাচন করুন / Choose your language:",
            reply_keyboard([["বাংলা", "English"]]),
        )

    def set_language(self, chat_id: int, selection: str) -> None:
        language = "bn" if selection == "বাংলা" else "en"
        self.db.set_language(chat_id, language)
        self.send(
            chat_id,
            "ভাষা বাংলা করা হয়েছে।" if language == "bn" else "Language changed to English.",
            self.main_reply_keyboard(chat_id),
        )

    def handle_getid(self, chat_id: int, text: str) -> None:
        if chat_id != self.settings.admin_user_id:
            self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            self.send(chat_id, "Usage: /getid @username", self.main_reply_keyboard(chat_id))
            return
        user = self.db.find_user(parts[1])
        if not user:
            self.send(chat_id, "User not found. The user must send /start first.", self.main_reply_keyboard(chat_id))
            return
        self.send(
            chat_id,
            f"Username: @{user['username'] or '-'}\nTelegram UID: {user['telegram_user_id']}",
            self.main_reply_keyboard(chat_id),
        )

    def show_balance(self, chat_id: int) -> None:
        user = self.db.get_user(chat_id)
        self.send(
            chat_id,
            "💰 <b>Balance</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💳 Available: {self.amount(chat_id, user['balance_cents'])}\n"
            f"🔒 Reserved withdrawals: {self.amount(chat_id, user['reserved_cents'])}",
            inline_keyboard(
                [
                    [
                        ("🔵 👤 Profile", "profile"),
                        ("🔵 💰 History", "history"),
                    ],
                    [("🔵 🏠 Main Menu", "menu")],
                ]
            ),
            parse_mode="HTML",
        )

    def show_categories(self, chat_id: int) -> None:
        categories = self.db.connection.execute(
            """
            SELECT c.id, c.name,
              COALESCE(SUM(CASE WHEN i.status='available' THEN 1 ELSE 0 END), 0) stock
            FROM categories c
            LEFT JOIN options o ON o.category_id=c.id AND o.is_deleted=0
            LEFT JOIN inventory_batches b ON b.option_id=o.id
            LEFT JOIN inventory_items i ON i.batch_id=b.id
            WHERE c.is_deleted=0
            GROUP BY c.id ORDER BY c.name
            """
        ).fetchall()
        if not categories:
            self.send(chat_id, "📦 Marketplace is currently empty.", self.main_reply_keyboard(chat_id))
            return
        rows = [[(f"🔵 {row['name']} — Stock: {row['stock']}", f"category:{row['id']}")] for row in categories]
        rows.append([("🔵 🏠 Main Menu", "menu")])
        self.send(chat_id, "🏷️ <b>Categories</b>\n━━━━━━━━━━━━━━━━━━\nChoose a category:", inline_keyboard(rows), parse_mode="HTML")

    def show_options(self, chat_id: int, category_id: int) -> None:
        category = self.db.connection.execute(
            "SELECT * FROM categories WHERE id=? AND is_deleted=0", (category_id,)
        ).fetchone()
        if not category:
            self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
            return
        options = self.db.connection.execute(
            """
            SELECT o.id, o.name,
              COALESCE(SUM(CASE WHEN i.status='available' THEN 1 ELSE 0 END), 0) stock
            FROM options o
            LEFT JOIN inventory_batches b ON b.option_id=o.id
            LEFT JOIN inventory_items i ON i.batch_id=b.id
            WHERE o.category_id=? AND o.is_deleted=0 GROUP BY o.id ORDER BY o.name
            """,
                (category_id,),
).fetchall()
if not options:
    self.send(
        chat_id,
        "📦 Stock: 0\nCurrently unavailable.",
                    inline_keyboard(
                [
                    [("🟢 🏷️ Categories", "market"), ("🔵 🏠 Main Menu", "menu")],
                ]
            ),
        )
        return
    rows = [[(f"🟢 {row['name']} — Stock: {row['stock']}", f"option:{row['id']}")] for row in options]
    rows.append([("🔵 🏷️ Categories", "market"), ("🔵 🏠 Main Menu", "menu")])
    self.send(
        chat_id,
        f"🏷️ <b>Category: {html.escape(category['name'])}</b>\n━━━━━━━━━━━━━━━━━━",
        inline_keyboard(rows),
        parse_mode="HTML",
    )

def show_option(self, chat_id: int, option_id: int) -> None:
    option = self.db.option(option_id)
    if not option:
        self.send(chat_id, "Option not found.", self.main_reply_keyboard(chat_id))
        return
    variants = self.db.option_price_variants(option_id)
    if not variants:
        self.send(
            chat_id,
            f"📦 <b>{html.escape(option['category_name'])} / "
            f"{html.escape(option['name'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━\nAvailable Stock: 0\nCurrently unavailable.",
            inline_keyboard(
                [
                    [("🟢 🏷️ Categories", f"category:{option['category_id']}")],
                    [("🔵 🏠 Main Menu", "menu")],
                ]
            ),
            parse_mode="HTML",
        )
        return
    rows = [
        [
            (
                f"🟢 {option['name']} - {self.amount(chat_id, row['price_cents'])} "
                f"(Stock: {row['available_stock']})",
                f"variant:{option_id}:{row['price_cents']}",
            )
        ]
        for row in variants
    ]
    rows.append([("🔵 🏷️ Categories", f"category:{option['category_id']}"), ("🔵 🏠 Main Menu", "menu")])
    self.send(
        chat_id,
        f"📦 <b>{html.escape(option['category_name'])} / "
        f"{html.escape(option['name'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\nChoose a price variant:",
        inline_keyboard(rows),
        parse_mode="HTML",
    )

def start_purchase(self, chat_id: int, option_id: int, price_cents: int | None = None) -> None:
    option = self.db.option(option_id)
    if not option or option["available_stock"] <= 0:
        self.send(chat_id, "📦 Available Stock: 0\nCurrently unavailable.", self.main_reply_keyboard(chat_id))
        return
    variants = self.db.option_price_variants(option_id)
    if price_cents is None:
        if len(variants) != 1:
            self.show_option(chat_id, option_id)
            return
        price_cents = variants[0]["price_cents"]
    selected = next((row for row in variants if row["price_cents"] == price_cents), None)
    if selected is None or selected["available_stock"] <= 0:
        self.send(chat_id, "📦 That price variant is currently unavailable.", self.main_reply_keyboard(chat_id))
        return
    self.db.set_state(
        chat_id,
        "purchase_quantity",
        json.dumps({"option_id": option_id, "price_cents": price_cents}),
    )
    self.send(
        chat_id,
        f"🏷️ {option['name']} - {self.amount(chat_id, price_cents)}\n"
        f"Available Stock: {selected['available_stock']}\nEnter how many you want to buy:",
        inline_keyboard([[("🔴 Cancel", "cancel")]]),
    )

def start_deposit(self, chat_id: int) -> None:
    methods = self.db.connection.execute(
        """
        SELECT * FROM payment_methods
        WHERE is_active=1 AND method_key IN ('bkash', 'binance')
        ORDER BY id
        """
    ).fetchall()
    rows = [[(f"🟢 {method['display_name']}", f"deposit_method:{method['method_key']}")] for method in methods]
    rows.append([("🔴 Cancel", "cancel")])
    self.db.set_state(chat_id, "deposit_method")
    self.send(chat_id, "Choose deposit method:", inline_keyboard(rows))

def select_deposit_method(self, chat_id: int, method_key: str) -> None:
    method = self.db.connection.execute(
        "SELECT * FROM payment_methods WHERE method_key=? AND is_active=1", (method_key,)
    ).fetchone()
    if not method:
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    self.db.set_state(chat_id, "deposit_amount", json.dumps({"method_key": method_key}))
    self.send(
        chat_id,
        f"💳 <b>{method['display_name']} Deposit Details</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{copyable_details(method['details'])}\n\n"
        f"Enter deposit amount in {self.currency_name(chat_id)}:",
        inline_keyboard([[("🔴 Cancel", "cancel")]]),
        parse_mode="HTML",
    )

def start_withdrawal(self, chat_id: int) -> None:
    self.db.set_state(chat_id, "withdraw_method")
    self.send(
        chat_id,
        "Choose withdrawal method:",
        inline_keyboard(
            [
                [("🟢 bKash", "withdraw_method:bkash"), ("🟢 Binance", "withdraw_method:binance")],
                [("🔴 Cancel", "cancel")],
            ]
        ),
    )

def select_withdrawal_method(self, chat_id: int, method_key: str) -> None:
    labels = {"bkash": "Bkash number", "binance": "Binance wallet address"}
    if method_key not in labels:
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    self.db.set_state(chat_id, "withdraw_amount", json.dumps({"method_key": method_key}))
    self.send(
        chat_id,
        f"Enter withdrawal amount in {self.currency_name(chat_id)}.\n"
        f"Then enter your {labels[method_key]}.",
        inline_keyboard([[("🔴 Cancel", "cancel")]]),
    )

def show_support(self, chat_id: int) -> None:
    self.send(
        chat_id,
        "Need help? Click below to contact the Support Admin directly.",
        url_keyboard("🟢 💬 Click here to contact Support Admin", "https://t.me/Sojib31_4"),
)
def handle_input(self, chat_id: int, text: str, state: str, data: dict[str, Any]) -> None:
    if state == "deposit_amount":
        amount = self.parse_amount(text, chat_id)
        if amount is None:
            self.send(chat_id, "❌ Enter a valid positive number.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        data["amount_cents"] = amount
        self.db.set_state(chat_id, "deposit_txid", json.dumps(data))
        self.send(chat_id, "Enter payment TXID:", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "deposit_txid":
        data["txid"] = text
        self.db.set_state(chat_id, "deposit_confirm", json.dumps(data))
        self.send(
            chat_id,
            f"Deposit preview:\nMethod: {data['method_key']}\nAmount: {self.amount(chat_id, data['amount_cents'])}\nTXID: {text}\n\nConfirm?",
            inline_keyboard([[("🟢 ✅ Confirm", "deposit_confirm"), ("🔴 Cancel", "cancel")]]),
        )
    elif state == "withdraw_amount":
        amount = self.parse_amount(text, chat_id)
        user = self.db.get_user(chat_id)
        if amount is None:
            self.send(chat_id, "❌ Enter a valid positive number.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        if amount > user["balance_cents"]:
            self.send(
                chat_id,
                f"❌ Required: {self.amount(chat_id, amount)}\n"
                f"Current balance: {self.amount(chat_id, user['balance_cents'])}\nTry again.",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
            return
        data["amount_cents"] = amount
        prompt = {
            "bkash": "Enter your Bkash number",
            "nagad": "Enter your Nagad number",
            "binance": "Enter your Binance address",
        }[data["method_key"]]
        self.db.set_state(chat_id, "withdraw_details", json.dumps(data))
        self.send(chat_id, prompt, inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "withdraw_details":
        data["payout_details"] = text
        self.db.set_state(chat_id, "withdraw_confirm", json.dumps(data))
        self.send(
            chat_id,
            f"Withdrawal preview:\nMethod: {data['method_key']}\nAmount: {self.amount(chat_id, data['amount_cents'])}\nDetails: {text}\n\nConfirm?",
            inline_keyboard([[("🟢 ✅ Confirm", "withdraw_confirm"), ("🔴 Cancel", "cancel")]]),
        )
    elif state == "purchase_quantity":
        if not text.isdigit() or int(text) <= 0:
            self.send(chat_id, "❌ Enter a valid whole number.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        try:
            quote = self.db.quote_purchase(
                data["option_id"],
                int(text),
                self.settings.admin_user_id,
                data["price_cents"],
            )
        except ValueError as error:
            self.send(chat_id, f"❌ {error}", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        user = self.db.get_user(chat_id)
        if quote["total_cents"] > user["balance_cents"]:
            self.send(
                chat_id,
                f"❌ Required: {self.amount(chat_id, quote['total_cents'])}\n"
                f"Current balance: {self.amount(chat_id, user['balance_cents'])}",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
            return
        data.update(
            quantity=quote["quantity"],
            total_cents=quote["total_cents"],
            request_key=f"market:{chat_id}:{uuid.uuid4().hex}",
        )
        self.db.set_state(chat_id, "purchase_confirm", json.dumps(data))
        self.send(
            chat_id,
            f"You are buying {quote['quantity']} accounts for {self.amount(chat_id, quote['total_cents'])}. Confirm?",
            inline_keyboard([[("🟢 ✅ Confirm", "purchase_confirm"), ("🔴 Cancel", "cancel")]]),
        )
    elif state == "category_name":
        try:
            category = self.db.create_category(text)
            self.db.clear_state(chat_id)
            self.admin_category(chat_id, chat_id, category["id"])
        except ValueError as error:
            self.send(chat_id, f"❌ {error}\nTry another name.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "category_rename":
        try:
            category = self.db.rename_category(data["category_id"], text)
            self.db.clear_state(chat_id)
            self.admin_category(chat_id, chat_id, category["id"])
        except ValueError as error:
            self.send(chat_id, f"❌ {error}\nTry another name.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "option_name":
        try:
            option = self.db.create_option(data["category_id"], text)
            self.db.clear_state(chat_id)
            self.admin_option(chat_id, chat_id, option["id"])
        except ValueError as error:
            self.send(chat_id, f"❌ {error}\nTry another name.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "option_rename":
        try:
            option = self.db.rename_option(data["option_id"], text)
            self.db.clear_state(chat_id)
            self.admin_option(chat_id, chat_id, option["id"])
        except ValueError as error:
            self.send(chat_id, f"❌ {error}\nTry another name.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "stock_owner_uid":
        if not text.isdigit() or not self.db.find_user(text):
            self.send(chat_id, "❌ User must send /start first. Enter a valid UID.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        data["owner_telegram_user_id"] = int(text)
        self.db.set_state(chat_id, "stock_lines", json.dumps(data))
        self.send(
            chat_id,
            "Send the accounts separated by `#` (hashtag).",
            inline_keyboard([[("🔴 Cancel", "cancel")]]),
        )
    elif state == "stock_lines":
        lines = [account.strip() for account in text.split("#") if account.strip()]
        if not lines:
            self.send(
                chat_id,
                "❌ Send at least one account separated by `#`.",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
            return
        data["account_lines"] = lines
        self.db.set_state(chat_id, "stock_price", json.dumps(data))
        self.send(chat_id, f"{len(lines)} account(s) received. Enter price per account:", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "stock_price":
        amount = self.parse_amount(text, chat_id)
        if amount is None:
            self.send(chat_id, "❌ Enter a valid positive price.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        data["price_cents"] = amount
        self.db.set_state(chat_id, "stock_confirm", json.dumps(data))
        self.send(
            chat_id,
            f"Stock preview:\nAccounts: {len(data['account_lines'])}\n"
            f"Price/account: {self.amount(chat_id, amount)}\n"
            f"Owner UID: {data['owner_telegram_user_id']}\n\nConfirm upload?",
            inline_keyboard([[("🟢 ✅ Confirm", "stock_confirm"), ("🔴 Cancel", "cancel")]]),
        )
    elif state == "variant_price":
        amount = self.parse_amount(text, chat_id)
        if amount is None:
            self.send(
                chat_id,
                "❌ Enter a valid positive price.",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
            return
        try:
            changed = self.db.update_variant_price(
                data["option_id"], data["old_price_cents"], amount
            )
            self.db.clear_state(chat_id)
            self.send(
                chat_id,
                f"✅ Updated the price for {changed} available item(s).",
                inline_keyboard([[("🟡 📦 Product Management", f"admin:option:{data['option_id']}")]]),
            )
        except ValueError as error:
            self.send(
                chat_id,
                f"❌ {error}",
                inline_keyboard(
                    [[("🔵 🔙 Back", f"admin:variant:{data['option_id']}:{data['old_price_cents']}")]]
                ),
            )
    elif state == "variant_remove_quantity":
        if not text.isdigit() or int(text) <= 0:
            self.send(
                chat_id,
                "❌ Enter a valid whole-number quantity.",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
            return
        try:
            removed = self.db.remove_variant_stock(
                data["option_id"], data["price_cents"], int(text)
            )
            self.db.clear_state(chat_id)
            self.send(
                chat_id,
                f"✅ Removed {removed} available stock item(s) from this price pool.",
                inline_keyboard([[("🟡 📦 Product Management", f"admin:option:{data['option_id']}")]]),
            )
        except ValueError as error:
            self.send(
                chat_id,
                f"❌ {error}",
                inline_keyboard(
                    [[("🔵 🔙 Back", f"admin:variant:{data['option_id']}:{data['price_cents']}")]]
                ),
            )
    elif state == "balance_user":
        user = self.db.find_user(text)
        if not user:
            self.send(chat_id, "❌ Username/UID not found. The user must send /start first.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        data["target_telegram_user_id"] = user["telegram_user_id"]
        username = f"@{user['username']}" if user["username"] else str(user["telegram_user_id"])
        self.db.set_state(chat_id, "balance_user", json.dumps(data))
        self.send(
            chat_id,
            f"User {username} found.\nCurrent Balance: {self.amount(chat_id, user['balance_cents'])}",
            inline_keyboard(
                [
                    [("🟢 ➕ Add Balance", f"balance_mode:add:{user['telegram_user_id']}")],
                    [("🔴 ➖ Deduct Balance", f"balance_mode:deduct:{user['telegram_user_id']}")],
                    [("🔴 Cancel", "cancel")],
                ]
            ),
        )
    elif state == "balance_amount":
        amount = self.parse_amount(text, chat_id)
        if amount is None:
            self.send(chat_id, "❌ Enter a valid number.", inline_keyboard([[("🔴 Cancel", "cancel")]]))
            return
        data["amount_cents"] = amount if data["direction"] == "add" else -amount
        self.db.set_state(chat_id, "balance_note", json.dumps(data))
        action = "add" if data["direction"] == "add" else "deduct"
        self.send(chat_id, f"Enter a note for this {action} adjustment:", inline_keyboard([[("🔴 Cancel", "cancel")]]))
    elif state == "balance_note":
        data["note"] = text
        self.db.set_state(chat_id, "balance_confirm", json.dumps(data))
        self.send(
            chat_id,
            f"Balance adjustment:\nUser: {data['target_telegram_user_id']}\n"
            f"Amount: {self.amount(chat_id, data['amount_cents'])}\nNote: {text}\n\nConfirm?",
            inline_keyboard([[("🟢 ✅ Confirm", "balance_confirm"), ("🔴 Cancel", "cancel")]]),
        )
    elif state == "payment_details":
        self.db.connection.execute(
            "UPDATE payment_methods SET details=?, updated_at=CURRENT_TIMESTAMP WHERE method_key=?",
            (text, data["method_key"]),
        )
        self.db.connection.commit()
        self.db.clear_state(chat_id)
        self.show_payment_setup(chat_id, chat_id)
    elif state == "commission_percent":
        if not text.isdigit() or not 0 <= int(text) <= 100:
            self.send(
                chat_id,
                "❌ Enter a whole-number commission from 0 to 100.",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
            return
        self.db.set_vendor_commission_percent(int(text))
        self.db.clear_state(chat_id)
        self.show_commission_setup(chat_id, chat_id)
    elif state == "broadcast":
        self.db.set_state(chat_id, "broadcast_confirm", json.dumps({"message": text}))
        self.send(
            chat_id,
            f"Broadcast preview:\n\n{text}\n\nSend?",
            inline_keyboard([[("🟣 ✅ Send Broadcast", "broadcast_confirm"), ("🔴 Cancel", "cancel")]]),
        )

def parse_amount(self, text: str, chat_id: int) -> int | None:
    try:
        value = float(text.replace(",", ""))
        if self.db.get_user(chat_id)["language"] == "en":
            value *= BDT_PER_USDT
        cents = round(value * 100)
        return cents if cents > 0 else None
    except ValueError:
        return None

def confirm_from_callback(self, chat_id: int, action: str) -> None:
    state = self.db.get_state(chat_id)
    if not state or state["state"] != action:
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    self.handle_confirm(chat_id, action, json.loads(state["data_json"]))

def handle_confirm(self, chat_id: int, state: str, data: dict[str, Any]) -> None:
    if state == "deposit_confirm":
        try:
            deposit = self.db.create_deposit(chat_id, data["method_key"], data["amount_cents"], data["txid"])
            self.db.clear_state(chat_id)
            self.send(chat_id, f"✅ Deposit order #{deposit['id']} created. Awaiting admin approval.", self.main_reply_keyboard(chat_id))
        except ValueError as error:
            self.db.clear_state(chat_id)
            self.send(chat_id, f"❌ {error}", self.main_reply_keyboard(chat_id))
    elif state == "withdraw_confirm":
        try:
            item = self.db.create_withdrawal(chat_id, data["method_key"], data["amount_cents"], data["payout_details"])
            self.db.clear_state(chat_id)
            self.send(chat_id, f"✅ Withdrawal request #{item['id']} created. Amount reserved until admin processing.", self.main_reply_keyboard(chat_id))
        except ValueError as error:
            self.db.clear_state(chat_id)
            self.send(chat_id, f"❌ {error}", self.main_reply_keyboard(chat_id))
    elif state == "purchase_confirm":
        try:
            commission_percent = self.db.get_vendor_commission_percent()
            order, accounts = self.db.create_market_purchase(
                chat_id,
                data["option_id"],
                data["quantity"],
                data["request_key"],
                self.settings.admin_user_id,
                data["total_cents"],
                data["price_cents"],
                commission_percent,
            )
            self.db.clear_state(chat_id)
            for payout in self.db.market_order_vendor_payouts(
                order["id"], self.settings.admin_user_id, commission_percent
            ):
                try:
                    self.send(
                        payout["telegram_user_id"],
                        "🎉 <b>Product Sold</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"📦 Product: {html.escape(str(payout['option_name']))}\n"
                        f"{self.amount(payout['telegram_user_id'], payout['amount_cents'])} "
                        "credited to your balance after commission.\n"
                        f"💰 Current Balance: {self.amount(payout['telegram_user_id'], payout['balance_cents'])}",
                        self.main_reply_keyboard(payout["telegram_user_id"]),
                        parse_mode="HTML",
                    )
                except Exception as error:
                    print(
                        f"Vendor notification failed for {payout['telegram_user_id']}: {error}"
                    )
            self.send(
                chat_id,
                "✅ <b>Purchase Successful</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🧾 Order: #{order['id']}\n"
                f"Total: {self.amount(chat_id, order['total_cents'])}",
                self.main_reply_keyboard(chat_id),
                track=False,
                parse_mode="HTML",
            )
            delivery = "\n".join(
                f"🔑 <b>Account {index}</b>: <code>{html.escape(account)}</code>"
                for index, account in enumerate(accounts, start=1)
            )
            self.send(
                chat_id,
                "🔑 <b>Purchased Credentials</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{delivery}",
                self.main_reply_keyboard(chat_id),
                replace=False,
                parse_mode="HTML",
            )
        except ValueError as error:
            self.db.clear_state(chat_id)
            self.send(chat_id, f"❌ Purchase failed: {error}", self.main_reply_keyboard(chat_id))
    elif state == "stock_confirm":
        try:
            batch = self.db.add_inventory_batch(
                data["option_id"],
                data["owner_telegram_user_id"],
                data["price_cents"],
                data["account_lines"],
            )
            context = self.db.inventory_batch_context(batch["id"])
            alert = (
                "📢 <b>New Stock Alert!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Category: {html.escape(str(context['category_name']))}\n"
                f"Variant: {html.escape(str(context['option_name']))}\n"
                f"Price: {context['price_cents'] / 100:.2f} Tk\n"
                f"Available Stock: {context['item_count']} pcs added!"
            )
            for row in self.db.connection.execute(
                "SELECT telegram_user_id FROM users"
            ).fetchall():
                try:
                    self.send(
                        row["telegram_user_id"],
                        alert,
                        self.main_reply_keyboard(row["telegram_user_id"]),
                        replace=False,
                        track=False,
                        parse_mode="HTML",
                    )
                except Exception as error:
                    print(f"New stock notification failed for {row['telegram_user_id']}: {error}")
            self.db.clear_state(chat_id)
            self.send(chat_id, f"✅ Stock uploaded. Added {batch['item_count']} account(s).", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin"), ("🔵 🏠 Main Menu", "menu")]]))
        except ValueError as error:
            self.db.clear_state(chat_id)
            self.send(chat_id, f"❌ Stock upload failed: {error}", self.main_reply_keyboard(chat_id))
    elif state == "balance_confirm":
        try:
            user = self.db.adjust_balance(data["target_telegram_user_id"], data["amount_cents"], data["note"])
            self.db.clear_state(chat_id)
            self.send(chat_id, f"✅ Balance updated.\nCurrent balance: {self.amount(chat_id, user['balance_cents'])}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
            self.send(user["telegram_user_id"], f"Your balance was adjusted by admin.\nCurrent balance: {self.amount(user['telegram_user_id'], user['balance_cents'])}", self.main_reply_keyboard(user["telegram_user_id"]))
        except ValueError as error:
            self.db.clear_state(chat_id)
            self.send(chat_id, f"❌ Balance adjustment failed: {error}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
    elif state == "broadcast_confirm":
        sent = 0
        for row in self.db.connection.execute("SELECT telegram_user_id FROM users").fetchall():
            try:
                self.send(row["telegram_user_id"], data["message"], self.main_reply_keyboard(row["telegram_user_id"]), replace=False, track=False)
                sent += 1
            except Exception as error:
                print(f"Broadcast failed for {row['telegram_user_id']}: {error}")
        self.db.clear_state(chat_id)
        self.send(chat_id, f"✅ Broadcast complete. Sent: {sent}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))

def cancel(self, chat_id: int) -> None:
    self.db.clear_state(chat_id)
    self.send(chat_id, "🔴 Cancelled. No balance, stock, or order was changed.", self.main_reply_keyboard(chat_id))
    
# ============================================================
# NEW FEATURES: Profile, My Orders, Transaction History
# ============================================================

def show_profile(self, chat_id: int) -> None:
    try:
        stats = self.db.user_stats(chat_id)
    except ValueError:
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    username = f"@{stats['username']}" if stats["username"] else "—"
    language_label = "বাংলা" if stats["language"] == "bn" else "English"
    member_since = str(stats["created_at"])[:10] if stats["created_at"] else "—"
    message = (
        "🔵 <b>👤 My Profile</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 UID: <code>{stats['telegram_user_id']}</code>\n"
        f"📛 Name: {html.escape(str(stats['first_name'] or '—'))}\n"
        f"🔗 Username: {html.escape(username)}\n"
        f"🌐 Language: {language_label}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: {self.amount(chat_id, int(stats['balance_cents']))}\n"
        f"🔒 Reserved: {self.amount(chat_id, int(stats['reserved_cents']))}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Total Orders: {stats['total_orders']}\n"
        f"🔢 Total Items: {stats['total_items']}\n"
        f"💵 Total Spent: {self.amount(chat_id, int(stats['total_spent_cents']))}\n"
        f"🗓️ Member Since: {member_since}"
    )
    self.send(
        chat_id,
        message,
        inline_keyboard(
            [
                [
                    ("🔵 💳 Balance", "balance"),
                    ("🔵 📜 My Orders", "my_orders"),
                ],
                [
                    ("🔵 💰 History", "history"),
                    ("🔵 🏠 Main Menu", "menu"),
                ],
            ]
        ),
        parse_mode="HTML",
    )

def show_my_orders(self, chat_id: int) -> None:
    try:
        orders = self.db.user_orders(chat_id, limit=10)
    except ValueError:
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    if not orders:
        self.send(
            chat_id,
            "📜 <b>My Orders</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "You have not placed any orders yet.",
            inline_keyboard(
                [
                    [("🔵 🛍 Marketplace", "market"), ("🔵 🏠 Main Menu", "menu")],
                ]
            ),
            parse_mode="HTML",
        )
        return
    lines: list[str] = []
    for order in orders:
        date_str = str(order["created_at"])[:16] if order["created_at"] else "—"
        status_icon = "🟢" if order["status"] == "completed" else "🔴"
        lines.append(
            f"{status_icon} <b>Order #{order['order_id']}</b>\n"
            f"📦 {html.escape(str(order['category_name']))} / {html.escape(str(order['option_name']))}\n"
            f"🔢 Qty: {order['quantity']} • 💰 {self.amount(chat_id, int(order['total_cents']))}\n"
            f"📅 {date_str}"
        )
    message = "📜 <b>My Orders</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
    self.send(
        chat_id,
        message,
        inline_keyboard(
            [
                [("🔵 👤 Profile", "profile"), ("🔵 💰 History", "history")],
                [("🔵 🏠 Main Menu", "menu")],
            ]
        ),
        parse_mode="HTML",
    )

def show_my_transactions(self, chat_id: int) -> None:
    try:
        entries = self.db.user_transactions(chat_id, limit=10)
    except ValueError:
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    if not entries:
        self.send(
            chat_id,
            "💰 <b>Transaction History</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "No transactions recorded yet.",
            inline_keyboard(
                [
                    [("🔵 👤 Profile", "profile"), ("🔵 🏠 Main Menu", "menu")],
                ]
            ),
            parse_mode="HTML",
        )
        return
    type_labels = {
        "deposit_approved": "🟢 Deposit Approved",
        "withdrawal_reserve": "🔴 Withdrawal Reserved",
        "withdrawal_refund": "🟢 Withdrawal Refunded",
        "purchase": "🔴 Purchase",
        "owner_sale": "🟢 Sale Earning",
        "platform_fee": "🟣 Platform Fee",
        "admin_adjustment": "🟡 Admin Adjustment",
    }
    lines: list[str] = []
    for entry in entries:
        amount_cents = int(entry["amount_cents"])
        sign = "🟢 +" if amount_cents >= 0 else "🔴 -"
        label = type_labels.get(str(entry["entry_type"]), f"⚪ {entry['entry_type']}")
        date_str = str(entry["created_at"])[:16] if entry["created_at"] else "—"
        lines.append(
            f"{label}\n"
            f"{sign}{self.amount(chat_id, abs(amount_cents))} • Balance: {self.amount(chat_id, int(entry['balance_after_cents']))}\n"
            f"📅 {date_str}"
        )
    message = "💰 <b>Transaction History</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
    self.send(
        chat_id,
        message,
        inline_keyboard(
            [
                [("🔵 👤 Profile", "profile"), ("🔵 📜 My Orders", "my_orders")],
                [("🔵 🏠 Main Menu", "menu")],
            ]
        ),
        parse_mode="HTML",
    )

# ============================================================
# ADMIN PANEL
# ============================================================

def show_admin_menu(self, chat_id: int, user_id: int) -> None:
    if not self.is_admin(user_id):
        self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
        return
    self.send(
        chat_id,
        "🟡 <b>⚙️ Admin Panel</b>\n━━━━━━━━━━━━━━━━━━",
        inline_keyboard(
            [
                [
                    ("🟢 📥 Pending Deposits", "admin:deposits"),
                    ("🟢 📤 Pending Withdrawals", "admin:withdrawals"),
                ],
                [
                    ("🔵 👥 Users & Balance", "admin:balance"),
                    ("🟡 📦 Inventory", "admin:products"),
                ],
                [("🟠 💳 Payment Setup", "admin:payment")],
                [("🟠 💰 Commission Settings", "admin:commission")],
                [
                    ("🟣 📊 Sales", "admin:sales"),
                    ("🟣 🧾 Orders", "admin:orders"),
                ],
                [("🟣 📢 Broadcast", "admin:broadcast")],
                [("🔵 🏠 Main Menu", "menu")],
            ]
        ),
        parse_mode="HTML",
    )

def is_admin(self, user_id: int) -> bool:
    return user_id == self.settings.admin_user_id

def start_balance_adjustment(self, chat_id: int, user_id: int) -> None:
    if not self.is_admin(user_id):
        return
    self.db.set_state(chat_id, "balance_user")
    self.send(chat_id, "Enter @username or Telegram UID:", inline_keyboard([[("🔴 Cancel", "cancel")]]))

def show_commission_setup(self, chat_id: int, user_id: int) -> None:
    if not self.is_admin(user_id):
        return
    percent = self.db.get_vendor_commission_percent()
    self.send(
        chat_id,
        f"🟠 <b>Commission Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Vendor commission: {percent}%\n"
        f"Admin platform fee: {100 - percent}%",
        inline_keyboard(
            [
                [("🟠 ✏️ Edit Commission", "commission:edit")],
                [("🔵 🔙 Back", "admin"), ("🔵 🏠 Main Menu", "menu")],
            ]
        ),
        parse_mode="HTML",
    )

def start_commission_setup(self, chat_id: int, user_id: int) -> None:
    if not self.is_admin(user_id):
        return
    self.db.set_state(chat_id, "commission_percent")
    self.send(
        chat_id,
        "Enter vendor commission percentage (0-100):",
        inline_keyboard([[("🔴 Cancel", "cancel")]]),
    )

def select_balance_mode(self, chat_id: int, user_id: int, data: str) -> None:
    if not self.is_admin(user_id):
        return
    state = self.db.get_state(chat_id)
    parts = data.split(":")
    if not state or state["state"] != "balance_user" or len(parts) != 3:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    direction, target_text = parts[1], parts[2]
    if direction not in {"add", "deduct"} or not target_text.isdigit():
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    user = self.db.find_user(target_text)
    if not user:
        self.send(chat_id, "❌ User not found.", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    self.db.set_state(
        chat_id,
        "balance_amount",
        json.dumps(
            {
                "target_telegram_user_id": user["telegram_user_id"],
                "direction": direction,
            }
        ),
    )
    action = "add" if direction == "add" else "deduct"
    self.send(
        chat_id,
        f"Enter amount to {action} for @{user['username'] or user['telegram_user_id']}:",
        inline_keyboard([[("🔴 Cancel", "cancel")]]),
    )

def admin_products(self, chat_id: int, user_id: int) -> None:
    if not self.is_admin(user_id):
        return
    categories = self.db.connection.execute(
        "SELECT * FROM categories WHERE is_deleted=0 ORDER BY name"
    ).fetchall()
    rows = [[(f"🟡 📁 {category['name']}", f"admin:category:{category['id']}")] for category in categories]
    rows.append([("🟢 ➕ Create Category", "admin:new_category")])
    rows.append([("🟡 ⚙️ Admin Panel", "admin")])
    self.send(chat_id, "🟡 <b>Inventory Categories</b>", inline_keyboard(rows), parse_mode="HTML")

def start_new_category(self, chat_id: int, user_id: int) -> None:
    if not self.is_admin(user_id):
        return
    self.db.set_state(chat_id, "category_name")
    self.send(chat_id, "Enter category name:", inline_keyboard([[("🔴 Cancel", "cancel")]]))

def start_category_rename(self, chat_id: int, user_id: int, category_id: int) -> None:
    if not self.is_admin(user_id):
        return
    category = self.db.connection.execute(
        "SELECT * FROM categories WHERE id=? AND is_deleted=0", (category_id,)
    ).fetchone()
    if not category:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    self.db.set_state(chat_id, "category_rename", json.dumps({"category_id": category_id}))
    self.send(
        chat_id,
        f"Current category name: {category['name']}\nEnter the new category name:",
        inline_keyboard([[("🔴 Cancel", f"admin:category:{category_id}")]]),
    )

def confirm_category_delete(self, chat_id: int, user_id: int, category_id: int) -> None:
    if not self.is_admin(user_id):
        return
    category = self.db.connection.execute(
        "SELECT * FROM categories WHERE id=? AND is_deleted=0", (category_id,)
    ).fetchone()
    if not category:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    self.send(
        chat_id,
        f"🔴 Delete category '<b>{html.escape(category['name'])}</b>'?\n"
        "All available linked stock will be removed.",
        inline_keyboard(
            [
                [("🔴 🗑 Confirm Delete", f"admin:category:delete_confirm:{category_id}")],
                [("🔵 🔙 Back", f"admin:category:{category_id}")],
            ]
        ),
        parse_mode="HTML",
    )

def delete_category(self, chat_id: int, user_id: int, category_id: int) -> None:
    if not self.is_admin(user_id):
        return
    try:
        category = self.db.delete_category(category_id)
        self.db.clear_state(chat_id)
        self.send(
            chat_id,
            f"✅ Category '{category['name']}' deleted. Linked available stock was removed.",
            inline_keyboard(
                [
                    [("🟡 📦 Inventory", "admin:products"), ("🟡 ⚙️ Admin Panel", "admin")],
                ]
            ),
        )
    except ValueError as error:
        self.send(chat_id, f"❌ {error}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))

def admin_category(self, chat_id: int, user_id: int, category_id: int) -> None:
    if not self.is_admin(user_id):
        return
    category = self.db.connection.execute(
        "SELECT * FROM categories WHERE id=? AND is_deleted=0", (category_id,)
    ).fetchone()
    if not category:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    options = self.db.connection.execute(
        """
        SELECT o.id, o.name, COALESCE(SUM(CASE WHEN i.status='available' THEN 1 ELSE 0 END), 0) stock
        FROM options o LEFT JOIN inventory_batches b ON b.option_id=o.id
        LEFT JOIN inventory_items i ON i.batch_id=b.id
        WHERE o.category_id=? AND o.is_deleted=0 GROUP BY o.id ORDER BY o.name
        """,
        (category_id,),
    ).fetchall()
    rows = [[(f"🟡 {option['name']} — Stock: {option['stock']}", f"admin:option:{option['id']}")] for option in options]
    rows.append(
        [
            ("🟠 ✏️ Rename Category", f"admin:category:rename:{category_id}"),
            ("🔴 🗑 Delete Category", f"admin:category:delete:{category_id}"),
        ]
    )
    rows.append([("🟢 ➕ Add Option", f"admin:new_option:{category_id}")])
    rows.append([("🔵 🔙 Back", "admin:products"), ("🟡 ⚙️ Admin Panel", "admin")])
    self.send(
        chat_id,
        f"🟡 <b>Category: {html.escape(category['name'])}</b>",
        inline_keyboard(rows),
        parse_mode="HTML",
    )

def start_new_option(self, chat_id: int, user_id: int, category_id: int) -> None:
    if not self.is_admin(user_id):
        return
    self.db.set_state(chat_id, "option_name", json.dumps({"category_id": category_id}))
    self.send(chat_id, "Enter option/sub-category name:", inline_keyboard([[("🔴 Cancel", "cancel")]]))

def start_option_rename(self, chat_id: int, user_id: int, option_id: int) -> None:
    if not self.is_admin(user_id):
        return
    option = self.db.option(option_id)
    if not option:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    self.db.set_state(chat_id, "option_rename", json.dumps({"option_id": option_id}))
    self.send(
        chat_id,
        f"Current sub-category name: {option['name']}\nEnter the new name:",
        inline_keyboard([[("🔴 Cancel", f"admin:option:{option_id}")]]),
    )

def confirm_option_delete(self, chat_id: int, user_id: int, option_id: int) -> None:
    if not self.is_admin(user_id):
        return
    option = self.db.option(option_id)
    if not option:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    self.send(
        chat_id,
        f"🔴 Delete sub-category '<b>{html.escape(option['name'])}</b>'?\n"
        "All available linked stock will be removed.",
        inline_keyboard(
            [
                [("🔴 🗑 Confirm Delete", f"admin:option:delete_confirm:{option_id}")],
                [("🔵 🔙 Back", f"admin:option:{option_id}")],
            ]
        ),
        parse_mode="HTML",
    )

def delete_option(self, chat_id: int, user_id: int, option_id: int) -> None:
    if not self.is_admin(user_id):
        return
    try:
        option = self.db.delete_option(option_id)
        self.db.clear_state(chat_id)
        self.send(
            chat_id,
            f"✅ Sub-category '{option['name']}' deleted. Linked available stock was removed.",
            inline_keyboard(
                [
                    [("🔵 📁 Back to Category", f"admin:category:{option['category_id']}")],
                    [("🟡 ⚙️ Admin Panel", "admin")],
                ]
            ),
        )
    except ValueError as error:
        self.send(chat_id, f"❌ {error}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))

def admin_option(self, chat_id: int, user_id: int, option_id: int) -> None:
    if not self.is_admin(user_id):
        return
    option = self.db.option(option_id)
    if not option:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    variants = self.db.option_price_variants(option_id)
    rows = [
        [
            (
                f"🟢 🏷️ {self.amount(chat_id, row['price_cents'])} "
                f"(Stock: {row['available_stock']})",
                f"admin:variant:{option_id}:{row['price_cents']}",
            )
        ]
        for row in variants
    ]
    rows.extend(
        [
            [("🟢 ➕ Add Stock", f"admin:add_stock:{option_id}")],
            [
                ("🟠 ✏️ Rename", f"admin:option:rename:{option_id}"),
                ("🔴 🗑 Delete", f"admin:option:delete:{option_id}"),
            ],
            [
                ("🔵 🔙 Back", f"admin:category:{option['category_id']}"),
                ("🟡 ⚙️ Admin Panel", "admin"),
            ],
        ]
    )
    self.send(
        chat_id,
        f"🟡 <b>{html.escape(str(option['category_name']))} / "
        f"{html.escape(str(option['name']))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📊 Available stock: {option['available_stock']}\nChoose a price variant:",
        inline_keyboard(rows),
        parse_mode="HTML",
    )

def admin_price_variant(
    self, chat_id: int, user_id: int, option_id: int, price_cents: int
) -> None:
    if not self.is_admin(user_id):
        return
    option = self.db.option(option_id)
    variants = self.db.option_price_variants(option_id)
    variant = next((row for row in variants if row["price_cents"] == price_cents), None)
    if not option or not variant:
        self.send(chat_id, FALLBACK, inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
        return
    self.send(
        chat_id,
        f"🟠 <b>🏷️ Price Variant Management</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Product: {html.escape(str(option['name']))}\n"
        f"💰 Price: {self.amount(chat_id, price_cents)}\n"
        f"📊 Available: {variant['available_stock']}",
        inline_keyboard(
            [
                [("🟠 ✏️ Edit Price", f"admin:variant:edit:{option_id}:{price_cents}")],
                [
                    ("🟠 🧹 Remove Quantity", f"admin:variant:remove:{option_id}:{price_cents}"),
                    ("🔴 🗑 Clear Stock", f"admin:variant:clear:{option_id}:{price_cents}"),
                ],
                [("🔵 🔙 Back", f"admin:option:{option_id}")],
            ]
        ),
        parse_mode="HTML",
    )

def start_variant_price_edit(
    self, chat_id: int, user_id: int, option_id: int, price_cents: int
) -> None:
    if not self.is_admin(user_id):
        return
    self.db.set_state(
        chat_id,
        "variant_price",
        json.dumps({"option_id": option_id, "old_price_cents": price_cents}),
    )
    self.send(
        chat_id,
        "🟠 Enter the new price per item:",
        inline_keyboard([[("🔴 Cancel", f"admin:variant:{option_id}:{price_cents}")]]),
    )

def start_variant_stock_remove(
    self, chat_id: int, user_id: int, option_id: int, price_cents: int
) -> None:
    if not self.is_admin(user_id):
        return
    self.db.set_state(
        chat_id,
        "variant_remove_quantity",
        json.dumps({"option_id": option_id, "price_cents": price_cents}),
    )
    self.send(
        chat_id,
        "🟠 Enter how many available stock items to remove:",
        inline_keyboard([[("🔴 Cancel", f"admin:variant:{option_id}:{price_cents}")]]),
    )

def confirm_variant_clear(
    self, chat_id: int, user_id: int, option_id: int, price_cents: int
) -> None:
    if not self.is_admin(user_id):
        return
    self.send(
        chat_id,
        "🔴 <b>Clear this price stock?</b>\n"
        "All available items in this price pool will be permanently removed. "
        "Sold history will remain unchanged.",
            inline_keyboard(
        [
            [("🔴 🗑 Confirm Clear", f"admin:variant:clear_confirm:{option_id}:{price_cents}")],
            [("🔵 🔙 Back", f"admin:variant:{option_id}:{price_cents}")],
        ]
    ),
    parse_mode="HTML",
    )
    
    def clear_variant_stock(
        self, chat_id: int, user_id: int, option_id: int, price_cents: int
    ) -> None:
        if not self.is_admin(user_id):
            return
        try:
            removed = self.db.remove_variant_stock(option_id, price_cents)
            self.db.clear_state(chat_id)
            self.send(
                chat_id,
                f"✅ Cleared {removed} available stock item(s) from this price pool.",
                inline_keyboard(
                    [
                        [("🟡 📦 Back to Product", f"admin:option:{option_id}")],
                        [("🟡 ⚙️ Admin Panel", "admin")],
                    ]
                ),
            )
        except ValueError as error:
            self.send(chat_id, f"❌ {error}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))

    def start_stock_upload(self, chat_id: int, user_id: int, option_id: int) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "stock_owner_type", json.dumps({"option_id": option_id}))
        self.send(
            chat_id,
            "Whose product is this stock?",
            inline_keyboard(
                [
                    [("🟢 👑 Admin's Product", "stock_owner:admin")],
                    [("🟢 👤 User's Product", "stock_owner:user")],
                    [("🔴 Cancel", "cancel")],
                ]
            ),
        )

    def select_stock_owner(self, chat_id: int, user_id: int, owner_type: str) -> None:
        if not self.is_admin(user_id):
            return
        state = self.db.get_state(chat_id)
        if not state or state["state"] != "stock_owner_type":
            self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
            return
        data = json.loads(state["data_json"])
        if owner_type == "admin":
            data["owner_telegram_user_id"] = self.settings.admin_user_id
            self.db.set_state(chat_id, "stock_lines", json.dumps(data))
            self.send(
                chat_id,
                "Send the accounts separated by `#` (hashtag).",
                inline_keyboard([[("🔴 Cancel", "cancel")]]),
            )
        elif owner_type == "user":
            self.db.set_state(chat_id, "stock_owner_uid", json.dumps(data))
            self.send(chat_id, "Enter target user's UID:", inline_keyboard([[("🔴 Cancel", "cancel")]]))

    def show_payment_setup(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        methods = self.db.connection.execute("SELECT * FROM payment_methods ORDER BY id").fetchall()
        text = (
            "🟠 <b>💳 Payment Methods</b>\n━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(
                f"<b>{html.escape(m['display_name'])}</b>: {copyable_details(m['details'])}"
                for m in methods
            )
        )
        rows = [[(f"🟠 ✏️ Edit {method['display_name']}", f"payment:{method['method_key']}")] for method in methods]
        rows.append([("🔵 🔙 Back", "admin"), ("🔵 🏠 Main Menu", "menu")])
        self.send(chat_id, text, inline_keyboard(rows), parse_mode="HTML")

    def start_payment_setup(self, chat_id: int, user_id: int, method_key: str) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "payment_details", json.dumps({"method_key": method_key}))
        self.send(chat_id, "Enter the payment account/details:", inline_keyboard([[("🔴 Cancel", "cancel")]]))

    def start_broadcast(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        self.db.set_state(chat_id, "broadcast")
        self.send(chat_id, "Write the broadcast message:", inline_keyboard([[("🔴 Cancel", "cancel")]]))

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
            self.send(chat_id, "No pending deposits.", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
            return
        for deposit in rows:
            self.send(
                chat_id,
                f"🟢 Deposit #{deposit['id']}\n"
                f"User: @{deposit['username'] or '-'} ({deposit['telegram_user_id']})\n"
                f"Amount: {self.amount(chat_id, deposit['amount_cents'])}\n"
                f"Method: {deposit['method_key']}\nTXID: {deposit['txid']}",
                inline_keyboard(
                    [
                        [
                            ("🟢 ✅ Approve", f"deposit:approve:{deposit['id']}"),
                            ("🔴 ❌ Reject", f"deposit:reject:{deposit['id']}"),
                        ]
                    ]
                ),
                replace=False,
                track=False,
            )

    def process_deposit(self, chat_id: int, user_id: int, data: str) -> None:
        if not self.is_admin(user_id):
            self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
            return
        action, deposit_id = data.split(":")[1:]
        try:
            deposit = self.db.approve_deposit(int(deposit_id)) if action == "approve" else self.db.reject_deposit(int(deposit_id))
            self.send(
                chat_id,
                f"✅ Deposit #{deposit['id']} {deposit['status']}.",
                inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]),
            )
            if action == "approve":
                user = self.db.connection.execute("SELECT telegram_user_id FROM users WHERE id=?", (deposit["user_id"],)).fetchone()
                self.send(
                    user["telegram_user_id"],
                    f"✅ Deposit approved. Added: "
                    f"{self.amount(user['telegram_user_id'], deposit['amount_cents'])}",
                    self.main_reply_keyboard(user["telegram_user_id"]),
                )
        except ValueError as error:
            self.send(chat_id, f"❌ {error}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))

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
            self.send(chat_id, "No pending withdrawals.", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))
            return
        for item in rows:
            self.send(
                chat_id,
                f"🟢 Withdrawal #{item['id']}\n"
                f"User: @{item['username'] or '-'} ({item['telegram_user_id']})\n"
                f"Amount: {self.amount(chat_id, item['amount_cents'])}\n"
                f"Method: {item['method_key']}\nPayout: {item['payout_details']}",
                inline_keyboard(
                    [
                        [
                            ("🟢 ✅ Paid & Approve", f"withdrawal:approve:{item['id']}"),
                            ("🔴 ❌ Reject", f"withdrawal:reject:{item['id']}"),
                        ]
                    ]
                ),
                replace=False,
                track=False,
            )

    def process_withdrawal(self, chat_id: int, user_id: int, data: str) -> None:
        if not self.is_admin(user_id):
            self.send(chat_id, FALLBACK, self.main_reply_keyboard(chat_id))
            return
        action, withdrawal_id = data.split(":")[1:]
        try:
            item = self.db.process_withdrawal(int(withdrawal_id), action == "approve")
            self.send(
                chat_id,
                f"✅ Withdrawal #{item['id']} {item['status']}.",
                inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]),
            )
            user = self.db.connection.execute("SELECT telegram_user_id FROM users WHERE id=?", (item["user_id"],)).fetchone()
            self.send(
                user["telegram_user_id"],
                f"Withdrawal #{item['id']} {item['status']}.",
                self.main_reply_keyboard(user["telegram_user_id"]),
            )
        except ValueError as error:
            self.send(chat_id, f"❌ {error}", inline_keyboard([[("🟡 ⚙️ Admin Panel", "admin")]]))

    def show_sales(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        sales = self.db.connection.execute(
            "SELECT COUNT(*) order_count, COALESCE(SUM(total_cents),0) total_cents, COALESCE(SUM(quantity),0) total_items FROM market_orders WHERE status='completed'"
        ).fetchone()
        self.send(
            chat_id,
            "🟣 <b>📊 Sales Overview</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"✅ Completed Orders: {sales['order_count']}\n"
            f"📦 Items Sold: {sales['total_items']}\n"
            f"💰 Gross Volume: {self.amount(chat_id, sales['total_cents'])}",
            inline_keyboard(
                [
                    [("🟡 ⚙️ Admin Panel", "admin"), ("🔵 🏠 Main Menu", "menu")],
                ]
            ),
            parse_mode="HTML",
        )

    def show_orders(self, chat_id: int, user_id: int) -> None:
        if not self.is_admin(user_id):
            return
        rows = self.db.connection.execute(
            """
            SELECT o.*, op.name option_name, u.telegram_user_id buyer_uid
            FROM market_orders o JOIN options op ON op.id=o.option_id
            JOIN users u ON u.id=o.buyer_user_id ORDER BY o.id DESC LIMIT 20
            """
        ).fetchall()
        text = "🟣 <b>🧾 Recent Orders</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(
            f"🟢 #{row['id']} {html.escape(str(row['option_name']))} x{row['quantity']} | "
            f"buyer {row['buyer_uid']} | {self.amount(chat_id, row['total_cents'])}"
            for row in rows
        ) if rows else "No orders yet."
        self.send(
            chat_id,
            text,
            inline_keyboard(
                [
                    [("🟡 ⚙️ Admin Panel", "admin"), ("🔵 🏠 Main Menu", "menu")],
                ]
            ),
            parse_mode="HTML",
        )


def main() -> None:
    settings = load_settings()
    bot = MarketplaceBot(settings)
    try:
        bot.run()
    finally:
        bot.db.close()


if __name__ == "__main__":
    main()
