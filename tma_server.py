import os
import sqlite3
import hmac
import hashlib
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Request, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import aiohttp

load_dotenv(override=True)

def _env_truthy(val: Optional[str]) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "y", "on")

# --- LOGGING CONFIG ---
LOG_PATH = os.path.join(os.path.dirname(__file__), "tma_debug.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("TMA")

# --- CONFIG ---
DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else 0
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
SLA_ACCEPT_MIN = 10
SLA_FIX_MIN = 30

# Roles
ROLE_MAIN_ADMIN = "main_admin"
ROLE_MONITORING_ADMIN = "sled"
ROLE_EXECUTING_ADMIN = "isp_admin"
ROLE_EXECUTOR = "isp"

DISABLE_OWNER = _env_truthy(os.getenv("DISABLE_OWNER"))
DEV_FORCE_ROLE_USER_ID = int(os.getenv("DEV_FORCE_ROLE_USER_ID", "0") or 0)
DEV_FORCE_ROLE = (os.getenv("DEV_FORCE_ROLE") or "").strip()
DEV_FORCE_ROLE_USERNAME = (os.getenv("DEV_FORCE_ROLE_USERNAME") or "").strip().lstrip("@")

def _dev_forced_role(user_id: int, username: Optional[str] = None) -> Optional[str]:
    username_norm = (username or "").strip().lstrip("@")
    if DEV_FORCE_ROLE_USER_ID:
        if user_id != DEV_FORCE_ROLE_USER_ID:
            return None
    elif DEV_FORCE_ROLE_USERNAME:
        if not username_norm or username_norm.lower() != DEV_FORCE_ROLE_USERNAME.lower():
            return None
    else:
        return None
    if DEV_FORCE_ROLE in [ROLE_MAIN_ADMIN, ROLE_MONITORING_ADMIN, ROLE_EXECUTING_ADMIN, ROLE_EXECUTOR]:
        return DEV_FORCE_ROLE
    return None

def _is_effective_owner(user_id: int, username: Optional[str] = None) -> bool:
    if not OWNER_ID or DISABLE_OWNER:
        return False
    if user_id != OWNER_ID:
        return False
    if _dev_forced_role(user_id, username=username):
        return False
    return True

DEFAULT_TERRITORIES = [
    "Ресепшен",
    "Мужская",
    "Женская",
    "БВБ",
    "МВБ",
    "Улица",
]

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    asyncio.create_task(reminder_loop())
    asyncio.create_task(telegram_updates_loop())
    yield

app = FastAPI(title="CleanBot TMA API", lifespan=lifespan)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"Request: {request.method} {request.url.path}")
    try:
        response = await call_next(request)
        if response.status_code >= 400:
            logger.warning(f"Response: {response.status_code} for {request.url.path}")
        return response
    except Exception as e:
        logger.error(f"Error processing {request.url.path}: {str(e)}", exc_info=True)
        raise

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATABASE HELPERS ---
def db_query(query: str, params: tuple = (), one: bool = False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query, params)
        rv = cur.fetchall()
        conn.commit()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        logger.error(f"DB Error: {str(e)} | Query: {query}", exc_info=True)
        raise
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS territories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)")
        conn.execute("CREATE TABLE IF NOT EXISTS shifts (id INTEGER PRIMARY KEY AUTOINCREMENT, shift_date TEXT, leader_user_id INTEGER, leader_username TEXT, chat_id INTEGER, is_open INTEGER DEFAULT 1, rating INTEGER, leader_full_name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS shift_ratings (shift_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rating INTEGER NOT NULL, rated_at TEXT NOT NULL, PRIMARY KEY (shift_id, user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, user_id INTEGER, added_by INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, role TEXT DEFAULT 'isp', is_active INTEGER DEFAULT 0, last_seen TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS tg_pending_reports (user_id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, created_at TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            seq_no INTEGER, 
            shift_id INTEGER, 
            creator_user_id INTEGER, 
            creator_username TEXT, 
            creator_full_name TEXT, 
            performer_user_id INTEGER,
            performer_username TEXT,
            performer_full_name TEXT,
            assigned_to INTEGER,
            chat_id INTEGER, 
            created_at TEXT, 
            accepted_at TEXT, 
            fixed_at TEXT, 
            territory TEXT, 
            status TEXT, 
            description TEXT,
            location TEXT,
            report_text TEXT,
            report_photo TEXT,
            before_message_id TEXT, 
            after_message_id TEXT,
            rating INTEGER
        )""")
        
        # Migrations
        columns = [c[1] for c in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "description" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN description TEXT")
        if "performer_user_id" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN performer_user_id INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN performer_username TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN performer_full_name TEXT")
        if "rating" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN rating INTEGER")
        if "assigned_to" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN assigned_to INTEGER")
        if "location" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN location TEXT")
        if "report_text" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN report_text TEXT")
        if "report_photo" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN report_photo TEXT")
        
        user_columns = [c[1] for c in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'isp'")
        if "avatar" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
        
        # Update owner role
        if OWNER_ID and not DISABLE_OWNER:
            conn.execute("UPDATE users SET role=? WHERE user_id=?", (ROLE_MAIN_ADMIN, OWNER_ID))
            # Also insert owner if not exists
            conn.execute("INSERT OR IGNORE INTO users (user_id, role, is_active) VALUES (?, ?, 1)", (OWNER_ID, ROLE_MAIN_ADMIN))
        
        # Migrations for admins table
        admin_columns = [c[1] for c in conn.execute("PRAGMA table_info(admins)").fetchall()]
        if "user_id" not in admin_columns:
            conn.execute("ALTER TABLE admins ADD COLUMN user_id INTEGER")
        if "added_by" not in admin_columns:
            conn.execute("ALTER TABLE admins ADD COLUMN added_by INTEGER")
        
        # Check if territories empty
        cur = conn.execute("SELECT COUNT(*) FROM territories")
        if cur.fetchone()[0] == 0:
            for t in DEFAULT_TERRITORIES:
                conn.execute("INSERT OR IGNORE INTO territories(name) VALUES(?)", (t,))
        
        # Default settings
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("sla_accept", "10"))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("sla_fix", "30"))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("new_mode", "admin_private"))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("checker_rating_mode", "shift"))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("authorized_chats", os.getenv("AUTHORIZED_CHATS", "")))
        
        conn.commit()
    finally:
        conn.close()

init_db()

async def get_user_role(user_id: int) -> str:
    forced = _dev_forced_role(user_id)
    if forced:
        return forced
    if _is_effective_owner(user_id):
        return ROLE_MAIN_ADMIN
    row = db_query("SELECT role FROM users WHERE user_id=?", (user_id,), one=True)
    return row["role"] if row else ROLE_EXECUTOR

async def is_admin(user: Dict[str, Any]) -> bool:
    role = await get_user_role(user["id"])
    return role in [ROLE_MAIN_ADMIN, ROLE_MONITORING_ADMIN, ROLE_EXECUTING_ADMIN]

# --- TELEGRAM NOTIFICATIONS ---
async def send_telegram_msg(chat_id: Optional[int], text: str, reply_markup: dict = None, photo_path: str = None):
    if not chat_id:
        return None
        
    # Check if user wants private notifications
    if chat_id > 0: # Private chat
        rows = db_query("SELECT value FROM settings WHERE key='private_notifications'")
        if rows and rows[0]["value"] == "off":
            return None

    if photo_path:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        data.add_field("caption", text)
        data.add_field("parse_mode", "HTML")
        if reply_markup:
            data.add_field("reply_markup", json.dumps(reply_markup))
        
        file_full_path = os.path.join(UPLOAD_DIR, photo_path)
        if os.path.exists(file_full_path):
            data.add_field("photo", open(file_full_path, "rb"), filename=photo_path)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data) as resp:
                    return await resp.json()

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            return await resp.json()

async def _tg_call(session: aiohttp.ClientSession, method: str, *, params: Optional[dict] = None, payload: Optional[dict] = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if payload is not None:
        async with session.post(url, json=payload, timeout=20) as resp:
            return await resp.json()
    async with session.get(url, params=params or {}, timeout=70) as resp:
        return await resp.json()

async def _tg_download_file(session: aiohttp.ClientSession, file_id: str, save_as_filename: str) -> Optional[str]:
    meta = await _tg_call(session, "getFile", params={"file_id": file_id})
    if not meta or not meta.get("ok") or not meta.get("result") or not meta["result"].get("file_path"):
        return None
    file_path = meta["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    full_path = os.path.join(UPLOAD_DIR, save_as_filename)
    async with session.get(url, timeout=70) as resp:
        if resp.status != 200:
            return None
        content = await resp.read()
    with open(full_path, "wb") as f:
        f.write(content)
    return save_as_filename

async def _handle_task_done_callback(session: aiohttp.ClientSession, cq: dict):
    cq_id = cq.get("id")
    from_user = cq.get("from") or {}
    user_id = from_user.get("id")
    data = cq.get("data") or ""

    if not cq_id or not user_id:
        return

    try:
        task_id = int(data.split("_")[-1])
    except Exception:
        await _tg_call(session, "answerCallbackQuery", payload={"callback_query_id": cq_id, "text": "Некорректная кнопка", "show_alert": True})
        return

    task = db_query("SELECT id, seq_no, territory, assigned_to, creator_user_id FROM tasks WHERE id=?", (task_id,), one=True)
    if not task:
        await _tg_call(session, "answerCallbackQuery", payload={"callback_query_id": cq_id, "text": "Задача не найдена", "show_alert": True})
        return

    if task["assigned_to"] and int(task["assigned_to"]) != int(user_id):
        await _tg_call(session, "answerCallbackQuery", payload={"callback_query_id": cq_id, "text": "Эта задача назначена другому исполнителю", "show_alert": True})
        return

    db_query(
        "INSERT OR REPLACE INTO tg_pending_reports(user_id, task_id, created_at) VALUES (?, ?, datetime('now'))",
        (user_id, task_id),
    )

    await _tg_call(session, "answerCallbackQuery", payload={"callback_query_id": cq_id, "text": "Пришлите фото-отчет или текст"})

    await _tg_call(
        session,
        "sendMessage",
        payload={
            "chat_id": user_id,
            "text": f"📸 Пришлите фото-отчет или текст по задаче <b>#{task['seq_no']}</b> ({task['territory'] or '---'}).\n\nДля отмены: /cancel",
            "parse_mode": "HTML",
        },
    )

async def _handle_pending_report_message(session: aiohttp.ClientSession, message: dict):
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return

    from_user = message.get("from") or {}
    user_id = from_user.get("id")
    if not user_id:
        return

    text = (message.get("text") or "").strip()
    if text == "/cancel":
        db_query("DELETE FROM tg_pending_reports WHERE user_id=?", (user_id,))
        await _tg_call(session, "sendMessage", payload={"chat_id": user_id, "text": "Отменено.", "parse_mode": "HTML"})
        return

    pending = db_query("SELECT task_id FROM tg_pending_reports WHERE user_id=?", (user_id,), one=True)
    if not pending:
        return

    task_id = int(pending["task_id"])
    task = db_query("SELECT id, seq_no, territory, creator_user_id, assigned_to FROM tasks WHERE id=?", (task_id,), one=True)
    if not task:
        db_query("DELETE FROM tg_pending_reports WHERE user_id=?", (user_id,))
        await _tg_call(session, "sendMessage", payload={"chat_id": user_id, "text": "Задача не найдена, состояние сброшено.", "parse_mode": "HTML"})
        return

    if task["assigned_to"] and int(task["assigned_to"]) != int(user_id):
        db_query("DELETE FROM tg_pending_reports WHERE user_id=?", (user_id,))
        await _tg_call(session, "sendMessage", payload={"chat_id": user_id, "text": "Эта задача назначена другому исполнителю.", "parse_mode": "HTML"})
        return

    report_text = None
    report_photo = None

    if message.get("photo"):
        photos = message["photo"]
        file_id = photos[-1].get("file_id")
        if not file_id:
            await _tg_call(session, "sendMessage", payload={"chat_id": user_id, "text": "Не удалось прочитать фото. Пришлите еще раз.", "parse_mode": "HTML"})
            return
        filename = f"report_tg_{task_id}_{int(datetime.now(timezone.utc).timestamp())}.jpg"
        saved = await _tg_download_file(session, file_id, filename)
        if not saved:
            await _tg_call(session, "sendMessage", payload={"chat_id": user_id, "text": "Не удалось скачать фото. Пришлите еще раз.", "parse_mode": "HTML"})
            return
        report_photo = saved
        report_text = (message.get("caption") or "").strip() or None
    elif text:
        report_text = text
    else:
        await _tg_call(session, "sendMessage", payload={"chat_id": user_id, "text": "Пришлите фото или текст отчета (или /cancel).", "parse_mode": "HTML"})
        return

    username = from_user.get("username")
    full_name = f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or username or str(user_id)

    db_query(
        "UPDATE tasks SET status='completed', fixed_at=?, report_text=?, report_photo=?, performer_user_id=?, performer_username=?, performer_full_name=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), report_text, report_photo, user_id, username, full_name, task_id),
    )
    db_query("DELETE FROM tg_pending_reports WHERE user_id=?", (user_id,))

    await _tg_call(
        session,
        "sendMessage",
        payload={
            "chat_id": user_id,
            "text": f"✅ Отчет принят по задаче <b>#{task['seq_no']}</b>.",
            "parse_mode": "HTML",
        },
    )

    creator_id = task["creator_user_id"]
    if creator_id:
        msg = f"✅ <b>Задача #{task['seq_no']} выполнена!</b>\n📍 Территория: {task['territory']}\n👤 Исполнитель: {full_name}\n📝 Отчет: {report_text or '---'}"
        await send_telegram_msg(int(creator_id), msg, photo_path=report_photo)

async def telegram_updates_loop():
    if not BOT_TOKEN:
        logger.warning("Telegram updates loop: TELEGRAM_BOT_TOKEN is empty, skipping")
        return

    logger.info("Telegram updates loop started (getUpdates)")
    offset = None
    allowed_updates = json.dumps(["message", "callback_query"])

    async with aiohttp.ClientSession() as session:
        try:
            await _tg_call(session, "deleteWebhook", payload={"drop_pending_updates": True})
        except Exception as e:
            logger.warning(f"Telegram updates loop: deleteWebhook failed: {e}")

        while True:
            try:
                params = {"timeout": 50, "allowed_updates": allowed_updates}
                if offset is not None:
                    params["offset"] = offset
                data = await _tg_call(session, "getUpdates", params=params)
                if not data or not data.get("ok"):
                    desc = None
                    if isinstance(data, dict):
                        desc = data.get("description") or data.get("error_code")
                    if desc:
                        logger.warning(f"Telegram getUpdates not ok: {desc}")
                    await asyncio.sleep(3)
                    continue
                for upd in data.get("result", []):
                    if upd.get("update_id") is not None:
                        offset = int(upd["update_id"]) + 1
                    if upd.get("callback_query") and (upd["callback_query"].get("data") or "").startswith("task_done_"):
                        await _handle_task_done_callback(session, upd["callback_query"])
                        continue
                    if upd.get("message"):
                        await _handle_pending_report_message(session, upd["message"])
            except Exception as e:
                logger.error(f"Telegram updates loop error: {e}", exc_info=True)
                await asyncio.sleep(2)

# --- REMINDERS (SLA) ---
async def schedule_reminders(task_id: int, chat_id: int, seq_no: int):
    # SLA Accept: 10 min
    await asyncio.sleep(10 * 60)
    task = db_query("SELECT status FROM tasks WHERE id=?", (task_id,), one=True)
    if task and task["status"] == "open":
        await send_telegram_msg(chat_id, f"⚠️ <b>SLA WARNING!</b>\nЗадача #{seq_no} не принята более 10 минут!")

    # SLA Fix: 30 min total
    await asyncio.sleep(20 * 60)
    task = db_query("SELECT status FROM tasks WHERE id=?", (task_id,), one=True)
    if task and task["status"] in ["open", "accepted"]:
        await send_telegram_msg(chat_id, f"🚨 <b>SLA CRITICAL!</b>\nЗадача #{seq_no} не выполнена более 30 минут!")

# --- SECURITY ---
def verify_telegram_data(init_data: str) -> Optional[Dict[str, Any]]:
    if not init_data: 
        logger.warning("Verify: Missing init_data")
        return None
    try:
        from urllib.parse import parse_qsl, unquote
        
        # Логируем начало процесса и сырые данные
        logger.info(f"Verify: Processing init_data (len={len(init_data)})")
        logger.debug(f"Verify: Raw init_data: {init_data[:100]}...")
        
        # Пытаемся распарсить как query string
        params = dict(parse_qsl(init_data))
        
        user_json = None
        if "user" in params:
            user_json = params["user"]
            logger.info("Verify: Found 'user' in params via parse_qsl")
        else:
            # Если parse_qsl не нашел 'user', ищем вручную в строке (на случай кривого кодирования)
            logger.info("Verify: 'user' not found in params, trying manual split")
            for part in init_data.split('&'):
                if part.startswith('user='):
                    user_json = unquote(part[5:])
                    logger.info("Verify: Found 'user' via manual split")
                    break
        
        if user_json:
            try:
                user_data = json.loads(user_json)
                # Гарантируем наличие id, даже если это строка
                if 'id' in user_data:
                    user_data['id'] = int(user_data['id'])
                
                logger.info(f"Verify SUCCESS: Found user {user_data.get('username')} (ID: {user_data.get('id')})")
                return user_data
            except Exception as je:
                logger.error(f"Verify: JSON decode error: {je} for data: {user_json}")
        
        logger.warning(f"Verify FAIL: No user data found in params keys: {list(params.keys())}")
        return None
            
    except Exception as e:
        logger.error(f"Verify Critical Error: {e}", exc_info=True)
        return None

async def get_current_user(x_telegram_init_data: Optional[str] = Header(None)):
    if not x_telegram_init_data:
        logger.warning("Auth: Missing X-Telegram-Init-Data header")
        raise HTTPException(status_code=401, detail="Missing Telegram init data")
    user = verify_telegram_data(x_telegram_init_data)
    if not user:
        logger.warning("Auth: Invalid Telegram init data")
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    
    # Save/Update user in DB
    try:
        # Check if user already exists to preserve role
        existing = db_query("SELECT role FROM users WHERE user_id=?", (user["id"],), one=True)
        role = existing["role"] if existing else ROLE_EXECUTOR
        
        forced = _dev_forced_role(user["id"], username=user.get("username"))
        if forced:
            role = forced
        elif _is_effective_owner(user["id"], username=user.get("username")):
            role = ROLE_MAIN_ADMIN

        db_query("""
            INSERT INTO users (user_id, username, full_name, role, last_seen, is_active)
            VALUES (?, ?, ?, ?, datetime('now'), 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen,
                is_active=1
        """, (user["id"], user.get("username"), f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(), role))
        
        user["role"] = role
    except Exception as e:
        logger.error(f"Error saving user to DB: {e}")
        user["role"] = ROLE_EXECUTOR
        
    return user

# --- API ROUTES ---

@app.get("/api/uploads/{filename}")
async def get_upload(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404)

@app.get("/favicon.ico")
async def favicon():
    # Return 404 instead of 204 to avoid confusing SPA routing in some browsers
    raise HTTPException(status_code=404)

@app.get("/api/me")
async def get_me(user = Depends(get_current_user)):
    return user

@app.get("/api/territories")
async def list_territories():
    rows = db_query("SELECT name FROM territories ORDER BY name")
    return [row["name"] for row in rows]

@app.post("/api/territories")
async def add_territory_api(data: Dict[str, str], user = Depends(get_current_user)):
    name = data.get("name")
    if not name: raise HTTPException(status_code=400)
    try:
        db_query("INSERT INTO territories(name) VALUES(?)", (name,))
        return {"status": "ok"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Territory already exists")

@app.delete("/api/territories/{name}")
async def delete_territory_api(name: str, user = Depends(get_current_user)):
    db_query("DELETE FROM territories WHERE name=?", (name,))
    return {"status": "ok"}

@app.get("/api/settings")
async def get_settings_api(user = Depends(get_current_user)):
    rows = db_query("SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in rows}

@app.post("/api/settings")
async def update_settings_api(data: Dict[str, str], user = Depends(get_current_user)):
    if not _is_effective_owner(user["id"], username=user.get("username")):
        raise HTTPException(status_code=403, detail="Only owner can change settings")
    for key, value in data.items():
        db_query("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, str(value)))
        # Update environment variables for bot if needed
        if key == "authorized_chats":
            os.environ["AUTHORIZED_CHATS"] = value
    return {"status": "ok"}

@app.get("/api/config")
async def get_config(user = Depends(get_current_user)):
    return {
        "owner_id": 0 if (DISABLE_OWNER or _dev_forced_role(user["id"], username=user.get("username"))) else OWNER_ID,
        "is_admin": await is_admin(user),
        "role": user.get("role")
    }

@app.get("/api/telegram/bot-info")
async def telegram_bot_info_api(user = Depends(get_current_user)):
    if not _is_effective_owner(user["id"], username=user.get("username")):
        raise HTTPException(status_code=403, detail="Only owner can view bot info")
    if not BOT_TOKEN:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN is empty")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10) as resp:
            me = await resp.json()
        async with session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=10) as resp:
            wh = await resp.json()
    return {"bot": me.get("result"), "webhook": wh.get("result")}

@app.post("/api/users/{user_id}/role")
async def update_user_role_api(user_id: int, data: Dict[str, str], user = Depends(get_current_user)):
    if user["role"] != ROLE_MAIN_ADMIN and not _is_effective_owner(user["id"], username=user.get("username")):
        raise HTTPException(status_code=403, detail="Only main admin can change roles")
    
    new_role = data.get("role")
    if not new_role:
        raise HTTPException(status_code=400, detail="Role is required")
        
    if new_role not in [ROLE_MAIN_ADMIN, ROLE_MONITORING_ADMIN, ROLE_EXECUTING_ADMIN, ROLE_EXECUTOR]:
        raise HTTPException(status_code=400, detail="Invalid role")
        
    db_query("UPDATE users SET role=? WHERE user_id=?", (new_role, user_id))
    return {"status": "ok"}

@app.get("/api/users/all")
async def get_all_users_api(user = Depends(get_current_user)):
    if user["role"] != ROLE_MAIN_ADMIN and not _is_effective_owner(user["id"], username=user.get("username")):
        raise HTTPException(status_code=403, detail="Only main admin can view all users")
    
    # Get all users with their roles and activity status
    rows = db_query("SELECT user_id, username, full_name, role, is_active, last_seen, avatar FROM users ORDER BY last_seen DESC")
    
    # Get list of admin usernames
    admin_rows = db_query("SELECT username FROM admins")
    admin_usernames = [r["username"] for r in admin_rows if r["username"]]
    
    # Fetch avatars (photos) for each user from Telegram
    users_list = []
    async with aiohttp.ClientSession() as session:
        async def fetch_avatar(u_dict):
            user_id = u_dict["user_id"]
            if not u_dict.get("avatar"):
                try:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos?user_id={user_id}&limit=1"
                    async with session.get(url, timeout=2) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("ok") and data["result"]["total_count"] > 0:
                                file_id = data["result"]["photos"][0][0]["file_id"]
                                url_file = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
                                async with session.get(url_file, timeout=2) as resp_file:
                                    if resp_file.status == 200:
                                        data_file = await resp_file.json()
                                        if data_file.get("ok"):
                                            file_path = data_file["result"]["file_path"]
                                            u_dict["avatar"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                                            db_query("UPDATE users SET avatar=? WHERE user_id=?", (u_dict["avatar"], user_id))
                except Exception as e:
                    logger.error(f"Error fetching avatar for {user_id}: {e}")
            return u_dict

        # Fetch avatars in parallel
        tasks = [fetch_avatar(dict(r)) for r in rows]
        users_list = await asyncio.gather(*tasks)
    
    return {"users": users_list, "admins": admin_usernames}


@app.get("/api/users/search")
async def search_users_api(q: str = "", user = Depends(get_current_user)):
    # Только главный админ может искать всех пользователей
    if user["role"] != ROLE_MAIN_ADMIN and user["id"] != OWNER_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Убираем @ если пользователь ввел его в поиске
    search_q = q.strip()
    if search_q.startswith("@"):
        search_q = search_q[1:]
    
    query = f"%{search_q}%"
    users = db_query("""
        SELECT user_id, username, full_name, role, is_active, avatar 
        FROM users 
        WHERE username LIKE ? OR full_name LIKE ? 
        OR LOWER(username) LIKE LOWER(?) OR LOWER(full_name) LIKE LOWER(?)
        LIMIT 20
    """, (query, query, query, query))
    
    users_list = []
    async with aiohttp.ClientSession() as session:
        async def fetch_avatar(u_dict):
            user_id = u_dict["user_id"]
            if not u_dict.get("avatar"):
                try:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos?user_id={user_id}&limit=1"
                    async with session.get(url, timeout=2) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("ok") and data["result"]["total_count"] > 0:
                                file_id = data["result"]["photos"][0][0]["file_id"]
                                url_file = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
                                async with session.get(url_file, timeout=2) as resp_file:
                                    if resp_file.status == 200:
                                        data_file = await resp_file.json()
                                        if data_file.get("ok"):
                                            file_path = data_file["result"]["file_path"]
                                            u_dict["avatar"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                                            db_query("UPDATE users SET avatar=? WHERE user_id=?", (u_dict["avatar"], user_id))
                except Exception:
                    pass
            return u_dict

        # Fetch avatars in parallel
        tasks = [fetch_avatar(dict(r)) for r in users]
        users_list = await asyncio.gather(*tasks)
    
    return users_list



@app.get("/api/users/executors")
async def get_executors_api(user = Depends(get_current_user)):
    # Criteria: role 'isp' (executor) and active
    users = db_query("SELECT user_id, username, full_name, role, avatar FROM users WHERE role = ? AND is_active = 1", (ROLE_EXECUTOR,))
    
    executors_list = []
    async with aiohttp.ClientSession() as session:
        async def fetch_avatar(u_dict):
            user_id = u_dict["user_id"]
            if not u_dict.get("avatar"):
                try:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos?user_id={user_id}&limit=1"
                    async with session.get(url, timeout=2) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("ok") and data["result"]["total_count"] > 0:
                                file_id = data["result"]["photos"][0][0]["file_id"]
                                url_file = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
                                async with session.get(url_file, timeout=2) as resp_file:
                                    if resp_file.status == 200:
                                        data_file = await resp_file.json()
                                        if data_file.get("ok"):
                                            file_path = data_file["result"]["file_path"]
                                            u_dict["avatar"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                                            db_query("UPDATE users SET avatar=? WHERE user_id=?", (u_dict["avatar"], user_id))
                except Exception as e:
                    logger.error(f"Error fetching avatar for {user_id}: {e}")
            return u_dict

        tasks = [fetch_avatar(dict(r)) for r in users]
        executors_list = await asyncio.gather(*tasks)
            
    return executors_list


@app.post("/api/tasks/{task_id}/assign")
async def assign_task_api(task_id: int, data: Dict[str, Any] = Body(...), user = Depends(get_current_user)):
    # Only Executing Admin or higher can assign
    role = user.get("role")
    if role not in [ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN]:
        raise HTTPException(status_code=403, detail="Only executing admins can assign tasks")
    
    territory = (data.get("territory") or "").strip() or None
    current = db_query("SELECT territory FROM tasks WHERE id=?", (task_id,), one=True)
    if not current:
        raise HTTPException(status_code=404, detail="Task not found")
    if not (current["territory"] or "").strip() and not territory:
        raise HTTPException(status_code=400, detail="Сначала выберите территорию")

    if territory:
        db_query("UPDATE tasks SET territory=? WHERE id=?", (territory, task_id))

    performer_id = data.get("performer_id")
    if not performer_id:
        # If closed without assignment, status is "in_progress_by_admin"
        db_query("UPDATE tasks SET status='in_progress_by_admin' WHERE id=?", (task_id,))
        return {"status": "ok", "message": "Task marked as in progress by admin"}

    # Assign to executor
    db_query("UPDATE tasks SET assigned_to=?, status='delegated' WHERE id=?", (performer_id, task_id))
    
    # Notify executor via bot
    task = db_query("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    msg = f"📋 <b>Вам назначена новая задача #{task['seq_no']}</b>\n📍 Территория: {task['territory'] or '---'}\n📍 Локация: {task['location'] or '---'}\n📝 Описание: {task['description'] or '---'}"
    
    reply_markup = {
        "inline_keyboard": [[{"text": "✅ Выполнил", "callback_data": f"task_done_{task_id}"}]]
    }
    
    await send_telegram_msg(performer_id, msg, reply_markup=reply_markup, photo_path=task['before_message_id'])
    
    return {"status": "ok"}

@app.post("/api/users/toggle-active")
async def toggle_user_active_api(data: Dict[str, Any], user = Depends(get_current_user)):
    if user["role"] != ROLE_MAIN_ADMIN and not _is_effective_owner(user["id"], username=user.get("username")):
        raise HTTPException(status_code=403, detail="Only main admin can toggle user activity")
    
    user_id = data.get("user_id")
    is_active = data.get("is_active")
    if user_id is None or is_active is None:
        raise HTTPException(status_code=400, detail="Missing user_id or is_active")
        
    db_query("UPDATE users SET is_active=? WHERE user_id=?", (is_active, user_id))
    return {"status": "ok"}

@app.post("/api/tasks/{task_id}/complete")
async def complete_task_api(
    task_id: int, 
    report_text: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user = Depends(get_current_user)
):
    # Can be completed by assigned executor or by admin if in progress by admin
    task = db_query("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    is_assigned = (task["assigned_to"] == user["id"])
    is_admin_completing = (task["status"] == "in_progress_by_admin" and user["role"] in [ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN])
    
    if not (is_assigned or is_admin_completing):
        raise HTTPException(status_code=403, detail="You are not authorized to complete this task")

    photo_path = None
    if photo:
        ext = photo.filename.split('.')[-1]
        filename = f"report_{task_id}_{datetime.now().timestamp()}.{ext}"
        photo_path = filename
        with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
            f.write(await photo.read())

    db_query(
        "UPDATE tasks SET status='completed', fixed_at=?, report_text=?, report_photo=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), report_text, photo_path, task_id)
    )
    
    # Notify creator (Sled Admin)
    creator_id = task["creator_user_id"]
    msg = f"✅ <b>Задача #{task['seq_no']} выполнена!</b>\n📍 Территория: {task['territory']}\n📝 Отчет: {report_text or '---'}"
    await send_telegram_msg(creator_id, msg, photo_path=photo_path)
    
    return {"status": "ok"}

@app.post("/api/tasks")
async def create_task_api(
    territory: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    chat_id: int = Form(...),
    photo: Optional[UploadFile] = File(None),
    user = Depends(get_current_user)
):
    # Only Sled Admin or higher can create tasks
    role = user.get("role")
    if role not in [ROLE_MAIN_ADMIN, ROLE_MONITORING_ADMIN]:
        raise HTTPException(status_code=403, detail="Only monitoring admins can create tasks")

    shift = db_query("SELECT id FROM shifts WHERE is_open=1 ORDER BY id DESC LIMIT 1", one=True)
    shift_id = shift["id"] if shift else None
    
    today = date.today().isoformat()
    max_seq = db_query("SELECT MAX(seq_no) as max_seq FROM tasks WHERE DATE(created_at)=? AND chat_id=?", (today, chat_id), one=True)
    seq_no = (max_seq["max_seq"] or 0) + 1
    
    photo_path = None
    if photo:
        ext = photo.filename.split('.')[-1]
        filename = f"task_{datetime.now().timestamp()}.{ext}"
        photo_path = filename
        with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
            f.write(await photo.read())

    territory_value = (territory or "").strip() or None
    db_query(
        """INSERT INTO tasks(seq_no, shift_id, creator_user_id, creator_username, creator_full_name, chat_id, created_at, status, territory, description, location, before_message_id) 
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (seq_no, shift_id, user["id"], user.get("username", ""), f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(), 
         chat_id, datetime.now(timezone.utc).isoformat(), "open", territory_value, description, location, photo_path)
    )
    
    task_id = db_query("SELECT last_insert_rowid() as id", one=True)["id"]
    
    # Notify Executing Admins (Ispolnyayuschiy Admin)
    isp_admins = db_query("SELECT user_id FROM users WHERE role IN (?, ?)", (ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN))
    for admin in isp_admins:
        msg = f"🆕 <b>Новая задача #{seq_no} ожидает распределения</b>\n📍 Территория: {territory_value or '---'}\n📍 Локация: {location or '---'}\n📝: {description or '---'}\n👤 Автор: {user.get('first_name')}"
        await send_telegram_msg(admin["user_id"], msg, photo_path=photo_path)

    if chat_id:
        msg = f"🆕 <b>Новая задача #{seq_no}</b>\n📍 Территория: {territory_value or '---'}\n📍 Локация: {location or '---'}\n📝: {description or '---'}\n👤 Автор: {user.get('first_name')}"
        await send_telegram_msg(chat_id, msg, photo_path=photo_path)
        asyncio.create_task(schedule_reminders(task_id, chat_id, seq_no))
        
    return {"status": "ok", "id": task_id}

@app.get("/api/shifts/active")
async def get_active_shift():
    row = db_query("SELECT * FROM shifts WHERE is_open=1 ORDER BY id DESC LIMIT 1", one=True)
    return dict(row) if row else None

@app.get("/api/shift/status")
async def get_shift_status(user = Depends(get_current_user)):
    # Find if there is an open shift
    shift = db_query("SELECT id, shift_date, leader_full_name, leader_user_id, leader_username FROM shifts WHERE is_open=1 ORDER BY id DESC LIMIT 1", one=True)
    if shift:
        return {"active": True, "shift": dict(shift)}
    last_closed = db_query("SELECT id, shift_date, leader_full_name, leader_user_id, leader_username, rating FROM shifts WHERE is_open=0 ORDER BY id DESC LIMIT 1", one=True)
    if not last_closed:
        return {"active": False}
    my_rating = db_query("SELECT rating FROM shift_ratings WHERE shift_id=? AND user_id=?", (last_closed["id"], user["id"]), one=True)
    return {
        "active": False,
        "last_closed_shift": dict(last_closed),
        "needs_rating": my_rating is None,
        "my_shift_rating": my_rating["rating"] if my_rating else None
    }

@app.get("/api/shifts/last-closed")
async def get_last_closed_shift_api(user = Depends(get_current_user)):
    last_closed = db_query("SELECT id, shift_date, leader_full_name, leader_user_id, leader_username, rating FROM shifts WHERE is_open=0 ORDER BY id DESC LIMIT 1", one=True)
    if not last_closed:
        return {"shift": None, "needs_rating": False, "my_shift_rating": None}
    my_rating = db_query("SELECT rating FROM shift_ratings WHERE shift_id=? AND user_id=?", (last_closed["id"], user["id"]), one=True)
    return {
        "shift": dict(last_closed),
        "needs_rating": my_rating is None,
        "my_shift_rating": my_rating["rating"] if my_rating else None
    }

@app.post("/api/shifts/{shift_id}/rate")
async def rate_shift_api(shift_id: int, rating: int = Body(..., embed=True), user = Depends(get_current_user)):
    if user.get("role") != ROLE_MONITORING_ADMIN:
        raise HTTPException(status_code=403, detail="Only checker can rate shifts")
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Invalid rating")
    shift = db_query("SELECT is_open FROM shifts WHERE id=?", (shift_id,), one=True)
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    if shift["is_open"] == 1:
        raise HTTPException(status_code=400, detail="Shift is still open")
    existing = db_query("SELECT rating FROM shift_ratings WHERE shift_id=? AND user_id=?", (shift_id, user["id"]), one=True)
    if existing:
        return {"status": "ok", "already": True}
    db_query(
        "INSERT OR REPLACE INTO shift_ratings(shift_id, user_id, rating, rated_at) VALUES(?,?,?,datetime('now'))",
        (shift_id, user["id"], rating),
    )
    avg_row = db_query("SELECT AVG(rating) as avg_rating FROM shift_ratings WHERE shift_id=?", (shift_id,), one=True)
    avg_rating = int(round(avg_row["avg_rating"] or 0))
    db_query("UPDATE shifts SET rating=? WHERE id=?", (avg_rating if avg_rating > 0 else None, shift_id))
    return {"status": "ok"}

@app.post("/api/tasks/{task_id}/rate")
async def rate_task_api(task_id: int, rating: int = Body(..., embed=True), user = Depends(get_current_user)):
    if user.get("role") == ROLE_MONITORING_ADMIN:
        mode_row = db_query("SELECT value FROM settings WHERE key='checker_rating_mode'", one=True)
        checker_mode = (mode_row["value"] if mode_row else "task") or "task"
        if checker_mode == "shift":
            raise HTTPException(status_code=403, detail="Task rating is disabled for checker")

    task = db_query("SELECT shift_id FROM tasks WHERE id=?", (task_id,), one=True)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    role = user.get("role")
    if _is_effective_owner(user["id"], username=user.get("username")) or role in [ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN, ROLE_MONITORING_ADMIN]:
        db_query("UPDATE tasks SET rating=? WHERE id=?", (rating, task_id))
        return {"status": "ok"}
    
    shift = db_query("SELECT leader_user_id FROM shifts WHERE id=?", (task["shift_id"],), one=True)
    if not shift or shift["leader_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only shift leader can rate tasks")
    
    db_query("UPDATE tasks SET rating=? WHERE id=?", (rating, task_id))
    return {"status": "ok"}

@app.post("/api/shift/start")
async def start_shift_api(user = Depends(get_current_user)):
    if user.get("role") not in [ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN]:
        raise HTTPException(status_code=403, detail="Только руководитель смены может начать смену")
    # Start shift logic similar to bot.py
    date_str = datetime.now().strftime("%Y-%m-%d")
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('username', 'Unknown')
    
    # Close any open shifts just in case
    db_query("UPDATE shifts SET is_open=0 WHERE is_open=1")
    
    db_query(
        "INSERT INTO shifts(shift_date, leader_user_id, leader_username, leader_full_name, is_open) VALUES(?,?,?,?,1)",
        (date_str, user["id"], user.get("username"), full_name)
    )
    return {"status": "ok"}

@app.post("/api/shift/end")
async def end_shift_api(user = Depends(get_current_user)):
    if user.get("role") not in [ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN]:
        raise HTTPException(status_code=403, detail="Только руководитель смены может завершить смену")
    db_query("UPDATE shifts SET is_open=0 WHERE is_open=1")
    return {"status": "ok"}

@app.get("/api/tasks")
async def get_tasks(user = Depends(get_current_user)):
    tasks = db_query("SELECT * FROM tasks ORDER BY created_at DESC")
    return [dict(t) for t in tasks]

@app.post("/api/tasks/{task_id}/accept")
async def accept_task_api(task_id: int, data: Dict[str, Any] = None, user = Depends(get_current_user)):
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('username', 'Unknown')
    
    territory = data.get("territory") if data else None
    
    if territory:
        db_query(
            "UPDATE tasks SET status='accepted', accepted_at=?, performer_user_id=?, performer_username=?, performer_full_name=?, territory=? WHERE id=?", 
            (datetime.now(timezone.utc).isoformat(), user["id"], user.get("username"), full_name, territory, task_id)
        )
    else:
        db_query(
            "UPDATE tasks SET status='accepted', accepted_at=?, performer_user_id=?, performer_username=?, performer_full_name=? WHERE id=?", 
            (datetime.now(timezone.utc).isoformat(), user["id"], user.get("username"), full_name, task_id)
        )
    return {"status": "ok"}

@app.post("/api/tasks/{task_id}/fix")
async def fix_task_api(
    task_id: int, 
    photo: Optional[UploadFile] = File(None),
    report_text: Optional[str] = Form(None),
    user = Depends(get_current_user)
):
    task = db_query("SELECT chat_id, seq_no FROM tasks WHERE id=?", (task_id,), one=True)
    if not task: raise HTTPException(status_code=404)
    
    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('username', 'Unknown')
    
    photo_path = None
    if photo:
        ext = photo.filename.split('.')[-1]
        filename = f"fix_{datetime.now().timestamp()}.{ext}"
        photo_path = filename
        with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
            f.write(await photo.read())
    
    db_query(
        "UPDATE tasks SET status='closed', fixed_at=?, after_message_id=?, report_text=?, performer_user_id=?, performer_username=?, performer_full_name=? WHERE id=?", 
        (datetime.now(timezone.utc).isoformat(), photo_path, report_text, user["id"], user.get("username"), full_name, task_id)
    )
    
    if task["chat_id"]:
        msg = f"✅ <b>Задача #{task['seq_no']} исправлена!</b>\n👤 Исполнитель: {user.get('first_name')}"
        await send_telegram_msg(task["chat_id"], msg, photo_path=photo_path)
        
    return {"status": "ok"}

@app.get("/api/analytics")
async def get_analytics():
    total_tasks = db_query("SELECT COUNT(*) as count FROM tasks", one=True)["count"]
    completed_tasks = db_query("SELECT COUNT(*) as count FROM tasks WHERE status='closed' OR status='completed'", one=True)["count"]
    today_tasks = db_query("SELECT COUNT(*) as count FROM tasks WHERE DATE(created_at)=?", (date.today().isoformat(),), one=True)["count"]
    return {"total_tasks": total_tasks, "completed_tasks": completed_tasks, "today_tasks": today_tasks}

@app.get("/api/stats/detailed")
async def get_detailed_stats(user = Depends(get_current_user)):
    if not await is_admin(user):
        raise HTTPException(status_code=403, detail="Access denied")

    # Total stats
    total = db_query("SELECT COUNT(*) as c FROM tasks", one=True)["c"]
    closed = db_query("SELECT COUNT(*) as c FROM tasks WHERE status='closed' OR status='completed'", one=True)["c"]
    
    # Today stats
    today = date.today().isoformat()
    today_created = db_query("SELECT COUNT(*) as c FROM tasks WHERE DATE(created_at)=?", (today,), one=True)["c"]
    today_closed = db_query("SELECT COUNT(*) as c FROM tasks WHERE (status='closed' OR status='completed') AND (DATE(fixed_at)=? OR DATE(created_at)=?)", (today, today), one=True)["c"]
    
    # Avg times
    avg_react = db_query("""
        SELECT AVG((julianday(accepted_at) - julianday(created_at)) * 24 * 60) as avg
        FROM tasks WHERE accepted_at IS NOT NULL
    """, one=True)["avg"]
    
    avg_fix = db_query("""
        SELECT AVG((julianday(fixed_at) - julianday(created_at)) * 24 * 60) as avg
        FROM tasks WHERE fixed_at IS NOT NULL
    """, one=True)["avg"]
    
    # Territory stats with reaction/fix times
    terr_stats = db_query("""
        SELECT 
            territory, 
            COUNT(*) as count,
            AVG((julianday(accepted_at) - julianday(created_at)) * 24 * 60) as avg_react,
            AVG((julianday(fixed_at) - julianday(created_at)) * 24 * 60) as avg_fix
        FROM tasks 
        GROUP BY territory 
        ORDER BY count DESC
    """)
    
    # Performer stats (very detailed)
    perf_stats = db_query("""
        SELECT 
            performer_full_name, 
            performer_username,
            COUNT(*) as count,
            AVG((julianday(accepted_at) - julianday(created_at)) * 24 * 60) as avg_react,
            AVG((julianday(fixed_at) - julianday(created_at)) * 24 * 60) as avg_fix
        FROM tasks 
        WHERE (status='closed' OR status='completed') AND performer_full_name IS NOT NULL
        GROUP BY performer_full_name, performer_username
        ORDER BY count DESC
    """)

    # Active admins
    admins = db_query("SELECT username FROM admins")
    admin_list = [row["username"] for row in admins]

    return {
        "total": total,
        "closed": closed,
        "today_created": today_created,
        "today_closed": today_closed,
        "avg_react": round(avg_react or 0, 1),
        "avg_fix": round(avg_fix or 0, 1),
        "territory_stats": [dict(r) for r in terr_stats],
        "performer_stats": [dict(r) for r in perf_stats],
        "admins": admin_list
    }

@app.post("/api/admins/toggle")
async def toggle_admin_api(data: dict, user = Depends(get_current_user)):
    if not _is_effective_owner(user["id"], username=user.get("username")):
        raise HTTPException(status_code=403, detail="Only owner can toggle admins")
    
    username = (data.get("username") or "").strip()
    user_id = data.get("user_id")
    is_admin_flag = data.get("is_admin")

    if username.startswith("@"):
        username = username[1:].strip()

    if not username:
        if user_id is not None:
            row = db_query("SELECT username FROM users WHERE user_id=?", (int(user_id),), one=True)
            username = (row["username"] or "").strip() if row else ""
            if username.startswith("@"):
                username = username[1:].strip()
        if not username and user_id is not None:
            username = f"id{int(user_id)}"
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")

    if is_admin_flag:
        db_query("INSERT OR IGNORE INTO admins (username, user_id) VALUES (?, ?)", (username, user_id))
    else:
        db_query("DELETE FROM admins WHERE username = ?", (username,))
    return {"status": "ok"}

async def reminder_loop():
    logger.info("SLA Reminder loop started")
    while True:
        try:
            # 1. Check for tasks not accepted within 10 mins
            overdue_accept = db_query("""
                SELECT * FROM tasks 
                WHERE status='open' 
                AND (julianday('now') - julianday(created_at)) * 24 * 60 > 10
            """)
            
            # 2. Check for tasks not fixed within 30 mins
            overdue_fix = db_query("""
                SELECT * FROM tasks 
                WHERE status='accepted' 
                AND (julianday('now') - julianday(created_at)) * 24 * 60 > 30
            """)

            # Notify for each (only once per task ideally, but for now we keep it simple as per request)
            # To avoid spam, we could add a last_reminded_at column, but the user wants "persistent" reminders
            for task in list(overdue_accept) + list(overdue_fix):
                text = f"🚨 **SLA НАРУШЕН!**\n\nЗадача #{task['seq_no']} ({task['territory']}) висит слишком долго!\n"
                text += f"Статус: {task['status']}\nСоздана: {task['created_at']}"
                
                # Notify owner and current shift leader
                shifts = db_query("SELECT chat_id FROM shifts WHERE is_open=1")
                for s in shifts:
                    if s['chat_id']:
                        await send_telegram_msg(s['chat_id'], text)
                
                # Also notify owner directly
                if OWNER_ID:
                    await send_telegram_msg(OWNER_ID, text)

        except Exception as e:
            logger.error(f"Error in reminder loop: {e}")
        
        await asyncio.sleep(300) # Check every 5 mins

# --- FRONTEND ---
@app.get("/", response_class=HTMLResponse)
async def serve_spa(request: Request):
    logger.info(f"Serving SPA for {request.client.host}")
    html_content = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <!-- Cache-bust: CACHE_BUST_VAL -->
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>CleanBot Mini App</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <script>
        window.onerror = function(msg, url, lineNo, columnNo, error) {
            console.error('Window Error:', msg, 'at', url, ':', lineNo);
            // Можно добавить визуальное уведомление если нужно
            return false;
        };
        window.onunhandledrejection = function(event) {
            console.error('Unhandled Promise Rejection:', event.reason);
        };
    </script>
    <style>
        :root {
            --tg-theme-bg-color: #ffffff;
            --tg-theme-text-color: #000000;
            --tg-theme-hint-color: #999999;
            --tg-theme-link-color: #2481cc;
            --tg-theme-button-color: #2481cc;
            --tg-theme-button-text-color: #ffffff;
            --tg-theme-secondary-bg-color: #efeff3;
            --tg-theme-header-bg-color: #ffffff;
        }
        body.dark {
            --tg-theme-bg-color: #1c1c1d;
            --tg-theme-text-color: #ffffff;
            --tg-theme-hint-color: #8e8e93;
            --tg-theme-link-color: #64b5f6;
            --tg-theme-button-color: #2481cc;
            --tg-theme-button-text-color: #ffffff;
            --tg-theme-secondary-bg-color: #000000;
            --tg-theme-header-bg-color: #2c2c2e;
        }
        body {
            background-color: var(--tg-theme-secondary-bg-color);
            color: var(--tg-theme-text-color);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            transition: all 0.2s ease;
            overflow-x: hidden;
        }
        .card {
            background-color: var(--tg-theme-bg-color);
            border-radius: 16px;
            margin: 12px;
            padding: 16px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.05);
        }
        .btn-primary {
            background-color: var(--tg-theme-button-color);
            color: var(--tg-theme-button-text-color);
            padding: 12px;
            border-radius: 12px;
            text-align: center;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.1s;
        }
        .btn-primary:active { transform: scale(0.98); }
        .badge {
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 10px;
            font-weight: 800;
            text-transform: uppercase;
        }
        .status-open { background-color: #fee2e2; color: #ef4444; }
        .status-accepted { background-color: #dbeafe; color: #3b82f6; }
        .status-closed { background-color: #dcfce7; color: #22c55e; }
        
        .archive-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px;
            color: var(--tg-theme-hint-color);
            font-size: 14px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .nav-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            height: 70px;
            background-color: var(--tg-theme-bg-color);
            display: flex;
            justify-content: space-around;
            align-items: center;
            border-top: 1px solid rgba(0,0,0,0.1);
            padding-bottom: env(safe-area-inset-bottom);
            z-index: 40;
            transition: transform 0.3s ease-in-out;
        }
        .nav-bar.hidden {
            transform: translateY(100%);
        }
        .nav-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            color: var(--tg-theme-hint-color);
            font-size: 10px;
            font-weight: 600;
            transition: color 0.2s;
            flex: 1;
            height: 100%;
            justify-content: center;
        }
        .nav-item.active {
            color: var(--tg-theme-button-color);
        }
        .nav-item i {
            font-size: 20px;
        }

        /* Loading Spinner */
        .spinner {
            width: 20px;
            height: 20px;
            border: 3px solid rgba(255,255,255,0.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s ease-in-out infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            height: 100dvh;
            background-color: rgba(0,0,0,0.6);
            backdrop-filter: blur(4px);
            z-index: 2000; /* Increased z-index to be above everything */
            display: flex;
            align-items: center;
            justify-content: center;
            touch-action: none;
            padding-top: calc(20px + env(safe-area-inset-top));
            padding-left: 20px;
            padding-right: 20px;
            padding-bottom: calc(20px + env(safe-area-inset-bottom) + 90px);
        }
        .modal-content {
            width: 100%;
            max-width: 500px;
            background-color: var(--tg-theme-bg-color);
            border-radius: 24px;
            padding: 24px;
            max-height: calc(100dvh - 40px - env(safe-area-inset-top) - env(safe-area-inset-bottom) - 90px);
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
            position: relative;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
        }
        
        /* Toast Notification */
        .toast {
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #333;
            color: white;
            padding: 12px 24px;
            border-radius: 12px;
            z-index: 1000;
            font-weight: 600;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            animation: fadeInOut 3s ease-in-out forwards;
        }
        @keyframes fadeInOut {
            0% { opacity: 0; transform: translate(-50%, -20px); }
            15% { opacity: 1; transform: translate(-50%, 0); }
            85% { opacity: 1; transform: translate(-50%, 0); }
            100% { opacity: 0; transform: translate(-50%, -20px); }
        }

        .photo-zoom { cursor: zoom-in; }
        .modal-full { position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 1000; display: flex; align-items: center; justify-content: center; }
        .modal-full img { max-width: 100%; max-height: 100%; object-fit: contain; }
        .shift-overlay { position: fixed; inset: 0; background: var(--tg-theme-bg-color); z-index: 500; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; text-align: center; }
        .stat-card { background: var(--tg-theme-secondary-bg-color); padding: 15px; border-radius: 15px; }
        .stat-value { font-size: 24px; font-weight: 900; color: #3b82f6; }
        .stat-label { font-size: 10px; font-weight: 700; opacity: 0.5; text-transform: uppercase; letter-spacing: 0.5px; }
        
        @keyframes fadeIn { from { opacity: 0; transform: scale(0.95); } to { opacity: 1; transform: scale(1); } }
        .animate-in { animation: fadeIn 0.2s ease-out forwards; }

        .shift-lead-mode {
            background: linear-gradient(180deg, rgba(59, 130, 246, 0.05) 0%, transparent 100%);
        }
        .shift-lead-mode .card {
            border-left: 4px solid #3b82f6;
        }
    </style>
</head>
<body>
    <div id="root"></div>

    <script type="text/babel">
        console.log("React SPA Initializing...");
        const { useState, useEffect, useCallback } = React;

        const fetchApi = async (url, method = 'GET', body = null) => {
            const tg = window.Telegram.WebApp;
            const options = {
                method,
                headers: { 'X-Telegram-Init-Data': tg.initData }
            };
            if (body) {
                options.body = JSON.stringify(body);
                options.headers['Content-Type'] = 'application/json';
            }
            const res = await fetch(url, options);
            if (res.status === 401) return { error: 'unauthorized' };
            return await res.json();
        };

        const FullImageModal = ({ src, onClose }) => (
            <div className="modal-full animate-in" onClick={onClose}>
                <img src={src} alt="Full size" />
                <div className="absolute top-4 right-4 text-white text-2xl">
                    <i className="fas fa-times"></i>
                </div>
            </div>
        );

        const SettingsMenu = ({ users, fetchUsers, updateRole, toggleUserActive, onClose }) => {
            const roleLabels = {
                'main_admin': 'Главный',
                'sled': 'Проверяющий',
                'isp_admin': 'Руководитель смены',
                'isp': 'Исполнитель'
            };

            return (
                <div className="modal-overlay" onClick={onClose}>
                    <div className="modal-content animate-in" onClick={e => e.stopPropagation()}>
                        <div className="flex justify-between items-center mb-6">
                            <h2 className="text-xl font-black">Управление ролями</h2>
                            <div className="w-8 h-8 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-full" onClick={onClose}>
                                <i className="fas fa-times opacity-50"></i>
                            </div>
                        </div>
                        
                        <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-2">
                            {users.map(u => (
                                <div key={u.user_id} className="p-4 rounded-2xl border border-gray-100 dark:border-gray-800 space-y-3">
                                    <div className="flex justify-between items-center">
                                        <div className="flex items-center gap-3">
                                            {u.avatar ? (
                                                <img src={u.avatar} className="w-10 h-10 rounded-full object-cover" />
                                            ) : (
                                                <div className="w-10 h-10 rounded-full bg-blue-500 flex items-center justify-center text-white font-bold">
                                                    {(u.full_name || u.username || String(u.user_id) || '?')[0].toUpperCase()}
                                                </div>
                                            )}
                                            <div>
                                                <p className="font-bold text-sm truncate max-w-[150px]">{u.full_name || u.username || u.user_id}</p>
                                                <p className="text-[10px] opacity-50">@{u.username || 'no_username'}</p>
                                            </div>
                                        </div>
                                        <div onClick={() => toggleUserActive(u.user_id, u.is_active)} 
                                             className={`w-10 h-6 rounded-full relative transition-colors cursor-pointer flex-shrink-0 ${u.is_active ? 'bg-green-500' : 'bg-gray-300'}`}>
                                            <div className={`absolute top-1 w-4 h-4 bg-white rounded-full transition-transform ${u.is_active ? 'translate-x-5' : 'translate-x-1'}`}></div>
                                        </div>
                                    </div>
                                    
                                    <div className="grid grid-cols-2 gap-2">
                                        {Object.entries(roleLabels).map(([role, label]) => (
                                            <div key={role} 
                                                 onClick={() => updateRole(u.user_id, role)}
                                                 className={`px-2 py-2 rounded-xl text-[10px] font-bold border text-center transition-all cursor-pointer ${u.role === role ? 'bg-blue-500 border-blue-500 text-white' : 'border-gray-100 dark:border-gray-800 opacity-60 hover:opacity-100'}`}>
                                                {label}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            );
        };

        const TaskCard = ({ task, onAction, territories, isShiftLead, config }) => {
            const isClosed = task.status === 'completed' || task.status === 'closed';
            const [loading, setLoading] = useState(false);
            const [zoomImg, setZoomImg] = useState(null);
            const [showAssignModal, setShowAssignModal] = useState(false);
            const [executors, setExecutors] = useState([]);
            const [rating, setRating] = useState(task.rating);
            const [selectedTerritory, setSelectedTerritory] = useState(task.territory || '');
            
            // New state for confirmation
            const [showConfirmUI, setShowConfirmUI] = useState(false);
            const [reportPhoto, setReportPhoto] = useState(null);
            const [reportPreview, setReportPreview] = useState(null);
            const [reportText, setReportText] = useState('');

            useEffect(() => {
                setRating(task.rating);
            }, [task.rating]);

            const handleRate = async (val) => {
                setRating(val);
                await onAction(`/api/tasks/${task.id}/rate`, 'POST', { rating: val });
            };

            const handleAssign = async (performerId = null) => {
                if (!selectedTerritory) {
                    window.Telegram.WebApp.showAlert("Выберите территорию");
                    return;
                }
                setLoading(true);
                await onAction(`/api/tasks/${task.id}/assign`, 'POST', { performer_id: performerId, territory: selectedTerritory });
                setLoading(false);
                setShowAssignModal(false);
            };

            const fetchExecutors = async () => {
                const data = await fetchApi('/api/users/executors');
                if (data) setExecutors(data);
                setSelectedTerritory(task.territory || '');
                setShowAssignModal(true);
            };

            const handleConfirmFix = async () => {
                setLoading(true);
                const fd = new FormData();
                if (reportPhoto) fd.append('photo', reportPhoto);
                if (reportText) fd.append('report_text', reportText);
                
                await onAction(`/api/tasks/${task.id}/fix`, 'POST', fd, true);
                setLoading(false);
                setShowConfirmUI(false);
                setReportPhoto(null);
                setReportPreview(null);
                setReportText('');
            };

            const canAssign = config?.role === 'main_admin' || config?.role === 'isp_admin';
            const canRateTask = isShiftLead || config?.role === 'main_admin' || config?.role === 'isp_admin';

            const getStatusLabel = () => {
                if (task.status === 'open') return 'Ожидает';
                if (task.status === 'delegated') return 'Делегировано';
                if (task.status === 'completed' || task.status === 'closed') {
                    const fixedAt = task.fixed_at ? new Date(task.fixed_at) : null;
                    const today = new Date();
                    if (fixedAt && fixedAt.toDateString() === today.toDateString()) {
                        return 'Выполнено сегодня';
                    }
                    return 'Завершено';
                }
                return 'В работе у админа';
            };

            return (
                <div className={`card overflow-hidden !p-0 ${isClosed ? 'opacity-75 grayscale-[0.3]' : ''} border-2 border-transparent hover:border-blue-500/20 transition-all`}>
                    {zoomImg && <FullImageModal src={zoomImg} onClose={() => setZoomImg(null)} />}
                    
                    {showAssignModal && (
                        <div className="modal-overlay" onClick={() => setShowAssignModal(false)}>
                            <div className="modal-content animate-in" onClick={e => e.stopPropagation()}>
                                <div className="flex justify-between items-center mb-6">
                                    <h3 className="text-xl font-black">Назначить исполнителя</h3>
                                    <div onClick={() => setShowAssignModal(false)} className="w-8 h-8 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-full">
                                        <i className="fas fa-times opacity-40"></i>
                                    </div>
                                </div>
                                <div className="mb-4">
                                    <div className="text-[10px] uppercase tracking-widest font-black opacity-40 mb-3">Территория</div>
                                    <div className="grid grid-cols-2 gap-2">
                                        {territories.length > 0 ? territories.map(t => (
                                            <div key={t} onClick={() => setSelectedTerritory(t)}
                                                className={`px-4 py-3 rounded-xl border-2 text-center text-sm font-bold transition-all active:scale-95 ${selectedTerritory === t ? 'bg-blue-500 border-blue-500 text-white' : 'border-gray-100 dark:border-gray-800'}`}>
                                                {t}
                                            </div>
                                        )) : (
                                            <p className="text-sm opacity-50 italic col-span-2">Территории не настроены.</p>
                                        )}
                                    </div>
                                </div>
                                <div className="space-y-2 mb-6 max-h-[50vh] overflow-y-auto">
                                    {executors.length > 0 ? executors.map(ex => (
                                        <div key={ex.user_id} onClick={() => handleAssign(ex.user_id)} 
                                             className="flex items-center gap-3 p-4 rounded-2xl bg-gray-50 dark:bg-gray-800/50 active:scale-95 transition-all cursor-pointer">
                                            {ex.avatar ? (
                                                <img src={ex.avatar} className="w-10 h-10 rounded-full object-cover" />
                                            ) : (
                                                <div className="w-10 h-10 rounded-full bg-blue-500 flex items-center justify-center text-white font-bold">
                                                    {(ex.full_name || ex.username || String(ex.user_id) || '?')[0].toUpperCase()}
                                                </div>
                                            )}
                                            <div className="flex-1 overflow-hidden">
                                                <p className="font-bold text-sm truncate">{ex.full_name || ex.username || ex.user_id}</p>
                                                <p className="text-[10px] opacity-50">@{ex.username || 'no_username'}</p>
                                            </div>
                                            <i className="fas fa-chevron-right text-blue-500 opacity-30"></i>
                                        </div>
                                    )) : (
                                        <div className="text-center py-10 opacity-50">
                                            <i className="fas fa-users-slash text-4xl mb-3 block"></i>
                                            <p className="text-sm">Нет доступных исполнителей</p>
                                        </div>
                                    )}
                                </div>
                                <div onClick={() => handleAssign(null)} className="btn-primary !bg-gray-500 py-4">Взять на себя (Крестик)</div>
                            </div>
                        </div>
                    )}

                    {/* Task Photo Section */}
                    <div className="relative h-72 bg-gray-100 dark:bg-gray-800 overflow-hidden group">
                        {task.before_message_id ? (
                            <img src={`/api/uploads/${task.before_message_id}`} 
                                 className="w-full h-full object-cover photo-zoom transition-transform duration-500 group-hover:scale-105" 
                                 onClick={() => setZoomImg(`/api/uploads/${task.before_message_id}`)} />
                        ) : (
                            <div className="w-full h-full flex flex-col items-center justify-center opacity-20">
                                <i className="fas fa-image text-5xl mb-2"></i>
                                <span className="text-[10px] font-bold uppercase">Нет фото</span>
                            </div>
                        )}
                        <div className="absolute top-4 left-4 right-4 flex justify-between items-start pointer-events-none">
                            <span className="bg-black/60 backdrop-blur-md text-white px-3 py-1.5 rounded-full text-[10px] font-black pointer-events-auto">
                                #{task.seq_no}
                            </span>
                            <span className={`badge !text-[10px] !px-3 !py-1.5 backdrop-blur-md shadow-lg pointer-events-auto status-${task.status}`}>
                                {getStatusLabel()}
                            </span>
                        </div>
                    </div>

                    <div className="p-5">
                        <div className="flex items-center gap-2 mb-3">
                            <div className="w-8 h-8 rounded-full bg-blue-500/10 text-blue-500 flex items-center justify-center">
                                <i className="fas fa-map-marker-alt text-xs"></i>
                            </div>
                            <h3 className="font-black text-lg">{task.territory || 'Территория не выбрана'}</h3>
                        </div>

                        {task.location && (
                            <div className="flex items-center gap-2 mb-3 text-sm opacity-70">
                                <i className="fas fa-compass"></i>
                                <span>{task.location}</span>
                            </div>
                        )}

                        {task.description && (
                            <div className="bg-gray-50 dark:bg-gray-800/30 p-4 rounded-2xl mb-4">
                                <p className="text-sm leading-relaxed">{task.description}</p>
                            </div>
                        )}

                        {/* Report Section if completed */}
                        {isClosed && (
                            <div className="mt-4 pt-4 border-t border-gray-100 dark:border-gray-800">
                                <p className="text-[10px] font-black uppercase text-green-500 mb-3">Отчет о выполнении</p>
                                {(task.report_photo || task.after_message_id) && (
                                    <img src={`/api/uploads/${task.report_photo || task.after_message_id}`} 
                                         className="w-full h-40 object-cover rounded-2xl mb-3 photo-zoom border-2 border-green-500/20" 
                                         onClick={() => setZoomImg(`/api/uploads/${task.report_photo || task.after_message_id}`)} />
                                )}
                                {task.report_text && (
                                    <p className="text-sm italic opacity-70">«{task.report_text}»</p>
                                )}
                            </div>
                        )}

                        <div className="flex flex-col gap-2 mt-4 text-[10px] font-bold opacity-40">
                            <div className="flex items-center gap-2">
                                <i className="fas fa-clock"></i>
                                <span>Создано: {new Date(task.created_at).toLocaleString('ru-RU')}</span>
                            </div>
                            <div className="flex items-center gap-2">
                                <i className="fas fa-user-edit"></i>
                                <span>Автор: {task.creator_full_name || task.creator_username}</span>
                            </div>
                        </div>

                        {!isClosed && !showConfirmUI && (
                            <div className="mt-6 space-y-2">
                                {canAssign && task.status === 'open' && (
                                    <div onClick={() => !loading && fetchExecutors()} className="btn-primary w-full flex justify-center items-center gap-2 !rounded-2xl py-4 shadow-lg shadow-blue-500/20">
                                        {loading ? <div className="spinner"></div> : (
                                            <>
                                                <i className="fas fa-user-plus"></i>
                                                <span>Назначить исполнителя</span>
                                            </>
                                        )}
                                    </div>
                                )}
                                
                                {task.status !== 'open' && (
                                    <div onClick={() => setShowConfirmUI(true)} className="btn-primary !bg-green-500 w-full flex justify-center items-center gap-2 !rounded-2xl py-4 shadow-lg shadow-green-500/20">
                                        <i className="fas fa-check-circle"></i>
                                        <span>Подтвердить выполнение</span>
                                    </div>
                                )}
                            </div>
                        )}

                        {showConfirmUI && (
                            <div className="mt-6 p-5 bg-green-50 dark:bg-green-900/10 rounded-3xl border border-green-100 dark:border-green-900/20 animate-in">
                                <p className="text-[10px] font-black uppercase text-green-600 mb-4">Отчет о выполнении</p>
                                
                                <div className="space-y-4">
                                    <label className="block">
                                        <div className={`w-full h-32 rounded-2xl border-2 border-dashed flex flex-col items-center justify-center transition-all ${reportPreview ? 'border-green-500' : 'border-gray-300 opacity-50'}`}>
                                            {reportPreview ? (
                                                <img src={reportPreview} className="w-full h-full object-cover rounded-2xl" />
                                            ) : (
                                                <>
                                                    <i className="fas fa-camera text-2xl mb-2"></i>
                                                    <span className="text-[10px] font-bold">ДОБАВИТЬ ФОТО</span>
                                                </>
                                            )}
                                        </div>
                                        <input type="file" accept="image/*" capture="environment" className="hidden" 
                                            onChange={e => {
                                                const file = e.target.files[0];
                                                if (file) {
                                                    setReportPhoto(file);
                                                    const reader = new FileReader();
                                                    reader.onloadend = () => setReportPreview(reader.result);
                                                    reader.readAsDataURL(file);
                                                }
                                            }} />
                                    </label>

                                    <textarea value={reportText} onChange={e => setReportText(e.target.value)} 
                                        placeholder="Комментарий (необязательно)..."
                                        className="w-full bg-white dark:bg-gray-800 p-4 rounded-2xl text-sm outline-none border border-green-100 dark:border-green-900/20 focus:ring-2 ring-green-500" />
                                    
                                    <div className="flex gap-2">
                                        <div onClick={() => !loading && handleConfirmFix()} className="btn-primary !bg-green-500 flex-1 py-4">
                                            {loading ? <div className="spinner"></div> : 'ОТПРАВИТЬ'}
                                        </div>
                                        <div onClick={() => setShowConfirmUI(false)} className="px-6 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-2xl opacity-50">
                                            <i className="fas fa-times"></i>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        )}

                        {isClosed && !rating && canRateTask && (
                            <div className="mt-6 bg-yellow-50 dark:bg-yellow-900/10 p-5 rounded-3xl border border-yellow-100 dark:border-yellow-900/20 text-center">
                                <p className="text-[10px] font-black uppercase text-yellow-600 mb-3">Оцените работу</p>
                                <div className="flex justify-center gap-4">
                                    {[1,2,3,4,5].map(star => (
                                        <i key={star} onClick={() => handleRate(star)} 
                                           className="far fa-star text-3xl text-yellow-500 active:scale-125 transition-all"></i>
                                    ))}
                                </div>
                            </div>
                        )}
                        
                        {rating > 0 && (
                            <div className="mt-6 flex justify-center gap-2 opacity-30">
                                {[1,2,3,4,5].map(star => (
                                    <i key={star} className={`fas fa-star text-sm ${star <= rating ? 'text-yellow-500' : ''}`}></i>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            );
        };

        const CheckerTaskModal = ({ task, onClose, onAction, enableTaskRating }) => {
            const isClosed = task.status === 'completed' || task.status === 'closed';
            const [zoomImg, setZoomImg] = useState(null);
            const [rating, setRating] = useState(task.rating);

            useEffect(() => {
                setRating(task.rating);
            }, [task.rating]);

            const handleRate = async (val) => {
                if (!enableTaskRating) return;
                setRating(val);
                await onAction(`/api/tasks/${task.id}/rate`, 'POST', { rating: val });
            };

            const statusLabel = (() => {
                if (task.status === 'open') return 'Ожидает';
                if (task.status === 'delegated') return 'Делегировано';
                if (task.status === 'accepted') return 'В работе';
                if (task.status === 'completed' || task.status === 'closed') return 'Завершено';
                return 'В работе';
            })();

            const beforeSrc = task.before_message_id ? `/api/uploads/${task.before_message_id}` : null;
            const afterSrc = (task.report_photo || task.after_message_id) ? `/api/uploads/${task.report_photo || task.after_message_id}` : null;

            return (
                <div className="modal-overlay" onClick={onClose}>
                    <div className="modal-content animate-in" onClick={e => e.stopPropagation()}>
                        {zoomImg && <FullImageModal src={zoomImg} onClose={() => setZoomImg(null)} />}
                        <div className="flex justify-between items-center mb-6">
                            <div>
                                <div className="text-[10px] opacity-50 font-bold">Задача #{task.seq_no}</div>
                                <div className="font-black text-lg">{task.territory || 'Территория не выбрана'}</div>
                            </div>
                            <div className="w-8 h-8 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-full" onClick={onClose}>
                                <i className="fas fa-times opacity-50"></i>
                            </div>
                        </div>

                        <div className="flex items-center justify-between mb-4">
                            <span className={`badge !text-[10px] !px-3 !py-1.5 status-${task.status}`}>{statusLabel}</span>
                            <div className="text-[10px] opacity-50 font-bold">{new Date(task.created_at).toLocaleString('ru-RU')}</div>
                        </div>

                        <div className="grid grid-cols-2 gap-3">
                            <div className="bg-gray-50 dark:bg-gray-800/30 rounded-2xl overflow-hidden">
                                <div className="text-[10px] font-black uppercase opacity-40 px-3 pt-3">До</div>
                                {beforeSrc ? (
                                    <img src={beforeSrc} className="w-full h-48 object-cover photo-zoom" onClick={() => setZoomImg(beforeSrc)} />
                                ) : (
                                    <div className="h-48 flex items-center justify-center opacity-20">
                                        <i className="fas fa-image text-3xl"></i>
                                    </div>
                                )}
                            </div>
                            <div className="bg-gray-50 dark:bg-gray-800/30 rounded-2xl overflow-hidden">
                                <div className="text-[10px] font-black uppercase opacity-40 px-3 pt-3">После</div>
                                {afterSrc ? (
                                    <img src={afterSrc} className="w-full h-48 object-cover photo-zoom" onClick={() => setZoomImg(afterSrc)} />
                                ) : (
                                    <div className="h-48 flex items-center justify-center opacity-20">
                                        <i className="fas fa-image text-3xl"></i>
                                    </div>
                                )}
                            </div>
                        </div>

                        {enableTaskRating && (
                            <div className="mt-6">
                                <div className="text-[10px] font-black uppercase opacity-40 mb-3">Оценка</div>
                                <div className="flex justify-center gap-3">
                                    {[1,2,3,4,5].map(star => (
                                        <i key={star} onClick={() => handleRate(star)}
                                           className={`${star <= (rating || 0) ? 'fas' : 'far'} fa-star text-3xl text-yellow-500 active:scale-125 transition-all`}></i>
                                    ))}
                                </div>
                                {!isClosed && (
                                    <div className="text-center text-[10px] opacity-40 mt-3">Оценка доступна и до, и после закрытия</div>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            );
        };

        const CheckerTaskRow = ({ task, onClick, enableTaskRating }) => {
            const statusLabel = (() => {
                if (task.status === 'open') return 'Ожидает';
                if (task.status === 'delegated') return 'Делегировано';
                if (task.status === 'accepted') return 'В работе';
                if (task.status === 'completed' || task.status === 'closed') return 'Завершено';
                return 'В работе';
            })();

            return (
                <div onClick={onClick} className="p-4 rounded-2xl border border-gray-100 dark:border-gray-800 bg-white/40 dark:bg-gray-800/20 backdrop-blur-md active:scale-[0.99] transition-all cursor-pointer">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-2xl bg-blue-500/10 text-blue-500 flex items-center justify-center flex-shrink-0">
                            <i className="fas fa-clipboard-check"></i>
                        </div>
                        <div className="flex-1 overflow-hidden">
                            <div className="flex items-center justify-between gap-2">
                                <div className="font-black text-sm truncate">{task.territory || 'Территория не выбрана'}</div>
                                <div className="text-[10px] opacity-40 font-bold flex-shrink-0">#{task.seq_no}</div>
                            </div>
                            <div className="flex items-center justify-between gap-2 mt-1">
                                <div className="text-[10px] opacity-40 font-bold truncate">{statusLabel}</div>
                                {enableTaskRating && (
                                    <div className="flex items-center gap-1 opacity-40 flex-shrink-0">
                                        {[1,2,3,4,5].map(star => (
                                            <i key={star} className={`fas fa-star text-[10px] ${star <= (task.rating || 0) ? 'text-yellow-500' : ''}`}></i>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>
                        <i className="fas fa-chevron-right opacity-20"></i>
                    </div>
                </div>
            );
        };

        const NewTaskModal = ({ territories, onClose, onAction }) => {
            const [desc, setDesc] = useState('');
            const [photo, setPhoto] = useState(null);
            const [preview, setPreview] = useState(null);
            const [loading, setLoading] = useState(false);

            const handleCreate = async () => {
                setLoading(true);
                const fd = new FormData();
                fd.append('description', desc);
                fd.append('chat_id', window.Telegram.WebApp.initDataUnsafe.chat?.id || window.Telegram.WebApp.initDataUnsafe.user?.id || 0);
                if (photo) fd.append('photo', photo);
                
                await onAction('/api/tasks', 'POST', fd, true);
                setLoading(false);
                onClose();
            };

            return (
                <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
                    <div className="modal-content animate-in slide-in-from-bottom duration-300">
                        <div className="flex justify-between items-center mb-6">
                            <h2 className="text-xl font-black">Новая задача</h2>
                            <div className="w-8 h-8 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-full" onClick={onClose}>
                                <i className="fas fa-times opacity-50"></i>
                            </div>
                        </div>
                        
                        <div className="space-y-6 mb-8">
                            <div>
                                <label className="text-[10px] font-bold uppercase opacity-40 mb-3 block">Что случилось?</label>
                                <textarea value={desc} onChange={e => setDesc(e.target.value)} placeholder="Опишите проблему..." rows="3"
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-4 rounded-xl outline-none focus:ring-2 ring-blue-500 resize-none" />
                            </div>

                            <label className="flex items-center gap-4 p-4 bg-gray-50 dark:bg-gray-800 rounded-xl cursor-pointer active:scale-95 transition-transform">
                                <div className="w-12 h-12 bg-blue-100 dark:bg-blue-900/30 rounded-xl flex items-center justify-center">
                                    <i className="fas fa-camera text-xl text-blue-500"></i>
                                </div>
                                <div className="flex-1">
                                    <p className="font-bold text-sm">{photo ? 'Фото выбрано ✅' : 'Сделать фото'}</p>
                                    <p className="text-xs opacity-50">Нажмите, чтобы открыть камеру</p>
                                </div>
                                <input type="file" accept="image/*" capture="environment" className="hidden" 
                                    onChange={e => {
                                        const file = e.target.files[0];
                                        if (file) { setPhoto(file); setPreview(URL.createObjectURL(file)); }
                                    }} />
                            </label>
                            {preview && <img src={preview} className="h-48 w-full object-cover rounded-2xl shadow-lg" />}
                        </div>

                        <div onClick={!loading ? handleCreate : null} className="btn-primary text-lg py-4 shadow-xl shadow-blue-500/20 flex justify-center items-center gap-3">
                            {loading ? <div className="spinner"></div> : 'Создать задачу'}
                        </div>
                    </div>
                </div>
            );
        };

        const UserManagementModal = ({ type, users, admins, onClose, onAction }) => {
            const [search, setSearch] = useState('');
            const [localUsers, setLocalUsers] = useState(users);
            const [isSearching, setIsSearching] = useState(false);

            useEffect(() => {
                setLocalUsers(users);
            }, [users]);

            const filtered = localUsers.filter(u => 
                (u.username?.toLowerCase().includes(search.toLowerCase()) || 
                 u.full_name?.toLowerCase().includes(search.toLowerCase()))
            );

            // Debounced search for new users if not found in local list
            useEffect(() => {
                if (search.length > 2 && filtered.length === 0) {
                    const timer = setTimeout(async () => {
                        setIsSearching(true);
                        try {
                            const res = await fetchApi(`/api/users/search?q=${encodeURIComponent(search)}`);
                            if (res && Array.isArray(res)) {
                                // Merge with local users avoiding duplicates
                                setLocalUsers(prev => {
                                    const existingIds = new Set(prev.map(u => u.user_id));
                                    const newOnes = res.filter(u => !existingIds.has(u.user_id));
                                    return [...prev, ...newOnes];
                                });
                            }
                        } catch (e) {
                            console.error("Search error", e);
                        } finally {
                            setIsSearching(false);
                        }
                    }, 500);
                    return () => clearTimeout(timer);
                }
            }, [search]);

            return (
                <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
                    <div className="modal-content animate-in slide-in-from-bottom duration-300">
                        <div className="flex justify-between items-center mb-4">
                            <h2 className="text-xl font-black">{type === 'admin' ? 'Управление админами' : 'Добавить пользователя'}</h2>
                            <div className="w-8 h-8 flex items-center justify-center bg-gray-100 dark:bg-gray-800 rounded-full" onClick={onClose}>
                                <i className="fas fa-times opacity-50"></i>
                            </div>
                        </div>
                        
                        <div className="relative mb-4">
                            <input type="text" value={search} onChange={e => setSearch(e.target.value)} 
                                placeholder="Поиск по имени или @username..."
                                className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-4 rounded-xl outline-none ring-2 ring-transparent focus:ring-blue-500 transition-all" />
                            {isSearching && (
                                <div className="absolute right-4 top-4">
                                    <div className="spinner !border-blue-500 !border-t-transparent"></div>
                                </div>
                            )}
                        </div>

                        <div className="max-h-[50vh] overflow-y-auto space-y-2 pr-2">
                            {filtered.length > 0 ? filtered.map(u => {
                                const userKey = u.username || ('id' + u.user_id);
                                const isAdmin = admins.includes(userKey);
                                return (
                                    <div key={u.user_id} className="flex justify-between items-center p-4 bg-gray-50 dark:bg-gray-800/50 rounded-2xl border border-transparent hover:border-blue-500/10 transition-all">
                                        <div className="flex-1">
                                            <div className="font-bold text-sm">{u.full_name || 'Без имени'}</div>
                                            <div className="text-[10px] opacity-50 font-mono">@{u.username || 'id' + u.user_id}</div>
                                        </div>
                                        {type === 'admin' ? (
                                            <div onClick={() => onAction('/api/admins/toggle', 'POST', { user_id: u.user_id, username: userKey, is_admin: !isAdmin })}
                                                className={`w-8 h-8 rounded-full flex items-center justify-center transition-all ${isAdmin ? 'bg-blue-500 text-white' : 'border-2 border-gray-200 dark:border-gray-700'}`}>
                                                {isAdmin ? <i className="fas fa-check text-xs"></i> : <i className="fas fa-plus text-xs opacity-30"></i>}
                                            </div>
                                        ) : (
                                            u.is_active ? (
                                                <div className="bg-green-500/10 text-green-500 px-3 py-1 rounded-lg text-[10px] font-bold">
                                                    АКТИВЕН
                                                </div>
                                            ) : (
                                                <div onClick={() => onAction('/api/users/toggle-active', 'POST', { user_id: u.user_id, is_active: 1 })}
                                                    className="bg-blue-500 text-white px-5 py-2 rounded-xl text-xs font-black active:scale-95 transition-transform shadow-lg shadow-blue-500/20">
                                                    ДОБАВИТЬ
                                                </div>
                                            )
                                        )}
                                    </div>
                                );
                            }) : (
                                <div className="text-center py-12 opacity-30">
                                    <i className="fas fa-user-slash text-3xl mb-3 block"></i>
                                    <p className="text-sm">{search.length > 0 ? 'Никого не нашли' : 'Начните поиск...'}</p>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            );
        };

        const SettingsView = ({ territories, onAction, refreshTerritories, userConfig }) => {
            const [newName, setNewName] = useState('');
            const [settings, setSettings] = useState({});
            const [loading, setLoading] = useState(false);
            const [userModal, setUserModal] = useState(null); // 'admin' or 'user'
            const [allUsers, setAllUsers] = useState({ users: [], admins: [] });
            const tg = window.Telegram.WebApp;

            useEffect(() => {
                fetch('/api/settings', { headers: { 'X-Telegram-Init-Data': tg.initData } })
                    .then(r => r.json())
                    .then(setSettings);
            }, []);

            const loadAllUsers = async () => {
                const res = await fetch('/api/users/all', { headers: { 'X-Telegram-Init-Data': tg.initData } });
                if (res.ok) setAllUsers(await res.json());
            };

            const handleUserAction = async (url, method, body) => {
                try {
                    const res = await onAction(url, method, body);
                    const errMsg = (res && (res.error || res.detail)) ? (res.error || res.detail) : null;
                    if (errMsg) {
                        tg.showAlert(`Ошибка: ${errMsg}`);
                    } else {
                        loadAllUsers();
                    }
                } catch (e) {
                    console.error("Action error", e);
                    tg.showAlert("Произошла ошибка при выполнении действия");
                }
            };

            const updateSetting = async (key, value) => {
                const newSettings = { ...settings, [key]: value };
                setSettings(newSettings);
                await onAction('/api/settings', 'POST', { [key]: value });
            };

            const addTerritory = async () => {
                if (!newName) return;
                setLoading(true);
                await onAction('/api/territories', 'POST', { name: newName });
                setNewName('');
                refreshTerritories();
                setLoading(false);
            };

            const deleteTerritory = async (name) => {
                if (!confirm(`Удалить территорию "${name}"?`)) return;
                await onAction(`/api/territories/${name}`, 'DELETE');
                refreshTerritories();
            };

            return (
                <div className="p-4 space-y-6 pb-32">
                    <h2 className="text-2xl font-black">Настройки</h2>

                    <div className="grid grid-cols-2 gap-3">
                        <div onClick={() => { loadAllUsers(); setUserModal('user'); }} 
                            className="bg-blue-500 text-white p-4 rounded-2xl shadow-lg active:scale-95 transition-transform flex flex-col items-center justify-center gap-2">
                            <i className="fas fa-user-plus text-xl"></i>
                            <span className="text-xs font-bold">Добавить пользователя</span>
                        </div>
                        <div onClick={() => { loadAllUsers(); setUserModal('admin'); }} 
                            className="bg-indigo-600 text-white p-4 rounded-2xl shadow-lg active:scale-95 transition-transform flex flex-col items-center justify-center gap-2">
                            <i className="fas fa-user-shield text-xl"></i>
                            <span className="text-xs font-bold">Управление админами</span>
                        </div>
                    </div>

                    {userModal && (
                        <UserManagementModal 
                            type={userModal} 
                            users={allUsers.users} 
                            admins={allUsers.admins}
                            onClose={() => setUserModal(null)}
                            onAction={handleUserAction}
                        />
                    )}
                    
                    <div className="card !m-0">
                        <h3 className="font-bold mb-4 uppercase text-[10px] opacity-40">Системные настройки (SLA)</h3>
                        <div className="space-y-4">
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">Время на принятие (мин)</label>
                                <input type="number" value={settings.sla_accept || ''} 
                                    onChange={e => updateSetting('sla_accept', e.target.value)}
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none" />
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">Время на выполнение (мин)</label>
                                <input type="number" value={settings.sla_fix || ''} 
                                    onChange={e => updateSetting('sla_fix', e.target.value)}
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none" />
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">Уведомления в личку</label>
                                <select value={settings.private_notifications || 'on'} 
                                    onChange={e => updateSetting('private_notifications', e.target.value)}
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none">
                                    <option value="on">Включены</option>
                                    <option value="off">Выключены (только группа)</option>
                                </select>
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">Режим уведомлений</label>
                                <select value={settings.new_mode || ''} 
                                    onChange={e => updateSetting('new_mode', e.target.value)}
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none">
                                    <option value="group">Только в группе</option>
                                    <option value="admin_private">Группа + админ в личку</option>
                                    <option value="any_private">Группа + все в личку</option>
                                </select>
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">Режим оценки Проверяющим</label>
                                <select value={settings.checker_rating_mode || 'shift'} 
                                    onChange={e => updateSetting('checker_rating_mode', e.target.value)}
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none">
                                    <option value="shift">Оценка смены (после закрытия)</option>
                                    <option value="task">Оценка задач (старый режим)</option>
                                </select>
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">ID разрешенных чатов (через запятую)</label>
                                <input type="text" value={settings.authorized_chats || ''} 
                                    onChange={e => updateSetting('authorized_chats', e.target.value)}
                                    placeholder="-100123, -100456"
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none" />
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">ID основной группы (куда слать задачи)</label>
                                <input type="text" value={settings.main_group_id || ''} 
                                    onChange={e => updateSetting('main_group_id', e.target.value)}
                                    placeholder="-100..."
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none" />
                            </div>
                            <div>
                                <label className="text-xs font-medium opacity-60 mb-1 block">ID канала для постов (опционально)</label>
                                <input type="text" value={settings.post_channel_id || ''} 
                                    onChange={e => updateSetting('post_channel_id', e.target.value)}
                                    placeholder="-100..."
                                    className="w-full bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none" />
                            </div>
                        </div>
                    </div>

                    <div className="card !m-0">
                        <h3 className="font-bold mb-4 uppercase text-[10px] opacity-40">Управление территориями</h3>
                        <div className="flex gap-2 mb-4">
                            <input value={newName} onChange={e => setNewName(e.target.value)} placeholder="Название..." 
                                className="flex-1 bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none" />
                            <div onClick={!loading ? addTerritory : null} className="bg-blue-500 text-white p-3 rounded-xl px-6 font-bold flex items-center">
                                {loading ? <div className="spinner"></div> : 'OK'}
                            </div>
                        </div>
                        <div className="space-y-2">
                            {territories.map(t => (
                                <div key={t} className="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800/50 rounded-xl">
                                    <span className="font-medium">{t}</span>
                                    <i className="fas fa-trash text-red-400 p-2" onClick={() => deleteTerritory(t)}></i>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="text-center opacity-30 text-[10px] py-4">
                        CleanApp v2.1.0 • Build 2026<br/>Telegram Mini App Engine
                    </div>
                </div>
            );
        };

        const StatsView = ({ isOwner, canManageRoles, fetchUsers, setShowSettingsMenu }) => {
            const [stats, setStats] = useState(null);
            const [newAdmin, setNewAdmin] = useState('');
            const tg = window.Telegram.WebApp;

            const loadStats = () => {
                fetch('/api/stats/detailed', { headers: { 'X-Telegram-Init-Data': tg.initData } })
                    .then(r => r.json())
                    .then(setStats);
            };

            useEffect(() => {
                loadStats();
            }, []);

            const addAdmin = async () => {
                if (!newAdmin) return;
                const res = await fetch('/api/admins/toggle', {
                    method: 'POST',
                    headers: { 
                        'X-Telegram-Init-Data': tg.initData,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ username: newAdmin, is_admin: true })
                });
                if (res.ok) {
                    setNewAdmin('');
                    loadStats();
                    tg.HapticFeedback.notificationOccurred('success');
                }
            };

            const deleteAdmin = async (username) => {
                const res = await fetch('/api/admins/toggle', {
                    method: 'POST',
                    headers: { 
                        'X-Telegram-Init-Data': tg.initData,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ username, is_admin: false })
                });
                if (res.ok) {
                    loadStats();
                    tg.HapticFeedback.notificationOccurred('success');
                }
            };

            if (!stats) return <div className="p-12 text-center opacity-30"><div className="spinner mx-auto mb-4"></div>Загрузка статистики...</div>;
            if (stats.detail === "Access denied") return <div className="p-12 text-center">Доступ ограничен</div>;

            return (
                <div className="p-4 space-y-6 pb-24 animate-in fade-in duration-500">
                    <h2 className="text-2xl font-black">Мега-Статистика</h2>
                    
                    <div className="grid grid-cols-2 gap-3">
                        <div className="stat-card">
                            <div className="stat-value">{stats.total}</div>
                            <div className="stat-label">Всего задач</div>
                        </div>
                        <div className="stat-card">
                            <div className="stat-value text-green-500">{stats.closed}</div>
                            <div className="stat-label">Выполнено</div>
                        </div>
                        <div className="stat-card">
                            <div className="stat-value">{stats.today_created}</div>
                            <div className="stat-label">Создано сегодня</div>
                        </div>
                        <div className="stat-card">
                            <div className="stat-value text-green-500">{stats.today_closed}</div>
                            <div className="stat-label">Закрыто сегодня</div>
                        </div>
                    </div>

                    <div className="card !m-0 space-y-4">
                        <h3 className="stat-label">Средние показатели</h3>
                        <div className="flex justify-between items-center">
                            <span className="text-sm font-medium">Реакция админов</span>
                            <span className="font-bold text-blue-500">{stats.avg_react} мин</span>
                        </div>
                        <div className="flex justify-between items-center">
                            <span className="text-sm font-medium">Время до закрытия</span>
                            <span className="font-bold text-blue-500">{stats.avg_fix} мин</span>
                        </div>
                    </div>

                    <div className="card !m-0">
                        <h3 className="stat-label mb-4">Детально по территориям</h3>
                        <div className="space-y-4">
                            {stats.territory_stats.map((t, i) => (
                                <div key={i} className="space-y-1">
                                    <div className="flex justify-between text-sm">
                                        <span className="font-bold">{t.territory}</span>
                                        <span className="opacity-50">{t.count} зад.</span>
                                    </div>
                                    <div className="flex gap-4 text-[10px] opacity-60">
                                        <span>Реакция: <b>{Math.round(t.avg_react || 0)}м</b></span>
                                        <span>Закрытие: <b>{Math.round(t.avg_fix || 0)}м</b></span>
                                    </div>
                                    <div className="h-1 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                                        <div className="h-full bg-blue-500" style={{width: `${(t.count / stats.total) * 100}%`}}></div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {stats.performer_stats?.length > 0 && (
                        <div className="card !m-0">
                            <h3 className="stat-label mb-4">Личная эффективность админов</h3>
                            <div className="space-y-6">
                                {stats.performer_stats.map((p, i) => (
                                    <div key={i} className="space-y-2 p-3 bg-gray-50 dark:bg-gray-800/30 rounded-xl">
                                        <div className="flex justify-between items-center">
                                            <span className="font-black text-sm">{p.performer_full_name}</span>
                                            <span className="bg-green-500 text-white px-2 py-0.5 rounded-lg font-bold text-[10px]">{p.count} выполнено</span>
                                        </div>
                                        <div className="grid grid-cols-2 gap-4 text-[10px]">
                                            <div className="flex flex-col">
                                                <span className="opacity-50 uppercase font-bold">Реакция</span>
                                                <span className="text-blue-500 font-black">{Math.round(p.avg_react || 0)} мин</span>
                                            </div>
                                            <div className="flex flex-col">
                                                <span className="opacity-50 uppercase font-bold">Устранение</span>
                                                <span className="text-green-500 font-black">{Math.round(p.avg_fix || 0)} мин</span>
                                            </div>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {canManageRoles && (
                        <div className="card !m-0">
                            <h3 className="stat-label mb-4 uppercase text-[10px] tracking-widest">Управление ролями</h3>
                            <p className="text-xs opacity-50 mb-4">Настройте права доступа для всех пользователей системы.</p>
                            <div onClick={() => { fetchUsers(); setShowSettingsMenu(true); }} className="btn-primary py-3 text-sm">
                                <i className="fas fa-users-cog mr-2"></i> Открыть редактор ролей
                            </div>
                        </div>
                    )}

                    {isOwner && (
                        <div className="card !m-0">
                            <h3 className="stat-label mb-4 uppercase text-[10px] tracking-widest">Управление доступом</h3>
                            <div className="flex gap-2 mb-4">
                                <input value={newAdmin} onChange={e => setNewAdmin(e.target.value)} placeholder="@username" 
                                    className="flex-1 bg-[var(--tg-theme-secondary-bg-color)] p-3 rounded-xl outline-none text-sm" />
                                <div onClick={addAdmin} className="bg-blue-500 text-white p-3 rounded-xl px-4 font-bold text-sm">
                                    Добавить
                                </div>
                            </div>
                            <div className="space-y-2">
                                {stats.admins?.map(a => (
                                    <div key={a} className="flex justify-between items-center p-3 bg-gray-50 dark:bg-gray-800/50 rounded-xl text-sm">
                                        <span className="font-medium">@{a}</span>
                                        <i className="fas fa-times text-red-400 p-1" onClick={() => deleteAdmin(a)}></i>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            );
        };

        const App = () => {
            const [view, setView] = useState('tasks'); // 'tasks', 'stats', 'settings'
            const [tasks, setTasks] = useState([]);
            const [territories, setTerritories] = useState([]);
            const [showNewTask, setShowNewTask] = useState(false);
            const [showArchive, setShowArchive] = useState(false);
            const [selectedTask, setSelectedTask] = useState(null);
            const [toast, setToast] = useState(null);
            const [userConfig, setUserConfig] = useState({ owner_id: 0 });
            const [appSettings, setAppSettings] = useState({});
            const [shiftActive, setShiftActive] = useState(false);
            const [shiftInfo, setShiftInfo] = useState(null);
            const [shiftMeta, setShiftMeta] = useState({ lastClosedShift: null, needsRating: false, myShiftRating: null });
            const [loadingShift, setLoadingShift] = useState(true);
            const [users, setUsers] = useState([]);
            const [showSettingsMenu, setShowSettingsMenu] = useState(false);
            const tg = window.Telegram.WebApp;

            const fetchUsers = async () => {
                const data = await fetchApi('/api/users/all');
                if (data && data.users) setUsers(data.users);
            };

            const updateRole = async (userId, newRole) => {
                await fetchApi(`/api/users/${userId}/role`, 'POST', { role: newRole });
                fetchUsers();
                tg.HapticFeedback.notificationOccurred('success');
            };

            const toggleUserActive = async (userId, currentActive) => {
                await fetchApi('/api/users/toggle-active', 'POST', { user_id: userId, is_active: currentActive ? 0 : 1 });
                fetchUsers();
            };

            const refreshData = async () => {
                try {
                    const res = await fetch('/api/tasks', { headers: { 'X-Telegram-Init-Data': tg.initData } });
                    if (res.status === 401) {
                        setTasks({ error: 'unauthorized' });
                        setLoadingShift(false);
                        return;
                    }
                    const data = await res.json();
                    setTasks(data);

                    const shiftRes = await fetch('/api/shift/status', { headers: { 'X-Telegram-Init-Data': tg.initData } });
                    if (shiftRes.status === 401) {
                        setLoadingShift(false);
                        return;
                    }
                    const shiftData = await shiftRes.json();
                    setShiftActive(shiftData.active);
                    setShiftInfo(shiftData.shift || null);

                    setShiftMeta({
                        lastClosedShift: shiftData.last_closed_shift || null,
                        needsRating: !!shiftData.needs_rating,
                        myShiftRating: shiftData.my_shift_rating || null
                    });

                    const settingsRes = await fetch('/api/settings', { headers: { 'X-Telegram-Init-Data': tg.initData } });
                    if (settingsRes.status !== 401) {
                        const settingsData = await settingsRes.json();
                        setAppSettings(settingsData || {});
                    }
                } catch (e) {
                    console.error("Refresh error:", e);
                } finally {
                    setLoadingShift(false);
                }
            };

            const refreshTerritories = async () => {
                const res = await fetch('/api/territories', { headers: { 'X-Telegram-Init-Data': tg.initData } });
                if (res.status === 401) return;
                const data = await res.json();
                setTerritories(data);
            };

            useEffect(() => {
                tg.ready();
                tg.expand();
                if (tg.colorScheme === 'dark') document.body.classList.add('dark');
                refreshData();
                refreshTerritories();
                fetch('/api/config', { headers: { 'X-Telegram-Init-Data': tg.initData } })
                    .then(r => r.status === 401 ? null : r.json())
                    .then(data => data && setUserConfig(data));
            }, []);

            const showToast = (msg) => {
                setToast(msg);
                setTimeout(() => setToast(null), 3000);
            };

            const handleAction = async (url, method = 'POST', body = null, isMultipart = false) => {
                try {
                    const options = {
                        method,
                        headers: { 'X-Telegram-Init-Data': tg.initData }
                    };
                    if (body) {
                        if (isMultipart) options.body = body;
                        else {
                            options.body = JSON.stringify(body);
                            options.headers['Content-Type'] = 'application/json';
                        }
                    }
                    const res = await fetch(url, options);
                    let result = null;
                    try {
                        result = await res.json();
                    } catch (e) {
                        result = null;
                    }
                    
                    if (!res.ok) {
                        const msg = (result && (result.detail || result.error)) ? (result.detail || result.error) : `HTTP ${res.status}`;
                        tg.HapticFeedback.notificationOccurred('error');
                        tg.showAlert("Ошибка: " + msg);
                        return result || { error: msg };
                    }
                    
                    if (result && result.status === 'ok') {
                        if (url.includes('/fix')) showToast("Задача завершена и перенесена в архив");
                        if (url.includes('/accept')) showToast("Задача принята в работу");
                        if (url.includes('/tasks') && method === 'POST') showToast("Задача успешно создана");
                        if (url.includes('/shift/start')) showToast("Смена начата!");
                        if (url.includes('/shift/end')) showToast("Смена завершена");
                        if (url.includes('/shifts/') && url.includes('/rate')) showToast("Смена оценена");
                        
                        tg.HapticFeedback.notificationOccurred('success');
                        refreshData();
                    }
                    return result;
                } catch (e) {
                    tg.HapticFeedback.notificationOccurred('error');
                    tg.showAlert("Ошибка: " + e.message);
                }
            };

            const isTaskList = Array.isArray(tasks);
            const isOwner = tg.initDataUnsafe.user?.id === userConfig.owner_id;
            const isAdmin = userConfig.is_admin || isOwner;
            const isShiftLead = tg.initDataUnsafe.user?.id === shiftInfo?.leader_user_id;
            const isShiftManager = userConfig?.role === 'main_admin' || userConfig?.role === 'isp_admin';
            const isChecker = userConfig?.role === 'sled';
            const canManageRoles = isOwner || userConfig?.role === 'main_admin';

            const checkerRatingMode = appSettings?.checker_rating_mode || 'shift';
            const enableCheckerTaskRating = checkerRatingMode === 'task';

            const openTasks = isTaskList ? tasks.filter(t => t.status !== 'closed' && t.status !== 'completed').sort((a, b) => {
                // Sorting: SLA overdue first, then by ID
                const getPriority = (task) => {
                    const created = new Date(task.created_at).getTime();
                    const now = new Date().getTime();
                    const diffMin = (now - created) / (1000 * 60);
                    if (task.status === 'open' && diffMin > 10) return 2; // Critical SLA for acceptance
                    if (task.status === 'accepted' && diffMin > 30) return 1; // Critical SLA for fixing
                    return 0;
                };
                const pa = getPriority(a);
                const pb = getPriority(b);
                if (pa !== pb) return pb - pa;
                return b.id - a.id;
            }) : [];

            const today = new Date().toISOString().split('T')[0];
            const todayClosed = isTaskList ? tasks.filter(t => (t.status === 'closed' || t.status === 'completed') && t.fixed_at?.startsWith(today)).sort((a, b) => b.id - a.id) : [];
            const archiveTasks = isTaskList ? tasks.filter(t => (t.status === 'closed' || t.status === 'completed') && !t.fixed_at?.startsWith(today)).sort((a, b) => b.id - a.id) : [];
            
            const overdueCount = openTasks.filter(t => {
                const created = new Date(t.created_at).getTime();
                const diffMin = (new Date().getTime() - created) / (1000 * 60);
                return (t.status === 'open' && diffMin > 10) || (t.status === 'accepted' && diffMin > 30);
            }).length;
            
            const shouldAskShiftRating = isChecker && checkerRatingMode === 'shift' && !shiftActive && shiftMeta?.lastClosedShift && shiftMeta?.needsRating;

            if (tasks && tasks.error === 'unauthorized') {
                return (
                    <div className="p-12 text-center">
                        <div className="w-20 h-20 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center mx-auto mb-6">
                            <i className="fas fa-user-lock text-3xl text-red-500"></i>
                        </div>
                        <h2 className="text-xl font-black mb-2">Ошибка авторизации</h2>
                        <p className="opacity-50 text-sm">Не удалось подтвердить ваш профиль Telegram. Попробуйте перезапустить приложение.</p>
                    </div>
                );
            }

            if (loadingShift) return <div className="p-12 text-center opacity-30"><div className="spinner mx-auto mb-4"></div>Загрузка...</div>;

            return (
                <div className="min-h-screen">
                    {toast && <div className="toast">{toast}</div>}
                    {showSettingsMenu && (
                        <SettingsMenu 
                            users={users} 
                            fetchUsers={fetchUsers} 
                            updateRole={updateRole} 
                            toggleUserActive={toggleUserActive} 
                            onClose={() => setShowSettingsMenu(false)} 
                        />
                    )}

                    {overdueCount > 0 && view === 'tasks' && (
                        <div className="mx-4 mt-2 p-3 bg-red-600 text-white rounded-2xl flex items-center gap-3 animate-pulse shadow-lg shadow-red-500/40">
                            <i className="fas fa-exclamation-triangle text-xl"></i>
                            <div className="flex-1">
                                <div className="font-black text-sm uppercase">SLA Нарушен!</div>
                                <div className="text-[10px] opacity-80">У вас {overdueCount} {overdueCount === 1 ? 'задача висит' : 'задачи висят'} слишком долго!</div>
                            </div>
                        </div>
                    )}

                    {!shiftActive && view === 'tasks' && (
                        shouldAskShiftRating ? (
                            <div className="shift-overlay animate-in">
                                <div className="w-24 h-24 bg-yellow-100 dark:bg-yellow-900/20 rounded-full flex items-center justify-center mb-6">
                                    <i className="fas fa-star text-4xl text-yellow-500"></i>
                                </div>
                                <h2 className="text-2xl font-black mb-2">Оцените работу смены</h2>
                                <p className="opacity-50 mb-4 max-w-xs">
                                    Смена закрыта. Поставьте оценку.
                                </p>
                                <div className="opacity-40 text-[10px] font-bold mb-8">
                                    Руководитель: {shiftMeta.lastClosedShift.leader_full_name || ('@' + shiftMeta.lastClosedShift.leader_username)}
                                </div>
                                <div className="flex justify-center gap-4">
                                    {[1,2,3,4,5].map(star => (
                                        <i key={star} onClick={() => handleAction(`/api/shifts/${shiftMeta.lastClosedShift.id}/rate`, 'POST', { rating: star })}
                                           className="far fa-star text-4xl text-yellow-500 active:scale-125 transition-all"></i>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <div className="shift-overlay animate-in">
                                <div className="w-24 h-24 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center mb-6">
                                    <i className="fas fa-clock-rotate-left text-4xl text-red-500"></i>
                                </div>
                                <h2 className="text-2xl font-black mb-2">Смена не начата</h2>
                                <p className="opacity-50 mb-8 max-w-xs">{isShiftManager ? 'Чтобы просматривать и создавать задачи, необходимо начать рабочую смену.' : 'Ожидайте, пока руководитель смены начнет рабочую смену.'}</p>
                                {canManageRoles && (
                                    <div onClick={() => { fetchUsers(); setShowSettingsMenu(true); }} className="btn-primary w-full max-w-xs py-4 text-lg mb-4">
                                        Управление ролями
                                    </div>
                                )}
                                {isShiftManager && (
                                    <div onClick={() => handleAction('/api/shift/start')} className="btn-primary w-full max-w-xs py-4 text-lg">
                                        Начать смену
                                    </div>
                                )}
                            </div>
                        )
                    )}

                    {view === 'tasks' && (
                        isChecker ? (
                            <div className="p-4 space-y-4 pb-32 animate-in fade-in slide-in-from-top-4 duration-500">
                                <div className="flex justify-between items-center mb-6">
                                    <h1 className="text-3xl font-black">Задачи</h1>
                                    <div className="flex gap-2">
                                        <div onClick={() => setShowNewTask(true)} className="w-12 h-12 bg-blue-500 text-white rounded-2xl flex items-center justify-center shadow-lg shadow-blue-500/30 active:scale-90 active:rotate-12 transition-all">
                                            <i className="fas fa-plus text-xl"></i>
                                        </div>
                                    </div>
                                </div>

                                {selectedTask && (
                                    <CheckerTaskModal task={selectedTask} onClose={() => setSelectedTask(null)} onAction={handleAction} enableTaskRating={enableCheckerTaskRating} />
                                )}

                                {isTaskList && tasks.length > 0 ? (
                                    <div className="space-y-3">
                                        {[...tasks].sort((a, b) => b.id - a.id).map(t => (
                                            <CheckerTaskRow key={t.id} task={t} onClick={() => setSelectedTask(t)} enableTaskRating={enableCheckerTaskRating} />
                                        ))}
                                    </div>
                                ) : (
                                    <div className="text-center py-10 opacity-30">
                                        <i className="fas fa-check-circle text-5xl mb-4"></i>
                                        <p className="font-bold">Задач пока нет</p>
                                    </div>
                                )}
                            </div>
                        ) : (
                            <div className={`p-4 space-y-4 pb-32 animate-in fade-in slide-in-from-top-4 duration-500 ${isShiftLead ? 'shift-lead-mode' : ''}`}>
                                {shiftInfo && (
                                    <div className="flex items-center gap-3 px-4 py-3 bg-white/40 dark:bg-gray-800/40 backdrop-blur-md rounded-2xl border border-white/20 shadow-sm mb-4">
                                        <div className="w-10 h-10 rounded-full bg-gradient-to-tr from-blue-500 to-indigo-600 flex items-center justify-center text-white shadow-lg">
                                            <i className="fas fa-user-tie"></i>
                                        </div>
                                        <div className="flex-1 overflow-hidden">
                                            <p className="text-[9px] uppercase font-black opacity-40 tracking-tighter">Ответственный смены</p>
                                            <p className="font-bold text-sm truncate">{shiftInfo.leader_full_name}</p>
                                        </div>
                                        {isShiftLead && (
                                            <div className="bg-blue-500 text-[9px] text-white px-2 py-1 rounded-lg font-black uppercase tracking-widest shadow-lg shadow-blue-500/30">
                                                Вы
                                            </div>
                                        )}
                                    </div>
                                )}

                                <div className="flex justify-between items-center mb-6">
                                    <h1 className="text-3xl font-black">Задачи</h1>
                                    <div className="flex gap-2">
                                        {isShiftManager && (
                                            <div onClick={() => {
                                                tg.showConfirm("Вы уверены, что хотите завершить смену?", (ok) => {
                                                    if (ok) handleAction('/api/shift/end');
                                                });
                                            }} className="w-12 h-12 bg-gray-100 dark:bg-gray-800 text-red-500 rounded-2xl flex items-center justify-center shadow-lg active:scale-90 active:rotate-12 transition-all">
                                                <i className="fas fa-power-off text-xl"></i>
                                            </div>
                                        )}
                                        <div onClick={() => setShowNewTask(true)} className="w-12 h-12 bg-blue-500 text-white rounded-2xl flex items-center justify-center shadow-lg shadow-blue-500/30 active:scale-90 active:rotate-12 transition-all">
                                            <i className="fas fa-plus text-xl"></i>
                                        </div>
                                    </div>
                                </div>

                                {openTasks.length > 0 ? openTasks.map(t => (
                                    <TaskCard key={t.id} task={t} onAction={handleAction} territories={territories} isShiftLead={isShiftLead} config={userConfig} />
                                )) : (
                                    <div className="text-center py-10 opacity-30">
                                        <i className="fas fa-check-circle text-5xl mb-4"></i>
                                        <p className="font-bold">Все задачи выполнены!</p>
                                    </div>
                                )}

                                {todayClosed.length > 0 && (
                                    <div className="mt-8">
                                        <div className="archive-header !p-0 mb-4">
                                            <span>Выполнено сегодня ({todayClosed.length})</span>
                                        </div>
                                        <div className="space-y-4">
                                            {todayClosed.map(t => (
                                                <TaskCard key={t.id} task={t} onAction={handleAction} territories={territories} isShiftLead={isShiftLead} config={userConfig} />
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {archiveTasks.length > 0 && (
                                    <div className="mt-8">
                                        <div onClick={() => setShowArchive(!showArchive)} className="flex items-center gap-2 opacity-40 uppercase text-[10px] font-bold mb-4 tracking-widest cursor-pointer">
                                            <i className={`fas fa-chevron-${showArchive ? 'down' : 'right'}`}></i>
                                            Архив прошлых дней ({archiveTasks.length})
                                        </div>
                                        {showArchive && (
                                            <div className="space-y-4 animate-in fade-in duration-500">
                                                {archiveTasks.map(t => (
                                                    <TaskCard key={t.id} task={t} onAction={handleAction} territories={territories} isShiftLead={isShiftLead} config={userConfig} />
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        )
                    )}

                    {view === 'stats' && !isChecker && <StatsView isOwner={isOwner} canManageRoles={canManageRoles} fetchUsers={fetchUsers} setShowSettingsMenu={setShowSettingsMenu} />}
                    {view === 'settings' && isAdmin && !isChecker && (
                        <SettingsView 
                            territories={territories} 
                            onAction={handleAction} 
                            refreshTerritories={refreshTerritories} 
                            userConfig={userConfig}
                        />
                    )}

                    {!isChecker && (
                        <div className={`nav-bar ${(showNewTask) ? 'hidden' : ''}`}>
                            <div onClick={() => setView('tasks')} className={`nav-item ${view === 'tasks' ? 'active' : ''}`}>
                                <i className="fas fa-tasks"></i>
                                <span>Задачи</span>
                            </div>
                            <div onClick={() => setView('stats')} className={`nav-item ${view === 'stats' ? 'active' : ''}`}>
                                <i className="fas fa-chart-pie"></i>
                                <span>Статистика</span>
                            </div>
                            {isOwner && (
                                <div onClick={() => setView('settings')} className={`nav-item ${view === 'settings' ? 'active' : ''}`}>
                                    <i className="fas fa-cog"></i>
                                    <span>Настройки</span>
                                </div>
                            )}
                        </div>
                    )}

                    {showNewTask && <NewTaskModal territories={territories} onClose={() => setShowNewTask(false)} onAction={handleAction} />}
                </div>
            );
        };

        const root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(<App />);
    </script>
</body>
</html>
    """
    return HTMLResponse(
        content=html_content.replace("CACHE_BUST_VAL", datetime.now().isoformat()),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
