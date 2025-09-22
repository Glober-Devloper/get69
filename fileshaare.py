
# fileshaare.py - ULTRA FileStore Bot (fixed, robust)
# Uses asyncpg for persistence. Button-only UI. Designed for Render (Docker).
import os
import asyncio
import logging
import uuid
import base64
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter as FloodWaitError

# ---------- Config ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "FileserveBot")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS","").replace(" ", "").split(",") if x]
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT","@admin")
CUSTOM_CAPTION = os.environ.get("CUSTOM_CAPTION","")
try:
    storage_id = int(os.environ.get("storage_id") or 0)
except Exception:
    storage_id = 0
PORT = int(os.environ.get("PORT","10000"))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ULTRAFileStore")

# ---------- Globals ----------
DB_POOL: Optional[asyncpg.pool.Pool] = None

# ---------- Utilities ----------
def is_admin(uid:int) -> bool:
    return uid in ADMIN_IDS

def gen_code() -> str:
    return base64.urlsafe_b64encode(uuid.uuid4().bytes).decode('utf-8').rstrip('=')[:18]

def nice_size(n:int)->str:
    try:
        n=int(n)
    except Exception:
        return "0 B"
    for unit in ['B','KB','MB','GB','TB']:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit!='B' else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

# ---------- DB Init ----------
async def init_db():
    async with DB_POOL.acquire() as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, user_id BIGINT UNIQUE NOT NULL, username TEXT, first_name TEXT,
            joined_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, is_active BOOLEAN DEFAULT TRUE
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS groups (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, owner_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, total_files INTEGER DEFAULT 0,
            total_size BIGINT DEFAULT 0
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS files (
            id SERIAL PRIMARY KEY, group_id INTEGER, serial INTEGER, unique_id TEXT UNIQUE,
            file_name TEXT, file_type TEXT, file_size BIGINT, storage_message_id BIGINT,
            uploader_id BIGINT, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS links (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE, file_id INTEGER, group_id INTEGER, owner_id BIGINT,
            expires_at TIMESTAMPTZ NULL, max_downloads INTEGER NULL, downloads INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, active BOOLEAN DEFAULT TRUE
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS stats (
            user_id BIGINT PRIMARY KEY, uploads INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0, last_active TIMESTAMPTZ
        )""")

# ---------- Background expiry worker ----------
async def expiry_worker():
    while True:
        try:
            async with DB_POOL.acquire() as conn:
                rows = await conn.fetch("SELECT id,code FROM links WHERE active = TRUE AND expires_at IS NOT NULL AND expires_at <= NOW()")
                for r in rows:
                    await conn.execute("UPDATE links SET active = FALSE WHERE id = $1", r['id'])
                    logger.info(f"Expired link: {r['code']}")
            await asyncio.sleep(20)
        except Exception as e:
            logger.error(f"expiry_worker error: {e}")
            await asyncio.sleep(5)

# ---------- Keyboards & UI ----------
def main_keyboard(is_admin_user:bool=False):
    kb = [
        [KeyboardButton("📤 Upload"), KeyboardButton("📦 Bulk Upload")],
        [KeyboardButton("🔗 My Links"), KeyboardButton("📂 My Files")],
        [KeyboardButton("👥 My Groups"), KeyboardButton("🔎 Search")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("❓ Help")],
    ]
    if is_admin_user:
        kb.append([KeyboardButton("👑 Admin")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=False)

def home_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]])

def file_actions_inline(group_id:int, serial:int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download", callback_data=f"download:{group_id}:{serial}"),
         InlineKeyboardButton("🔗 Share", callback_data=f"share:{group_id}:{serial}")],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"rename:{group_id}:{serial}"),
         InlineKeyboardButton("🖼 Edit Caption", callback_data=f"caption:{group_id}:{serial}")],
        [InlineKeyboardButton("📊 Stats", callback_data=f"stats:{group_id}:{serial}"),
         InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{group_id}:{serial}")],
        [InlineKeyboardButton("🔁 Replace", callback_data=f"replace:{group_id}:{serial}")],
        [InlineKeyboardButton("🏠 Home", callback_data="home")]
    ])

# ---------- User registration ----------
async def ensure_user(user):
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id,username,first_name) VALUES($1,$2,$3) ON CONFLICT (user_id) DO NOTHING", user.id, getattr(user,'username',None), getattr(user,'first_name',None))
            await conn.execute("INSERT INTO stats (user_id,last_active) VALUES ($1,NOW()) ON CONFLICT (user_id) DO UPDATE SET last_active=NOW()", user.id)
    except Exception as e:
        logger.error(f"ensure_user error: {e}")

# ---------- Handlers ----------
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    await update.message.reply_text(f"👋 Hello {user.first_name or user.username}! Welcome to the ULTRA File Manager.", reply_markup=main_keyboard(is_admin(user.id)))

async def cmd_help(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    text = ("*ULTRA Bot — Help*\n\nAll operations are available through buttons. Use Upload/Bulk Upload to add files to groups, "
            "My Groups/My Files to manage, and Settings for preferences.")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(is_admin(update.effective_user.id)))

# ---------- Upload flows ----------
async def choose_group_ui(update:Update, ctx:ContextTypes.DEFAULT_TYPE, bulk=False):
    user = update.effective_user
    await ensure_user(user)
    try:
        async with DB_POOL.acquire() as conn:
            groups = await conn.fetch("SELECT id,name,total_files FROM groups WHERE owner_id = $1 ORDER BY created_at DESC LIMIT 8", user.id)
    except Exception as e:
        logger.error(f"choose_group_ui DB error: {e}")
        groups = []
    buttons=[]; text="Select a group:"
    for g in groups:
        buttons.append([InlineKeyboardButton(f"{g['name']} ({g['total_files']})", callback_data=f"pick:{g['id']}")])
    buttons.append([InlineKeyboardButton("➕ New Group", callback_data="newgroup")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    ctx.user_data['is_bulk_upload']=bulk
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def create_group_from_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE, text:str):
    user = update.effective_user
    name = text.strip()
    if not name or len(name) > 80:
        await update.message.reply_text("Invalid group name. Try a shorter name.", reply_markup=main_keyboard(is_admin(user.id)))
        return
    try:
        async with DB_POOL.acquire() as conn:
            await conn.execute("INSERT INTO groups (name, owner_id) VALUES ($1, $2)", name, user.id)
        await update.message.reply_text(f"✅ Group '{name}' created.", reply_markup=main_keyboard(is_admin(user.id)))
    except asyncpg.UniqueViolationError:
        await update.message.reply_text("Group already exists.")
    except Exception as e:
        logger.error(f"create_group error: {e}")
        await update.message.reply_text("Could not create group. Try again later.")

async def message_router(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    txt = (update.message.text or "").strip()
    user = update.effective_user
    if txt == "📤 Upload":
        await choose_group_ui(update, ctx, bulk=False); return
    if txt == "📦 Bulk Upload":
        await choose_group_ui(update, ctx, bulk=True); return
    if txt == "🔗 My Links":
        await show_my_links(update, ctx); return
    if txt == "📂 My Files":
        await manage_files_ui(update, ctx); return
    if txt == "👥 My Groups":
        await list_groups_ui(update, ctx); return
    if txt == "🔎 Search":
        await update.message.reply_text("Send search keywords:"); ctx.user_data['awaiting_search'] = True; return
    if txt == "⚙️ Settings":
        await settings_ui(update, ctx); return
    if txt == "❓ Help":
        await cmd_help(update, ctx); return
    if txt == "👑 Admin" and is_admin(user.id):
        await admin_ui(update, ctx); return
    if ctx.user_data.get('awaiting_search') and txt:
        ctx.user_data.pop('awaiting_search', None)
        await do_search(update, ctx, txt); return
    if ctx.user_data.get('awaiting_new_group') and txt:
        ctx.user_data.pop('awaiting_new_group', None)
        await create_group_from_text(update, ctx, txt); return
    # file upload
    if any([update.message.document, update.message.photo, update.message.video, update.message.audio, update.message.voice]):
        await handle_upload(update, ctx); return
    await update.message.reply_text("Use the buttons below.", reply_markup=main_keyboard(is_admin(user.id)))

async def handle_upload(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    gid = ctx.user_data.get('upload_group_id')
    if not gid:
        await update.message.reply_text("Select a group first (Upload → pick a group)."); return
    msg = update.message
    file_obj = None; fname = None; fsize = 0
    if msg.document:
        file_obj = msg.document; fname = file_obj.file_name; fsize = file_obj.file_size or 0
    elif msg.photo:
        file_obj = msg.photo[-1]; fname = f"photo_{file_obj.file_unique_id}.jpg"; fsize = file_obj.file_size or 0
    elif msg.video:
        file_obj = msg.video; fname = file_obj.file_name or f"video_{file_obj.file_unique_id}"; fsize = file_obj.file_size or 0
    elif msg.audio:
        file_obj = msg.audio; fname = file_obj.file_name or f"audio_{file_obj.file_unique_id}"; fsize = file_obj.file_size or 0
    else:
        await update.message.reply_text("Unsupported file type."); return
    status = await update.message.reply_text(f"Uploading {fname}...")
    try:
        forwarded = await update.message.forward(chat_id=storage_id)
        sid = forwarded.message_id
        async with DB_POOL.acquire() as conn:
            serial = await conn.fetchval("SELECT COALESCE(MAX(serial),0)+1 FROM files WHERE group_id = $1", gid)
            await conn.execute("INSERT INTO files (group_id,serial,unique_id,file_name,file_type,file_size,storage_message_id,uploader_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                               gid, serial, gen_code(), fname, 'file', fsize, sid, user.id)
            await conn.execute("UPDATE groups SET total_files = total_files + 1, total_size = total_size + $1 WHERE id = $2", fsize, gid)
            await conn.execute("INSERT INTO stats (user_id,uploads,last_active) VALUES ($1,1,NOW()) ON CONFLICT (user_id) DO UPDATE SET uploads = stats.uploads + 1, last_active = NOW()", user.id)
        await status.delete()
        await update.message.reply_text(f"✅ Saved `{fname}`\\nSerial: `#{serial:03d}`", parse_mode=ParseMode.MARKDOWN, reply_markup=file_actions_inline(gid, serial))
    except FloodWaitError:
        await status.edit_text("Rate limited. Try again later.")
    except Exception as e:
        logger.error(f"upload error: {e}")
        try:
            await status.edit_text("Upload failed.")
        except:
            pass
    finally:
        if not ctx.user_data.get('is_bulk_upload'):
            ctx.user_data.pop('upload_group_id', None)

# ---------- Groups & Lists ----------
async def list_groups_ui(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        async with DB_POOL.acquire() as conn:
            groups = await conn.fetch("SELECT id,name,total_files,total_size FROM groups WHERE owner_id = $1 ORDER BY created_at DESC LIMIT 12", user.id)
    except Exception as e:
        logger.error(f"list_groups_ui DB error: {e}")
        groups = []
    if not groups:
        await update.message.reply_text("No groups yet. Create one from Upload → New Group.", reply_markup=main_keyboard(is_admin(user.id)))
        return
    text = "**Your Groups**\\n\\n"; buttons = []
    for g in groups:
        text += f"{g['name']} — {g['total_files']} files ({nice_size(g['total_size'])})\\n"
        buttons.append([InlineKeyboardButton(f"Open {g['name']}", callback_data=f"open:{g['id']}")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def open_group(query:CallbackQuery, gid:int):
    try:
        async with DB_POOL.acquire() as conn:
            group = await conn.fetchrow("SELECT id,name,total_files,total_size,owner_id FROM groups WHERE id = $1", gid)
            files = await conn.fetch("SELECT serial,file_name,file_size FROM files WHERE group_id = $1 ORDER BY serial DESC LIMIT 12", gid)
    except Exception as e:
        logger.error(f"open_group DB error: {e}")
        await query.edit_message_text("Error loading group.")
        return
    if not group:
        await query.edit_message_text("Group not found.")
        return
    text = f"**{group['name']}** — {group['total_files']} files | {nice_size(group['total_size'])}\\n\\n"
    buttons = []
    for f in files:
        text += f"`#{f['serial']:03d}` {f['file_name']} — {nice_size(f['file_size'])}\\n"
        buttons.append([InlineKeyboardButton(f"📥 #{f['serial']:03d}", callback_data=f"download:{gid}:{f['serial']}"),
                        InlineKeyboardButton("🔗", callback_data=f"share:{gid}:{f['serial']}"),
                        InlineKeyboardButton("🗑️", callback_data=f"delete:{gid}:{f['serial']}")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

# ---------- Links & Sharing ----------
async def create_share_link(group_id:int, serial:int, owner_id:int, expires_seconds:Optional[int]=None, max_downloads:Optional[int]=None):
    code = gen_code()
    expires_at = None
    if expires_seconds:
        expires_at = datetime.utcnow() + timedelta(seconds=expires_seconds)
    try:
        async with DB_POOL.acquire() as conn:
            file_id = await conn.fetchval("SELECT id FROM files WHERE group_id = $1 AND serial = $2", group_id, serial)
            if not file_id:
                return None
            await conn.execute("INSERT INTO links (code,file_id,group_id,owner_id,expires_at,max_downloads) VALUES($1,$2,$3,$4,$5,$6)",
                               code, file_id, group_id, owner_id, expires_at, max_downloads)
        return code
    except Exception as e:
        logger.error(f"create_share_link error: {e}")
        return None

# ---------- Callbacks ----------
async def callback_handler(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    parts = data.split(":")
    action = parts[0]
    await query.answer()
    try:
        if action == "pick":
            gid = int(parts[1])
            # fetch name
            try:
                async with DB_POOL.acquire() as conn:
                    name = await conn.fetchval("SELECT name FROM groups WHERE id = $1", gid)
            except Exception as e:
                logger.error(f"callback pick DB error: {e}")
                name = "Group"
            ctx.user_data['upload_group_id'] = gid
            await query.edit_message_text(f"Selected **{name}** — now send files.", parse_mode=ParseMode.MARKDOWN, reply_markup=home_inline())
        elif action == "newgroup":
            ctx.user_data['awaiting_new_group'] = True
            await query.edit_message_text("Send the new group name as a message.")
        elif action == "open":
            gid = int(parts[1])
            await open_group(query, gid)
        elif action in ("download","download_file"):
            gid = int(parts[1]); serial = int(parts[2])
            try:
                async with DB_POOL.acquire() as conn:
                    row = await conn.fetchrow("SELECT storage_message_id,file_name FROM files WHERE group_id = $1 AND serial = $2", gid, serial)
            except Exception as e:
                logger.error(f"callback download DB error: {e}")
                row = None
            if not row:
                await query.edit_message_text("File not found.")
                return
            try:
                await ctx.bot.copy_message(chat_id=query.message.chat.id, from_chat_id=storage_id, message_id=row['storage_message_id'], caption=row['file_name'])
                async with DB_POOL.acquire() as conn:
                    await conn.execute("INSERT INTO stats (user_id,downloads,last_active) VALUES($1,1,NOW()) ON CONFLICT (user_id) DO UPDATE SET downloads = stats.downloads + 1, last_active = NOW()", query.from_user.id)
            except Exception as e:
                logger.error(f"callback download error: {e}")
                await query.edit_message_text("Could not send file. Bot needs forward permission in storage channel.")
        elif action == "share":
            gid = int(parts[1]); serial = int(parts[2])
            opts = [
                [InlineKeyboardButton("5m", callback_data=f"sharec:{gid}:{serial}:300"), InlineKeyboardButton("10m", callback_data=f"sharec:{gid}:{serial}:600")],
                [InlineKeyboardButton("30m", callback_data=f"sharec:{gid}:{serial}:1800"), InlineKeyboardButton("1h", callback_data=f"sharec:{gid}:{serial}:3600")],
                [InlineKeyboardButton("1d", callback_data=f"sharec:{gid}:{serial}:86400"), InlineKeyboardButton("Never", callback_data=f"sharec:{gid}:{serial}:0")],
                [InlineKeyboardButton("🏠 Home", callback_data="home")]
            ]
            await query.edit_message_text("Choose expiry:", reply_markup=InlineKeyboardMarkup(opts))
        elif action == "sharec":
            gid = int(parts[1]); serial = int(parts[2]); seconds = int(parts[3])
            code = await create_share_link(gid, serial, query.from_user.id, expires_seconds=(seconds if seconds>0 else None))
            if not code:
                await query.edit_message_text("Could not create link.")
                return
            url = f"https://t.me/{BOT_USERNAME}?start={code}"
            await query.edit_message_text(f"🔗 Link:\\n`{url}`", parse_mode=ParseMode.MARKDOWN, reply_markup=home_inline())
        elif action == "delete":
            gid = int(parts[1]); serial = int(parts[2])
            try:
                async with DB_POOL.acquire() as conn:
                    row = await conn.fetchrow("SELECT id,storage_message_id FROM files WHERE group_id = $1 AND serial = $2", gid, serial)
                    if not row:
                        await query.edit_message_text("File not found.")
                        return
                    owner = await conn.fetchval("SELECT owner_id FROM groups WHERE id = $1", gid)
                    if query.from_user.id != owner and not is_admin(query.from_user.id):
                        await query.edit_message_text("🔒 You don't have permission to delete this file.")
                        return
                    await conn.execute("DELETE FROM files WHERE id = $1", row['id'])
                    await conn.execute("UPDATE groups SET total_files = total_files - 1 WHERE id = $1", gid)
            except Exception as e:
                logger.error(f"callback delete DB error: {e}")
                await query.edit_message_text("Error deleting file.")
                return
            try:
                await ctx.bot.delete_message(storage_id, row['storage_message_id'])
            except Exception:
                pass
            await query.edit_message_text("✅ Deleted.", reply_markup=home_inline())
        elif action == "home":
            await query.edit_message_text("🏠 Home", reply_markup=main_keyboard(is_admin(query.from_user.id)))
        else:
            await query.edit_message_text("Unknown action.")
    except Exception as e:
        logger.error(f"callback_handler error: {e}")
        try:
            await query.edit_message_text("An error occurred while processing action.")
        except Exception:
            pass

# ---------- Misc UI handlers ----------
async def show_my_links(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT code,expires_at,downloads,active FROM links WHERE owner_id = $1 ORDER BY created_at DESC LIMIT 25", user.id)
    except Exception as e:
        logger.error(f"show_my_links DB error: {e}")
        rows = []
    if not rows:
        await update.message.reply_text("You have no links.", reply_markup=main_keyboard(is_admin(user.id)))
        return
    text = "**Your Links**\\n\\n"
    for r in rows:
        url = f"https://t.me/{BOT_USERNAME}?start={r['code']}"
        exp = r['expires_at'].isoformat() if r['expires_at'] else "Never"
        text += f"`{url}` — {exp} — downloads: {r['downloads']} — {'active' if r['active'] else 'expired'}\\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(is_admin(user.id)))

async def manage_files_ui(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT group_id,serial,file_name FROM files WHERE uploader_id = $1 ORDER BY created_at DESC LIMIT 12", user.id)
    except Exception as e:
        logger.error(f"manage_files_ui DB error: {e}")
        rows = []
    if not rows:
        await update.message.reply_text("You have no recent files.", reply_markup=main_keyboard(is_admin(user.id)))
        return
    text = "**Your Files**\\n\\n"; buttons = []
    for r in rows:
        text += f"`#{r['serial']:03d}` {r['file_name']}\\n"
        buttons.append([InlineKeyboardButton(f"Open #{r['serial']:03d}", callback_data=f"openf:{r['group_id']}:{r['serial']}")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def do_search(update:Update, ctx:ContextTypes.DEFAULT_TYPE, q:str):
    qlike = f"%{q}%"
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT group_id,serial,file_name FROM files WHERE file_name ILIKE $1 ORDER BY created_at DESC LIMIT 25", qlike)
    except Exception as e:
        logger.error(f"do_search DB error: {e}")
        rows = []
    if not rows:
        await update.message.reply_text("No files found.", reply_markup=main_keyboard(is_admin(update.effective_user.id)))
        return
    text = f"Search results for `{q}`:\\n\\n"; buttons = []
    for r in rows:
        text += f"`#{r['serial']:03d}` {r['file_name']}\\n"
        buttons.append([InlineKeyboardButton(f"📥 #{r['serial']:03d}", callback_data=f"download:{r['group_id']}:{r['serial']}"),
                        InlineKeyboardButton("🔗", callback_data=f"share:{r['group_id']}:{r['serial']}")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

# ---------- Settings & Admin ----------
async def settings_ui(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = "⚙️ Settings\\n\\nChoose an option below."
    buttons = [
        [InlineKeyboardButton("⏱ Default Expiry: 10m", callback_data="set_default_expiry")],
        [InlineKeyboardButton("📝 Edit Caption Template", callback_data="edit_caption")],
        [InlineKeyboardButton("🏠 Home", callback_data="home")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_ui(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Admins only.", reply_markup=main_keyboard(False))
        return
    buttons = [
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("💾 Backup DB", callback_data="admin_backup")],
        [InlineKeyboardButton("🏠 Home", callback_data="home")]
    ]
    await update.message.reply_text("👑 Admin Panel", reply_markup=InlineKeyboardMarkup(buttons))

# ---------- Main ----------
async def main_async():
    global DB_POOL
    logger.info("Starting ULTRA FileStore (fixed)...")
    if not all([BOT_TOKEN, DATABASE_URL, storage_id]):
        logger.critical("FATAL: Missing critical environment variables! Set BOT_TOKEN, DATABASE_URL, storage_id.")
        return
    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6)
    await init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE, handle_upload))
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Start background expiry worker
    asyncio.create_task(expiry_worker())

    # Start health server in thread
    import threading, http.server, socketserver
    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200); self.send_header("Content-type","text/plain"); self.end_headers(); self.wfile.write(b"OK")
            else:
                self.send_response(404); self.end_headers()
    def start_health():
        with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
            logger.info(f"Health server on port {PORT}")
            httpd.serve_forever()
    threading.Thread(target=start_health, daemon=True).start()

    # Run bot
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        await asyncio.Event().wait()

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Stopping...")

if __name__ == "__main__":
    main()
