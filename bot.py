import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Tuple
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

load_dotenv()

# --- МЕГО КРУТОЕ ЛОГГИРОВАНИЕ ---
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)"
LOG_FILE = "bot_debug.log"

# Настройка корневого логгера
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("CleanBot")
# Устанавливаем уровень DEBUG для нашего логгера, чтобы видеть всё
logger.setLevel(logging.DEBUG)

logger.info("Бот запускается и инициализирует логгирование...")
# -------------------------------

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties

from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.enums.parse_mode import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.getenv("APP_URL", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else 0
AUTHORIZED_CHATS = [int(x.strip()) for x in os.getenv("AUTHORIZED_CHATS", "").split(",") if x.strip()]

ROLE_MAIN_ADMIN = "main_admin"
ROLE_MONITORING_ADMIN = "sled"
ROLE_EXECUTING_ADMIN = "isp_admin"
ROLE_EXECUTOR = "isp"

def is_authorized(chat_id: int) -> bool:
    if not AUTHORIZED_CHATS:
        return True
    return chat_id in AUTHORIZED_CHATS or chat_id == OWNER_ID

def get_user_role(conn: sqlite3.Connection, user_id: int) -> str:
    if user_id == OWNER_ID:
        return ROLE_MAIN_ADMIN
    row = conn.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row and row[0] else ROLE_EXECUTOR

def is_executing_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    return get_user_role(conn, user_id) in (ROLE_MAIN_ADMIN, ROLE_EXECUTING_ADMIN)

DEFAULT_TERRITORIES = [
    "Ресепшен",
    "Мужская",
    "Женская",
    "БВБ",
    "МВБ",
    "Улица",
]

def get_new_mode_label(mode: str) -> str:
    """Получить понятное название режима создания задач"""
    labels = {
        "group": "Только в группе",
        "admin_private": "Группа + админ в личку", 
        "any_private": "Группа + все в личку"
    }
    return labels.get(mode, mode)

def get_admin_stats() -> dict:
    """Получить статистику для админ-панели"""
    conn = db()
    
    # Общая статистика
    total_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    completed_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    
    # Статистика за сегодня
    today = date.today()
    today_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE DATE(created_at)=?", 
        (today.isoformat(),)
    ).fetchone()[0]
    today_completed = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done' AND DATE(fixed_at)=?", 
        (today.isoformat(),)
    ).fetchone()[0]
    
    # Статистика по территориям
    territory_stats = conn.execute("""
        SELECT t.name, COUNT(tsk.id) as task_count 
        FROM territories t 
        LEFT JOIN tasks tsk ON t.name = tsk.territory 
        GROUP BY t.name 
        ORDER BY task_count DESC
    """).fetchall()
    
    # Среднее время выполнения
    avg_react = conn.execute(
        "SELECT AVG((strftime('%s',accepted_at)-strftime('%s',created_at))) FROM tasks WHERE accepted_at IS NOT NULL"
    ).fetchone()[0]
    avg_fix = conn.execute(
        "SELECT AVG((strftime('%s',fixed_at)-strftime('%s',accepted_at))) FROM tasks WHERE accepted_at IS NOT NULL AND fixed_at IS NOT NULL"
    ).fetchone()[0]
    
    # Активные смены
    active_shifts = conn.execute("SELECT COUNT(*) FROM shifts WHERE is_open=1").fetchone()[0]
    
    conn.close()
    
    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "today_tasks": today_tasks,
        "today_completed": today_completed,
        "territory_stats": territory_stats,
        "avg_react": round(avg_react, 1) if avg_react else 0,
        "avg_fix": round(avg_fix, 1) if avg_fix else 0,
        "active_shifts": active_shifts
    }

class TaskInput(StatesGroup):
    waiting_payload = State()

class ProofInput(StatesGroup):
    waiting_proof = State()

class AdminInput(StatesGroup):
    waiting_territory_name = State()
    waiting_setting_value = State()

def db() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except Exception as e:
        logger.error(f"Критическая ошибка подключения к БД: {e}", exc_info=True)
        raise

def migrate_db():
    logger.info("Проверка необходимости миграции БД...")
    conn = db()
    try:
        conn.execute("INSERT INTO tasks(seq_no, shift_id, creator_user_id, chat_id, created_at, status) VALUES(0, NULL, 0, 0, 'temp', 'temp')")
        conn.rollback()
        logger.debug("Миграция не требуется.")
        return
    except sqlite3.IntegrityError:
        logger.info("Обнаружена необходимость миграции (shift_id NOT NULL).")
    except Exception as e:
        logger.error(f"Ошибка при проверке миграции: {e}")
        return

    logger.warning("Запуск процесса миграции tasks (nullable shift_id)...")
    try:
        conn.execute("ALTER TABLE tasks RENAME TO tasks_old")
        conn.execute(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq_no INTEGER NOT NULL,
                shift_id INTEGER,
                creator_user_id INTEGER NOT NULL,
                creator_username TEXT,
                chat_id INTEGER NOT NULL,
                card_message_id INTEGER,
                created_at TEXT NOT NULL,
                accepted_at TEXT,
                fixed_at TEXT,
                territory TEXT,
                status TEXT NOT NULL,
                before_message_id INTEGER,
                after_message_id INTEGER,
                FOREIGN KEY(shift_id) REFERENCES shifts(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute("INSERT INTO tasks SELECT * FROM tasks_old")
        conn.execute("DROP TABLE tasks_old")
        conn.commit()
        logger.info("Миграция успешно завершена.")
    except Exception as e:
        logger.error(f"ОШИБКА МИГРАЦИИ: {e}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()

def init_db():
    logger.info("Инициализация таблиц базы данных...")
    conn = db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_date TEXT NOT NULL,
                leader_user_id INTEGER NOT NULL,
                leader_username TEXT,
                chat_id INTEGER NOT NULL,
                is_open INTEGER NOT NULL DEFAULT 1,
                rating INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq_no INTEGER NOT NULL,
                shift_id INTEGER,
                creator_user_id INTEGER NOT NULL,
                creator_username TEXT,
                chat_id INTEGER NOT NULL,
                card_message_id INTEGER,
                created_at TEXT NOT NULL,
                accepted_at TEXT,
                fixed_at TEXT,
                territory TEXT,
                status TEXT NOT NULL,
                before_message_id INTEGER,
                after_message_id INTEGER,
                FOREIGN KEY(shift_id) REFERENCES shifts(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                due_at TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS territories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # Add full_name columns if they don't exist
        try:
            conn.execute("ALTER TABLE shifts ADD COLUMN leader_full_name TEXT")
            logger.debug("Добавлена колонка leader_full_name в shifts")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN creator_full_name TEXT")
            logger.debug("Добавлена колонка creator_full_name в tasks")
        except sqlite3.OperationalError:
            pass
        
        # Init defaults
        cur = conn.execute("SELECT COUNT(*) FROM territories")
        if cur.fetchone()[0] == 0:
            logger.info("Наполнение таблицы территорий значениями по умолчанию...")
            for t in DEFAULT_TERRITORIES:
                conn.execute("INSERT OR IGNORE INTO territories(name) VALUES(?)", (t,))
                
        # Default settings
        logger.debug("Проверка настроек по умолчанию...")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("sla_accept", "10"))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("sla_fix", "30"))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", ("new_mode", "admin_private"))
        
        conn.commit()
        logger.info("База данных успешно инициализирована.")
    except Exception as e:
        logger.error(f"Ошибка при инициализации БД: {e}", exc_info=True)
        raise
    finally:
        conn.close()
    
    # Run migration
    migrate_db()

def get_territories(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT name FROM territories ORDER BY name")
    return [row[0] for row in cur.fetchall()]

def add_territory_db(name: str):
    conn = db()
    try:
        conn.execute("INSERT INTO territories(name) VALUES(?)", (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def delete_territory_db(name: str):
    conn = db()
    conn.execute("DELETE FROM territories WHERE name=?", (name,))
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = db()
    cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    conn.commit()
    conn.close()


def now() -> datetime:
    return datetime.now(timezone.utc)

def today_str() -> str:
    # Use UTC date to match created_at which is UTC
    return now().date().isoformat()

def get_open_shift(conn: sqlite3.Connection, chat_id: int) -> Optional[Tuple]:
    cur = conn.execute(
        "SELECT id, shift_date, leader_user_id, leader_username, chat_id, is_open, leader_full_name FROM shifts WHERE chat_id=? AND is_open=1 ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    return cur.fetchone()

def start_shift(conn: sqlite3.Connection, chat_id: int, user_id: int, username: Optional[str], full_name: Optional[str]) -> int:
    open_shift = get_open_shift(conn, chat_id)
    if open_shift:
        # If full_name is missing in existing shift, update it
        if full_name and (not open_shift[6] or not open_shift[6].strip()):
            conn.execute("UPDATE shifts SET leader_full_name=? WHERE id=?", (full_name, open_shift[0]))
            conn.commit()
        return open_shift[0]
    
    # Start new shift
    conn.execute(
        "INSERT INTO shifts(shift_date, leader_user_id, leader_username, leader_full_name, chat_id, is_open) VALUES(?,?,?,?,?,1)",
        (today_str(), user_id, username or "", full_name or "", chat_id),
    )
    shift_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    # Claim pending tasks (shift_id IS NULL)
    # Also update their created_at to NOW so SLA starts from now
    current_time = now().isoformat()
    conn.execute(
        "UPDATE tasks SET shift_id=?, created_at=? WHERE shift_id IS NULL AND chat_id=?",
        (shift_id, current_time, chat_id)
    )
    
    # Schedule reminders for these tasks
    # We can't easily schedule asyncio tasks from here since this is synchronous DB func
    # We will return the list of claimed task IDs
    
    conn.commit()
    return shift_id, True # ID, created_new

def update_shift_full_name(conn: sqlite3.Connection, shift_id: int, full_name: str):
    if full_name:
        conn.execute("UPDATE shifts SET leader_full_name=? WHERE id=? AND (leader_full_name IS NULL OR leader_full_name = '')", (full_name, shift_id))
        conn.commit()

def claim_pending_tasks_reminders(bot: Bot, shift_id: int):
    conn = db()
    cur = conn.execute("SELECT id FROM tasks WHERE shift_id=?", (shift_id,))
    tasks = cur.fetchall()
    conn.close()
    for t in tasks:
        asyncio.create_task(schedule_reminders(bot, t[0]))

def next_seq_for_today(conn: sqlite3.Connection, chat_id: int) -> int:
    cur = conn.execute(
        "SELECT COALESCE(MAX(seq_no),0) FROM tasks WHERE chat_id=? AND date(created_at)=date(?)",
        (chat_id, today_str()),
    )
    return cur.fetchone()[0] + 1

def create_task(conn: sqlite3.Connection, chat_id: int, shift_id: Optional[int], creator_id: int, creator_username: Optional[str], creator_full_name: Optional[str], before_message_id: Optional[int]) -> int:
    try:
        seq = next_seq_for_today(conn, chat_id)
        conn.execute(
            "INSERT INTO tasks(seq_no, shift_id, creator_user_id, creator_username, creator_full_name, chat_id, created_at, status, before_message_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (seq, shift_id, creator_id, creator_username or "", creator_full_name or "", chat_id, now().isoformat(), "open", before_message_id or None),
        )
        conn.commit()
        task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        logger.info(f"Создана новая задача #{seq} (ID: {task_id}) пользователем {creator_full_name or creator_username} в чате {chat_id}")
        return task_id
    except Exception as e:
        logger.error(f"Ошибка при создании задачи: {e}", exc_info=True)
        conn.rollback()
        raise

def set_task_card(conn: sqlite3.Connection, task_id: int, message_id: int):
    conn.execute("UPDATE tasks SET card_message_id=? WHERE id=?", (message_id, task_id))
    conn.commit()

def accept_task(conn: sqlite3.Connection, task_id: int):
    conn.execute("UPDATE tasks SET accepted_at=?, status='accepted' WHERE id=?", (now().isoformat(), task_id))
    conn.commit()

def set_task_territory(conn: sqlite3.Connection, task_id: int, territory: str):
    conn.execute("UPDATE tasks SET territory=? WHERE id=?", (territory, task_id))
    conn.commit()

def request_proof(conn: sqlite3.Connection, task_id: int):
    conn.execute("UPDATE tasks SET status='fixing' WHERE id=?", (task_id,))
    conn.commit()

def fix_task(conn: sqlite3.Connection, task_id: int, after_message_id: Optional[int]):
    conn.execute("UPDATE tasks SET fixed_at=?, status='closed', after_message_id=? WHERE id=?", (now().isoformat(), after_message_id or None, task_id))
    conn.commit()

def get_task(conn: sqlite3.Connection, task_id: int) -> Optional[Tuple]:
    cur = conn.execute("SELECT id, seq_no, shift_id, creator_user_id, creator_username, chat_id, card_message_id, created_at, accepted_at, fixed_at, territory, status, before_message_id, after_message_id, creator_full_name FROM tasks WHERE id=?", (task_id,))
    return cur.fetchone()

def task_times(task: Tuple) -> Tuple[Optional[int], Optional[int]]:
    created = datetime.fromisoformat(task[7])
    accepted = datetime.fromisoformat(task[8]) if task[8] else None
    fixed = datetime.fromisoformat(task[9]) if task[9] else None
    reaction = int((accepted - created).total_seconds()) if accepted else None
    total = int((fixed - created).total_seconds()) if fixed else None
    return reaction, total

def format_duration(sec: int) -> str:
    m = sec // 60
    return f"{m} мин"

def task_link(chat_id: int, message_id: int) -> str:
    cid = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{cid}/{message_id}"

def display_name(value: Optional[str], full_name: Optional[str] = None) -> str:
    if full_name and full_name.strip():
        return full_name
    if not value:
        return "неизвестен"
    v = str(value)
    if v.startswith("@"):
        return v
    if " " in v:
        return v
    if v.isdigit():
        return f"id{v}"
    return f"@{v}"

def card_text(task: Tuple, leader_username: Optional[str], leader_full_name: Optional[str] = None) -> str:
    reaction, total = task_times(task)
    # task: id(0), seq(1), shift_id(2), creator_id(3), creator_name(4), chat_id(5), card_mid(6), 
    # created_at(7), accepted_at(8), fixed_at(9), territory(10), status(11), before_mid(12), after_mid(13), creator_full_name(14)
    
    seq = task[1]
    creator = display_name(task[4] if task[4] else str(task[3]), task[14])
    leader = display_name(leader_username, leader_full_name)
    territory = task[10] if task[10] else "Не назначена"
    
    status_map = {
        "open": "Открыто",
        "accepted": "В работе",
        "fixing": "На проверке",
        "closed": "Исправлено"
    }
    status_text = status_map.get(task[11], task[11])
    
    lines = []
    lines.append(f"📄 Задача №{seq}")
    lines.append(f"👤 Автор: {creator}")
    lines.append(f"👨‍✈️ Руководитель: {leader}")
    lines.append(f"📍 Территория: {territory}")
    lines.append(f"📌 Статус: {status_text}")
    
    chat_id = task[5]
    before_mid = task[12]
    after_mid = task[13]
    
    link_before = task_link(chat_id, before_mid) if before_mid else None
    link_after = task_link(chat_id, after_mid) if after_mid else None
    
    if link_before:
        lines.append(f"🔗 До: <a href=\"{link_before}\">открыть</a>")
    if link_after:
        lines.append(f"🔗 После: <a href=\"{link_after}\">открыть</a>")
        
    react_min = (reaction // 60) if reaction is not None else 0
    
    fix_duration_from_accept = 0
    if task[8] and task[9]:
        acc = datetime.fromisoformat(task[8])
        fix = datetime.fromisoformat(task[9])
        fix_duration_from_accept = int((fix - acc).total_seconds()) // 60
        
    lines.append(f"⏱ Принято: {react_min} мин")
    if task[11] == "closed":
         lines.append(f"✅ Исправлено: {fix_duration_from_accept} мин (от «Принято»)")
         
    return "\n".join(lines)

async def safe_edit_message(message: Message, text: str, reply_markup: InlineKeyboardBuilder):
    try:
        await message.edit_text(text, reply_markup=reply_markup.as_markup())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

def build_keyboard_for_status(task: Tuple) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    status = task[11]
    if status == "open":
        kb.button(text="Принято", callback_data=f"accept:{task[0]}")
    elif status == "accepted":
        if task[10]:
            kb.button(text="Исправлено", callback_data=f"fixed:{task[0]}")
        else:
            conn = db()
            ts = get_territories(conn)
            conn.close()
            for t in ts:
                kb.button(text=t, callback_data=f"territory:{task[0]}:{t}")
            kb.button(text="🔙 Назад", callback_data=f"territory_back:{task[0]}")
            kb.adjust(2, 2, 2, 1)
    elif status == "fixing":
        kb.button(text="Исправлено", callback_data=f"fixed:{task[0]}")
    elif status == "closed":
        kb.button(text="Закрыто", callback_data=f"noop")
    return kb

def build_choose_territory_keyboard(task_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Выбрать территорию", callback_data=f"territory_list:{task_id}")
    kb.adjust(1)
    return kb

def build_territory_list_keyboard(task_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    conn = db()
    ts = get_territories(conn)
    conn.close()
    for t in ts:
        kb.button(text=t, callback_data=f"territory:{task_id}:{t}")
    kb.button(text="🔙 Назад", callback_data=f"territory_back:{task_id}")
    kb.adjust(2, 2, 2, 1)
    return kb

async def auto_delete_message(chat_id: int, message_id: int, delay: int = 10):
    """Автоматически удаляет сообщение через заданное количество секунд"""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except:
        pass

async def schedule_reminders(bot: Bot, task_id: int):
    await asyncio.sleep(0)
    conn = db()
    t = get_task(conn, task_id)
    conn.close()
    if not t:
        return
    chat_id = t[5]
    
    sla_accept = int(get_setting("sla_accept", "10"))
    sla_fix = int(get_setting("sla_fix", "30"))
    
    def need_accept(tt: Tuple) -> bool:
        return tt[11] == "open" and tt[8] is None
    def need_fix(tt: Tuple) -> bool:
        return tt[11] in ("open", "accepted", "fixing") and tt[9] is None
        
    # Wait for acceptance
    await asyncio.sleep(sla_accept * 60)
    conn = db()
    t = get_task(conn, task_id)
    conn.close()
    if t and need_accept(t):
        link = f"<a href=\"{task_link(chat_id, t[6])}\">карточка</a>" if t[6] else ""
        link_part = f" ({link})" if link else ""
        await bot.send_message(chat_id, f"Задача #{t[1]}{link_part} не принята более {sla_accept} минут")
        leader = db().execute("SELECT leader_user_id FROM shifts WHERE id=?", (t[2],)).fetchone()
        if leader:
            try:
                await bot.send_message(leader[0], f"Задача #{t[1]}{link_part} не принята более {sla_accept} минут")
            except Exception:
                pass
                
    # Wait for fix (remaining time)
    remaining = (sla_fix - sla_accept) * 60
    if remaining > 0:
        await asyncio.sleep(remaining)
        conn = db()
        t = get_task(conn, task_id)
        conn.close()
        if t and need_fix(t):
            link = f"<a href=\"{task_link(chat_id, t[6])}\">карточка</a>" if t[6] else ""
            link_part = f" ({link})" if link else ""
            await bot.send_message(chat_id, f"Задача #{t[1]}{link_part} не выполнена в течение {sla_fix} минут")
            leader = db().execute("SELECT leader_user_id FROM shifts WHERE id=?", (t[2],)).fetchone()
            if leader:
                try:
                    await bot.send_message(leader[0], f"Задача #{t[1]}{link_part} не выполнена в течение {sla_fix} минут")
                except Exception:
                    pass

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
@dp.message(Command(commands=["shift"]))
async def shift_entry(message: Message):
    if not is_authorized(message.chat.id):
        return

    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команда доступна только в группе")
        return
    
    conn = db()
    leader_value = message.from_user.username or f"id{message.from_user.id}"
    full_name = message.from_user.full_name
    res = start_shift(conn, message.chat.id, message.from_user.id, leader_value, full_name)
    conn.close()
    
    if isinstance(res, tuple):
        sid, is_new = res
        if is_new:
            claim_pending_tasks_reminders(bot, sid)
            # Save main group id
            set_setting("main_group_id", str(message.chat.id))
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая задача", callback_data="new_task_group")
    kb.adjust(1)
    sent = await message.answer(f"Руководитель дневной смены: {display_name(leader_value, full_name)}", reply_markup=kb.as_markup())
    try:
        await bot.pin_chat_message(message.chat.id, sent.message_id, disable_notification=True)
    except Exception:
        pass

@dp.message(Command(commands=["new"]))
async def new_task(message: Message, state: FSMContext):
    if not is_authorized(message.chat.id):
        return

    mode = get_setting("new_mode", "admin_private")
    target_chat_id = message.chat.id
    
    if message.chat.type == "private":
        if mode == "group":
            await message.answer("Команда доступна только в группе")
            return
        if mode == "admin_private" and OWNER_ID and message.from_user.id != OWNER_ID:
            await message.answer("Команда доступна только в группе или администратору")
            return
        gid = get_setting("main_group_id")
        if not gid:
            await message.answer("Сначала начните смену в основной группе (/shift), чтобы я запомнил её.")
            return
        target_chat_id = int(gid)
    elif message.chat.type not in ("group", "supergroup"):
        await message.answer("Команда доступна только в группе")
        return
    else:
        # We are in a group, update main_group_id
        set_setting("main_group_id", str(message.chat.id))

    # Check for shift or queue availability
    conn = db()
    sh = get_open_shift(conn, target_chat_id)
    conn.close()
    
    # If no shift, we can still queue tasks if admin wants (or anyone?)
    # User said "admin can queue tasks".
    # Let's allow queuing for everyone or just admin? "разрешить администратору «накидывать» задачи"
    # But usually shift leaders also want to queue. Let's allow all for now, or check permissions.
    # Assuming anyone with /new rights (everyone in group) can queue.
    
    msg_to_delete = []
    if message.text and message.text.startswith("/new"):
         msg_to_delete.append(message.message_id)

    await state.set_state(TaskInput.waiting_payload)
    await state.update_data(target_chat_id=target_chat_id, msg_to_delete=msg_to_delete)
    
    sent = await message.answer("Пришлите фото, видео, текст или пересланное сообщение")
    # Track bot message to delete later
    msg_to_delete.append(sent.message_id)
    await state.update_data(msg_to_delete=msg_to_delete)
    
    # Автоматически удаляем техническое сообщение через 10 секунд
    asyncio.create_task(auto_delete_message(message.chat.id, sent.message_id, 10))

@dp.message(TaskInput.waiting_payload)
async def collect_payload(message: Message, state: FSMContext):
    logger.debug(f"Получен ввод для задачи от пользователя {message.from_user.id} ({message.from_user.full_name})")
    data = await state.get_data()
    target_chat_id = data.get("target_chat_id", message.chat.id)
    msg_to_delete = data.get("msg_to_delete", [])
    
    try:
        conn = db()
        sh = get_open_shift(conn, target_chat_id)
        
        shift_id = sh[0] if sh else None
        leader_username = sh[3] if sh else None
        leader_full_name = sh[6] if sh else None
        
        post_channel = get_setting("post_channel_id")
        if post_channel:
            try:
                target_chat_id = int(post_channel)
                logger.debug(f"Целевой чат изменен на канал: {target_chat_id}")
            except Exception as e:
                logger.warning(f"Ошибка парсинга post_channel_id: {e}")
                pass
    
        creator_value = message.from_user.username or f"id{message.from_user.id}"
        creator_full_name = message.from_user.full_name
        
        tid = create_task(conn, target_chat_id, shift_id, message.from_user.id, creator_value, creator_full_name, message.message_id)
        t = get_task(conn, tid)
        conn.close()
        
        logger.info(f"Задача #{t[1]} (ID: {tid}) успешно инициализирована в БД")
        
        kb = build_keyboard_for_status(t)
        text = card_text(t, leader_username, leader_full_name)
    
        sent = None
        if target_chat_id != message.chat.id and message.chat.type != "private":
            try:
                sent = await bot.send_message(target_chat_id, text, reply_markup=kb.as_markup())
                tech_msg = await message.answer(f"✅ Задача #{t[1]} создана")
                asyncio.create_task(auto_delete_message(message.chat.id, tech_msg.message_id, 5))
                logger.info(f"Карточка задачи #{t[1]} отправлена в чат/канал {target_chat_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки карточки в канал {target_chat_id}: {e}", exc_info=True)
                tech_msg = await message.answer(f"⚠️ Ошибка отправки в канал: {e}. Задача создана здесь.")
                sent = await message.answer(text, reply_markup=kb.as_markup())
                conn = db()
                conn.execute("UPDATE tasks SET chat_id=? WHERE id=?", (message.chat.id, tid))
                conn.commit()
                conn.close()
                asyncio.create_task(auto_delete_message(message.chat.id, tech_msg.message_id, 5))
        elif message.chat.type == "private":
             try:
                sent = await bot.send_message(target_chat_id, text, reply_markup=kb.as_markup())
                tech_msg = await message.answer(f"✅ Задача #{t[1]} создана в группе.")
                asyncio.create_task(auto_delete_message(message.chat.id, tech_msg.message_id, 5))
                logger.info(f"Карточка задачи #{t[1]} отправлена из ЛС в группу {target_chat_id}")
             except Exception as e:
                logger.error(f"Ошибка отправки карточки из ЛС в группу {target_chat_id}: {e}", exc_info=True)
                tech_msg = await message.answer(f"⚠️ Ошибка отправки в группу: {e}.")
                asyncio.create_task(auto_delete_message(message.chat.id, tech_msg.message_id, 5))
        else:
            sent = await message.answer(text, reply_markup=kb.as_markup())
            logger.info(f"Карточка задачи #{t[1]} отправлена в текущий чат {message.chat.id}")
     
        if sent:
            conn = db()
            set_task_card(conn, tid, sent.message_id)
            conn.close()
            logger.debug(f"ID сообщения карточки ({sent.message_id}) сохранен для задачи {tid}")
        
        await state.clear()
        
        for mid in msg_to_delete:
            try:
                await bot.delete_message(message.chat.id, mid)
            except Exception as e:
                logger.debug(f"Не удалось удалить сообщение {mid}: {e}")
        
        if shift_id:
            asyncio.create_task(schedule_reminders(bot, tid))
            logger.debug(f"Запланированы напоминания для задачи {tid}")

    except Exception as e:
        logger.error(f"Критическая ошибка в collect_payload: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при создании задачи. Пожалуйста, попробуйте еще раз.")
        await state.clear()

@dp.callback_query(F.data.startswith("accept:"))
async def accept_cb(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    task_id = int(parts[1])
    logger.debug(f"Пользователь {cq.from_user.id} ({cq.from_user.full_name}) пытается принять задачу {task_id}")
    
    try:
        conn = db()
        t = get_task(conn, task_id)
        if not t:
            conn.close()
            logger.warning(f"Задача {task_id} не найдена при попытке принятия")
            await cq.answer("Задача не найдена")
            return
        
        if not t[2]:
            conn.close()
            logger.warning(f"Попытка принять задачу {task_id} без активной смены")
            await cq.answer("Смена не начата")
            return
        
        sh = conn.execute("SELECT leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (t[2],)).fetchone()
        if not sh or sh[0] != cq.from_user.id:
            conn.close()
            logger.warning(f"Пользователь {cq.from_user.id} не является руководителем смены {t[2]}")
            await cq.answer("Только руководитель смены может принять задачу")
            return
        
        # Если в смене не было ФИО, обновим его при принятии задачи
        if sh and not sh[2]:
            logger.debug(f"Обновление ФИО руководителя в смене {t[2]} на {cq.from_user.full_name}")
            conn.execute("UPDATE shifts SET leader_full_name = ? WHERE id = ?", (cq.from_user.full_name, t[2]))
            conn.commit()
            # Обновим локальную переменную sh для корректного отображения в карточке
            sh = (sh[0], sh[1], cq.from_user.full_name)
        
        accept_task(conn, task_id)
        t = get_task(conn, task_id)
        kb = build_keyboard_for_status(t)
        text = card_text(t, sh[1] if sh else None, sh[2] if sh else None)
        await safe_edit_message(cq.message, text, kb)
        conn.close()
        logger.info(f"Задача #{t[1]} (ID: {task_id}) принята руководителем {cq.from_user.full_name}")
        await cq.answer("Задача принята")
        
        # Отправляем автоудаляемое подтверждение
        sent = await cq.message.answer("✅ Принято!")
        asyncio.create_task(auto_delete_message(cq.message.chat.id, sent.message_id, 3))
    except Exception as e:
        logger.error(f"Ошибка в accept_cb: {e}", exc_info=True)
        await cq.answer("Произошла ошибка")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка просмотра статистики без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос статистики от {cq.from_user.id}")
        stats = get_admin_stats()
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Обновить", callback_data="admin_stats")
        kb.button(text="🔙 Назад в меню", callback_data="admin_menu")
        kb.adjust(1)
        
        # Формируем текст статистики
        text = (
            "📊 <b>Статистика работы бота</b>\n\n"
            
            "📈 <b>Общая статистика:</b>\n"
            f"• Всего задач: <b>{stats['total_tasks']}</b>\n"
            f"• Выполнено: <b>{stats['completed_tasks']}</b>\n"
            f"• В процессе: <b>{stats['total_tasks'] - stats['completed_tasks']}</b>\n"
            
            f"\n📅 <b>Сегодня ({date.today().strftime('%d.%m.%Y')}):</b>\n"
            f"• Создано задач: <b>{stats['today_tasks']}</b>\n"
            f"• Выполнено: <b>{stats['today_completed']}</b>\n"
            
            f"\n⏱️ <b>Эффективность:</b>\n"
            f"• Ср. время реакции: <b>{stats['avg_react']} мин</b>\n"
            f"• Ср. время устранения: <b>{stats['avg_fix']} мин</b>\n"
            f"• Активные смены: <b>{stats['active_shifts']}</b>\n"
        )
        
        # Добавляем статистику по территориям, если есть данные
        if stats['territory_stats']:
            text += "\n📍 <b>По территориям:</b>\n"
            for territory, count in stats['territory_stats']:
                if count > 0:
                    text += f"• {territory}: <b>{count}</b> задач\n"
                else:
                    text += f"• {territory}: пока нет задач\n"
        
        text += "\n<i>Данные обновляются в реальном времени</i>"
        
        await safe_edit_message(cq.message, text, kb)
        await cq.answer("Статистика обновлена")
    except Exception as e:
        logger.error(f"Ошибка в admin_stats_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при получении статистики", show_alert=True)

@dp.callback_query(F.data.startswith("territory:"))
async def territory_cb(cq: CallbackQuery, state: FSMContext):
    try:
        _, tid_str, terr = cq.data.split(":")
        task_id = int(tid_str)
        logger.debug(f"Пользователь {cq.from_user.id} ({cq.from_user.full_name}) выбирает территорию '{terr}' для задачи {task_id}")
        
        conn = db()
        t_check = get_task(conn, task_id)
        if not t_check:
            conn.close()
            logger.warning(f"Задача {task_id} не найдена при выборе территории")
            await cq.answer("Задача не найдена")
            return
        
        sh = conn.execute("SELECT leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (t_check[2],)).fetchone()
        if not is_executing_admin(conn, cq.from_user.id):
            conn.close()
            logger.warning(f"Пользователь {cq.from_user.id} не имеет прав на выбор территории для задачи {task_id}")
            await cq.answer("Только руководитель смены может выбрать территорию")
            return
        
        if t_check[8] is None:
            accept_task(conn, task_id)
            logger.debug(f"Задача {task_id} автоматически принята при выборе территории")
            
        set_task_territory(conn, task_id, terr)
        t = get_task(conn, task_id)
        kb = build_keyboard_for_status(t)
        text = card_text(t, sh[1] if sh else None, sh[2] if sh else None)
        await safe_edit_message(cq.message, text, kb)
        
        conn.close()
        logger.info(f"Для задачи #{t[1]} (ID: {task_id}) выбрана территория: {terr}")
        await cq.answer("Территория выбрана")
        
        # Отправляем автоудаляемое подтверждение
        sent = await cq.message.answer(f"📍 Территория '{terr}' принята!")
        asyncio.create_task(auto_delete_message(cq.message.chat.id, sent.message_id, 3))
    except Exception as e:
        logger.error(f"Ошибка в territory_cb: {e}", exc_info=True)
        await cq.answer("Произошла ошибка")

@dp.callback_query(F.data.startswith("territory_back:"))
async def territory_back_cb(cq: CallbackQuery):
    _, tid_str = cq.data.split(":")
    task_id = int(tid_str)
    conn = db()
    t = get_task(conn, task_id)
    if not t:
        conn.close()
        await cq.answer("Задача не найдена")
        return
    sh = conn.execute("SELECT leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (t[2],)).fetchone()
    if not is_executing_admin(conn, cq.from_user.id):
        conn.close()
        await cq.answer("Только руководитель смены может выбрать территорию")
        return
    text = card_text(t, sh[1] if sh else None, sh[2] if sh else None)
    kb = build_choose_territory_keyboard(task_id)
    await safe_edit_message(cq.message, text, kb)
    conn.close()
    await cq.answer()

@dp.callback_query(F.data.startswith("territory_list:"))
async def territory_list_cb(cq: CallbackQuery):
    _, tid_str = cq.data.split(":")
    task_id = int(tid_str)
    conn = db()
    t = get_task(conn, task_id)
    if not t:
        conn.close()
        await cq.answer("Задача не найдена")
        return
    sh = conn.execute("SELECT leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (t[2],)).fetchone()
    if not is_executing_admin(conn, cq.from_user.id):
        conn.close()
        await cq.answer("Только руководитель смены может выбрать территорию")
        return
    text = card_text(t, sh[1] if sh else None, sh[2] if sh else None)
    kb = build_territory_list_keyboard(task_id)
    await safe_edit_message(cq.message, text, kb)
    conn.close()
    await cq.answer()

@dp.callback_query(F.data.startswith("fixed:"))
async def fixed_cb(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    task_id = int(parts[1])
    logger.debug(f"Пользователь {cq.from_user.id} ({cq.from_user.full_name}) нажал 'Исправлено' для задачи {task_id}")
    
    try:
        conn = db()
        t = get_task(conn, task_id)
        if not t:
            conn.close()
            logger.warning(f"Задача {task_id} не найдена при попытке закрытия")
            await cq.answer("Задача не найдена")
            return
        
        sh = conn.execute("SELECT leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (t[2],)).fetchone()
        if not sh or sh[0] != cq.from_user.id:
            conn.close()
            logger.warning(f"Пользователь {cq.from_user.id} не имеет прав на закрытие задачи {task_id}")
            await cq.answer("Только руководитель смены может закрыть задачу")
            return
        
        if not t[10]:
            conn.close()
            logger.warning(f"Попытка закрыть задачу {task_id} без выбранной территории")
            await cq.answer("Сначала выберите территорию")
            return
        
        request_proof(conn, task_id)
        t = get_task(conn, task_id)
        kb = build_keyboard_for_status(t)
        text = card_text(t, sh[1] if sh else None, sh[2] if sh else None)
        await safe_edit_message(cq.message, text, kb)
        conn.close()
        
        await state.set_state(ProofInput.waiting_proof)
        prompt = await cq.message.reply("Отправьте подтверждение: фото/видео/текст/ГС или пересланное сообщение")
        await state.update_data(task_id=task_id, prompt_message_id=prompt.message_id)
        asyncio.create_task(auto_delete_message(cq.message.chat.id, prompt.message_id, 30))
        
        logger.info(f"Задача #{t[1]} (ID: {task_id}) переведена в статус 'fixing', ожидается подтверждение")
        await cq.answer("Ожидаю подтверждение")
        
        # Отправляем автоудаляемое подтверждение
        sent_ok = await cq.message.answer("✅ Принято! Жду подтверждение (фото/видео).")
        asyncio.create_task(auto_delete_message(cq.message.chat.id, sent_ok.message_id, 3))
    except Exception as e:
        logger.error(f"Ошибка в fixed_cb: {e}", exc_info=True)
        await cq.answer("Произошла ошибка")

# Legacy start removed

@dp.message(Command(commands=["analytics"]))
async def analytics_cmd(message: Message):
    logger.debug(f"Запрос аналитики от пользователя {message.from_user.id} (Full Name: {message.from_user.full_name})")
    if OWNER_ID and message.from_user.id != OWNER_ID:
        logger.warning(f"Попытка доступа к аналитике без прав: {message.from_user.id}")
        await message.answer("Недостаточно прав")
        return
        
    try:
        period = "day"
        extended = True
        obj = CommandObject(message.text)
        if obj.args:
            tokens = obj.args.strip().lower().split()
            for tok in tokens:
                if tok in ("full", "extended", "расширенная", "полная"):
                    extended = True
                elif tok in ("day", "день"):
                    period = "day"
                elif tok in ("week", "неделя"):
                    period = "week"
                elif tok in ("month", "месяц"):
                    period = "month"
                elif tok in ("year", "год"):
                    period = "year"
        
        logger.info(f"Формирование отчета: период={period}, расширенный={extended}")
        
        conn = db()
        if period == "day":
            where_sql = "date(created_at)=date(?)"
            params = (today_str(),)
            period_label = f"День ({today_str()})"
        elif period == "week":
            where_sql = "created_at >= datetime('now','-7 day')"
            params = ()
            period_label = "Неделя (последние 7 дней)"
        elif period == "month":
            where_sql = "created_at >= datetime('now','-30 day')"
            params = ()
            period_label = "Месяц (последние 30 дней)"
        else:
            where_sql = "created_at >= datetime('now','-365 day')"
            params = ()
            period_label = "Год (последние 365 дней)"
            
        cur = conn.execute(
            "SELECT COUNT(*), "
            "AVG(CASE WHEN accepted_at IS NOT NULL THEN (strftime('%s',accepted_at)-strftime('%s',created_at)) END), "
            "AVG(CASE WHEN accepted_at IS NOT NULL AND fixed_at IS NOT NULL THEN (strftime('%s',fixed_at)-strftime('%s',accepted_at)) END) "
            f"FROM tasks WHERE {where_sql}",
            params
        )
        count, avg_react, avg_total = cur.fetchone()
        
        terr = conn.execute(
            f"SELECT territory, COUNT(*) c FROM tasks WHERE territory IS NOT NULL AND {where_sql} GROUP BY territory ORDER BY c DESC LIMIT 1",
            params
        )
        terr = terr.fetchone()
        
        lines = []
        lines.append(f"Дата: {period_label}")
        lines.append(f"Всего задач: {int(count or 0)}")
        lines.append(f"Общее ср. время реакции: {format_duration(int(avg_react)) if avg_react else '-'}")
        lines.append(f"Общее ср. время устранения: {format_duration(int(avg_total)) if avg_total else '-'}")
        lines.append(f"Зона с макс. кол-вом замечаний: {terr[0]} ({terr[1]})" if terr else "Зона с макс. кол-вом замечаний: -")
        
        if extended:
            per_terr = conn.execute(
                "SELECT territory, COUNT(*), "
                "AVG(CASE WHEN accepted_at IS NOT NULL AND fixed_at IS NOT NULL THEN (strftime('%s',fixed_at)-strftime('%s',accepted_at)) END) "
                f"FROM tasks WHERE territory IS NOT NULL AND {where_sql} GROUP BY territory ORDER BY COUNT(*) DESC",
                params
            ).fetchall()
            lines.append("Детализация по зонам:")
            if per_terr:
                for idx, (territory, tcount, avg_fix) in enumerate(per_terr, start=1):
                    lines.append(f"{idx}. {territory} — {int(tcount)} — Ср. время устранения: {format_duration(int(avg_fix)) if avg_fix else '-'}")
            else:
                lines.append("1. -")
                
            if period == "day":
                shift = conn.execute(
                    "SELECT leader_username, rating, leader_full_name, leader_user_id FROM shifts WHERE shift_date=? ORDER BY id DESC LIMIT 1",
                    (today_str(),)
                ).fetchone()
            else:
                shift = conn.execute(
                    "SELECT leader_username, rating, leader_full_name, leader_user_id FROM shifts ORDER BY id DESC LIMIT 1"
                ).fetchone()
            
            # Если в смене не было ФИО, но сейчас мы его знаем (запрос от админа, но смена может быть другого пользователя)
            # В analytics_cmd message.from_user может быть не владельцем смены, поэтому обновлять ФИО тут рискованно без проверки ID.
            # Но мы можем использовать display_name для вывода.
            
            lines.append(f"Оценка смены: {shift[1]} ⭐" if shift and shift[1] is not None else "Оценка смены: -")
            lines.append(f"Ответственный смены: {display_name(shift[0], shift[2])}" if shift else "Ответственный смены: -")
            
        conn.close()
        text = "\n".join(lines)
        
        if message.chat.type != "private":
            try:
                await bot.send_message(message.from_user.id, text)
                notice = await message.answer("Отчет отправлен в ЛС")
                asyncio.create_task(auto_delete_message(message.chat.id, notice.message_id, 5))
                logger.info(f"Отчет аналитики отправлен в ЛС пользователю {message.from_user.id}")
            except Exception as e:
                logger.warning(f"Не удалось отправить аналитику в ЛС пользователю {message.from_user.id}: {e}")
                notice = await message.answer("Не могу отправить отчет в ЛС. Напишите боту в личку и повторите команду.")
                asyncio.create_task(auto_delete_message(message.chat.id, notice.message_id, 10))
        else:
            await message.answer(text)
            logger.info(f"Отчет аналитики отправлен в приватный чат пользователю {message.from_user.id}")
            
    except Exception as e:
        logger.error(f"Ошибка в analytics_cmd: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при формировании аналитики.")

# Old any_message removed


# Legacy close_shift removed

@dp.callback_query(F.data.startswith("rate:"))
async def rate_cb(cq: CallbackQuery):
    try:
        _, sid_str, score = cq.data.split(":")
        sid = int(sid_str)
        score = int(score)
        logger.debug(f"Получена оценка {score} для смены {sid} от {cq.from_user.id}")
        
        conn = db()
        
        # Обновляем оценку в базе
        conn.execute("UPDATE shifts SET rating=? WHERE id=?", (score, sid))
        conn.commit()
        
        # Получаем информацию о смене
        shift = conn.execute("SELECT shift_date, leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (sid,)).fetchone()
        
        if not shift:
            conn.close()
            logger.warning(f"Смена {sid} не найдена при попытке оценки")
            await cq.answer("Смена не найдена")
            return

        # Получаем имя оценивающего
        rater_name = cq.from_user.full_name or cq.from_user.username or f"id{cq.from_user.id}"
        
        # Получаем имя руководителя смены
        leader_name = display_name(shift[2], shift[3])
        
        # Формируем обновленное сообщение с оценкой
        lines = []
        lines.append(f"Дата: {shift[0]}")
        
        # Получаем статистику смены
        stats = conn.execute(
            "SELECT COUNT(*), "
            "AVG(CASE WHEN accepted_at IS NOT NULL THEN (strftime('%s',accepted_at)-strftime('%s',created_at)) END), "
            "AVG(CASE WHEN accepted_at IS NOT NULL AND fixed_at IS NOT NULL THEN (strftime('%s',fixed_at)-strftime('%s',accepted_at)) END) "
            "FROM tasks WHERE shift_id=?",
            (sid,)
        ).fetchone()
        terr = conn.execute("SELECT territory, COUNT(*) c FROM tasks WHERE shift_id=? AND territory IS NOT NULL GROUP BY territory ORDER BY c DESC LIMIT 1", (sid,)).fetchone()
        
        lines.append(f"Кол-во задач: {int(stats[0] or 0)}")
        lines.append(f"Ср. время реакции: {format_duration(int(stats[1])) if stats[1] else '-'}")
        lines.append(f"Ср. время устранения: {format_duration(int(stats[2])) if stats[2] else '-'}")
        lines.append(f"Больше всего замечаний: {terr[0] if terr else '-'}")
        lines.append(f"Оценка работы: {score} ⭐")
        lines.append(f"Оценил: {rater_name}")
        lines.append(f"Ответственный смены: {leader_name}")
        
        conn.close()
        
        # Обновляем сообщение с новой информацией
        await cq.message.edit_text("\n".join(lines))
        await cq.answer(f"Спасибо за оценку! Вы поставили {score} ⭐")
        logger.info(f"Смена {sid} успешно оценена пользователем {cq.from_user.id} на {score} звезд")
        
    except Exception as e:
        logger.error(f"Ошибка в rate_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при сохранении оценки", show_alert=True)

@dp.message(Command(commands=["start"]))
async def start(message: Message):
    logger.debug(f"Команда /start от {message.from_user.id}")
    
    # Автоматическая регистрация пользователя в БД
    try:
        conn = db()
        user_id = message.from_user.id
        username = message.from_user.username
        full_name = message.from_user.full_name
        
        # Проверяем, существует ли пользователь
        existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        
        if not existing:
            role = "isp"
            is_active = 1
            if user_id == OWNER_ID:
                role = "main_admin"
                
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, role, is_active, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, full_name, role, is_active, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            logger.info(f"Новый пользователь зарегистрирован: {full_name} (@{username}, ID: {user_id}) с ролью {role}, статус активен: {is_active}")
        else:
            # Обновляем данные и время последнего визита, и активируем пользователя после /start
            conn.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen = ?, is_active = 1 WHERE user_id = ?",
                (username, full_name, datetime.now(timezone.utc).isoformat(), user_id)
            )
            conn.commit()
            logger.debug(f"Данные пользователя {user_id} обновлены")
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при регистрации пользователя в /start: {e}", exc_info=True)

    if message.chat.type == "private":
        if not APP_URL:
            await message.answer("⚠️ APP_URL не настроен. Администратор должен указать ссылку на Mini App в .env")
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="🚀 Открыть CleanApp", web_app=WebAppInfo(url=APP_URL))
        builder.adjust(1)
        await message.answer(
            "Привет! Открой Mini App, чтобы управлять задачами прямо в Telegram.",
            reply_markup=builder.as_markup()
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Начать смену", callback_data="start_shift")
    kb.button(text="🛑 Закончить смену", callback_data="end_shift")
    kb.adjust(2)
    await message.answer("📌 Панель смены (закрепите это сообщение вручную):", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "start_shift")
async def start_shift_cb(cq: CallbackQuery):
    try:
        if not is_authorized(cq.message.chat.id):
            logger.warning(f"Попытка начать смену в неавторизованном чате {cq.message.chat.id} пользователем {cq.from_user.id}")
            await cq.answer("Этот чат не авторизован")
            return

        conn = db()
        if not is_executing_admin(conn, cq.from_user.id):
            conn.close()
            await cq.answer("Только руководитель смены может начать смену", show_alert=True)
            return
        leader_value = cq.from_user.username or f"id{cq.from_user.id}"
        full_name = cq.from_user.full_name
        logger.info(f"Пользователь {cq.from_user.id} ({full_name}) начинает смену в чате {cq.message.chat.id}")
        
        res = start_shift(conn, cq.message.chat.id, cq.from_user.id, leader_value, full_name)
        conn.close()
        
        if isinstance(res, tuple):
            sid, is_new = res
            if is_new:
                logger.info(f"Создана новая смена {sid} для чата {cq.message.chat.id}")
                claim_pending_tasks_reminders(bot, sid)
                set_setting("main_group_id", str(cq.message.chat.id))
        else:
            sid = res
            logger.info(f"Используется существующая открытая смена {sid} для чата {cq.message.chat.id}")
        
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Новая задача", callback_data="new_task_group")
        kb.adjust(1)
        
        # Создаем Reply-клавиатуру специально для iOS (с параметром persistent)
        reply_kb = ReplyKeyboardBuilder()
        reply_kb.button(text="➕ Новая задача")
        markup = reply_kb.as_markup(
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Нажмите кнопку для создания задачи"
        )
        
        sent_msg = await cq.message.answer(
            f"Руководитель дневной смены: {display_name(leader_value, full_name)}", 
            reply_markup=markup # Отправляем Reply-клавиатуру вместе с сообщением
        )
        
        # Также отправим сообщение с Inline-кнопкой (дублирование для надежности)
        await cq.message.answer("Кнопка создания задачи также доступна ниже:", reply_markup=kb.as_markup())
        
        try:
            await bot.pin_chat_message(cq.message.chat.id, sent_msg.message_id)
            logger.info(f"Сообщение о начале смены {sid} закреплено в чате {cq.message.chat.id}")
        except Exception as pin_err:
            logger.warning(f"Не удалось закрепить сообщение о начале смены: {pin_err}")
            
        await cq.answer("Смена начата")
    except Exception as e:
        logger.error(f"Ошибка в start_shift_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при начале смены", show_alert=True)

@dp.callback_query(F.data == "new_task_group")
async def new_task_group_cb(cq: CallbackQuery, state: FSMContext):
    """Обработчик для inline-кнопки Новая задача в группе"""
    try:
        logger.debug(f"Начало создания новой задачи в группе от {cq.from_user.id}")
        await state.set_state(TaskInput.waiting_payload)
        msg_to_delete = []
        sent = await cq.message.answer("Пришлите фото, видео, текст или пересланное сообщение")
        msg_to_delete.append(sent.message_id)
        await state.update_data(target_chat_id=cq.message.chat.id, msg_to_delete=msg_to_delete)
        asyncio.create_task(auto_delete_message(cq.message.chat.id, sent.message_id, 10))
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в new_task_group_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data == "new_task")
async def new_task_cb(cq: CallbackQuery, state: FSMContext):
    try:
        logger.debug(f"Начало создания новой задачи (кнопка) от {cq.from_user.id}")
        await state.set_state(TaskInput.waiting_payload)
        msg_to_delete = []
        sent = await cq.message.answer("Пришлите фото, видео, текст или пересланное сообщение")
        msg_to_delete.append(sent.message_id)
        await state.update_data(target_chat_id=cq.message.chat.id, msg_to_delete=msg_to_delete)
        asyncio.create_task(auto_delete_message(cq.message.chat.id, sent.message_id, 10))
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в new_task_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.message(F.text == "➕ Новая задача")
async def new_task_text(message: Message, state: FSMContext):
    logger.debug(f"Команда 'Новая задача' (текст) от {message.from_user.id}")
    await new_task(message, state)

async def close_shift_logic(message: Message, chat_id: int):
    try:
        if not is_authorized(chat_id):
            logger.warning(f"Попытка закрыть смену в неавторизованном чате {chat_id}")
            return

        conn = db()
        sh = get_open_shift(conn, chat_id)
        if not sh:
            conn.close()
            logger.warning(f"Попытка закрыть смену в чате {chat_id}, но открытых смен нет")
            await message.answer("Открытая смена не найдена", reply_markup=ReplyKeyboardRemove())
            return

        if not is_executing_admin(conn, message.from_user.id):
            conn.close()
            await message.answer("Только руководитель смены может завершить смену", reply_markup=ReplyKeyboardRemove())
            return
            
        logger.info(f"Закрытие смены {sh[0]} в чате {chat_id} пользователем {message.from_user.id}")
        
        open_tasks = conn.execute("SELECT id, seq_no, card_message_id FROM tasks WHERE shift_id=? AND status!='closed'", (sh[0],)).fetchall()
        if open_tasks:
            logger.info(f"Смена {sh[0]} не может быть закрыта: {len(open_tasks)} открытых задач")
            lines = []
            for tid, seq, mid in open_tasks:
                link = task_link(chat_id, mid) if mid else f"#{seq}"
                lines.append(f"Открыта задача #{seq}: {link}")
            conn.close()
            await message.answer("Смена не может быть закрыта. Открытые задачи:\n" + "\n".join(lines))
            return
            
        stats = conn.execute(
            "SELECT COUNT(*), "
            "AVG(CASE WHEN accepted_at IS NOT NULL THEN (strftime('%s',accepted_at)-strftime('%s',created_at)) END), "
            "AVG(CASE WHEN accepted_at IS NOT NULL AND fixed_at IS NOT NULL THEN (strftime('%s',fixed_at)-strftime('%s',accepted_at)) END) "
            "FROM tasks WHERE shift_id=?",
            (sh[0],)
        ).fetchone()
        terr = conn.execute("SELECT territory, COUNT(*) c FROM tasks WHERE shift_id=? AND territory IS NOT NULL GROUP BY territory ORDER BY c DESC LIMIT 1", (sh[0],)).fetchone()
        
        conn.execute("UPDATE shifts SET is_open=0 WHERE id=?", (sh[0],))
        conn.commit()
        conn.close()
        
        leader_name = display_name(sh[3], sh[6])
        
        lines = []
        lines.append(f"Дата: {sh[1]}")
        lines.append(f"Кол-во задач: {int(stats[0] or 0)}")
        lines.append(f"Ср. время реакции: {format_duration(int(stats[1])) if stats[1] else '-'}")
        lines.append(f"Ср. время устранения: {format_duration(int(stats[2])) if stats[2] else '-'}")
        lines.append(f"Больше всего замечаний: {terr[0] if terr else '-'}")
        lines.append(f"Оценка работы: -")
        lines.append(f"Оценил: -")
        lines.append(f"Ответственный смены: {leader_name}")
        
        kb = InlineKeyboardBuilder()
        for i in range(1, 6):
            kb.button(text=f"⭐{i}", callback_data=f"rate:{sh[0]}:{i}")
        kb.adjust(5)
        
        # Используем Inline-кнопку вместо Reply-кнопки для совместимости с iOS
        inline_kb = InlineKeyboardBuilder()
        inline_kb.button(text="➕ Новая задача", callback_data="new_task_group")
        inline_kb.adjust(1)
        
        await message.answer("\n".join(lines), reply_markup=inline_kb.as_markup())
        await message.answer(f"Оцените качество работы смены", reply_markup=kb.as_markup())
        logger.info(f"Смена {sh[0]} успешно закрыта")
        
    except Exception as e:
        logger.error(f"Ошибка в close_shift_logic: {e}", exc_info=True)
        await message.answer("❌ Ошибка при закрытии смены")

@dp.callback_query(F.data == "end_shift")
async def end_shift_cb(cq: CallbackQuery):
    try:
        logger.debug(f"Нажата кнопка завершения смены пользователем {cq.from_user.id} в чате {cq.message.chat.id}")
        await close_shift_logic(cq.message, cq.message.chat.id)
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в end_shift_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при завершении смены", show_alert=True)

@dp.message(Command(commands=["close"]))
async def close_shift(message: Message):
    try:
        logger.debug(f"Команда /close от пользователя {message.from_user.id} в чате {message.chat.id}")
        if message.chat.type not in ("group", "supergroup"):
            logger.warning(f"Команда /close вызвана не в группе: {message.chat.type}")
            await message.answer("Команда доступна только в группе")
            return
        await close_shift_logic(message, message.chat.id)
    except Exception as e:
        logger.error(f"Ошибка в close_shift (команда): {e}", exc_info=True)
        await message.answer("❌ Ошибка при выполнении команды")

@dp.message(Command(commands=["cleanup"]))
async def cleanup_cmd(message: Message):
    try:
        logger.debug(f"Команда /cleanup от пользователя {message.from_user.id}")
        if message.from_user.id != OWNER_ID:
            logger.warning(f"Попытка доступа к /cleanup без прав: {message.from_user.id}")
            return
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🧹 Очистить задачи и смены", callback_data="cleanup_tasks")
        kb.button(text="⚠️ Удалить ВСЮ базу", callback_data="cleanup_all")
        kb.button(text="📊 Показать статистику", callback_data="cleanup_stats")
        kb.button(text="❌ Отмена", callback_data="admin_close")
        kb.adjust(1)
        
        await message.answer(
            "🧹 Очистка тестовых данных\n\n"
            "Выберите действие:\n"
            "• Очистить задачи и смены - удалит все задачи, смены и напоминания, но сохранит территории и настройки\n"
            "• Удалить ВСЮ базу - полный сброс, потребуется перезапуск бота",
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка в cleanup_cmd: {e}", exc_info=True)
        await message.answer("❌ Ошибка при вызове меню очистки")

@dp.callback_query(F.data == "cleanup_tasks")
async def cleanup_tasks_cb(cq: CallbackQuery):
    try:
        logger.info(f"Запрос на очистку задач и смен от {cq.from_user.id}")
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка очистки задач без прав: {cq.from_user.id}")
            return
        
        conn = db()
        try:
            # Удаляем все задачи, смены и напоминания
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM reminders") 
            conn.execute("DELETE FROM shifts")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('tasks', 'shifts', 'reminders')")
            conn.commit()
            logger.info("База данных успешно очищена от задач, смен и напоминаний")
            
            await cq.message.edit_text("✅ Задачи, смены и напоминания удалены. ID сброшены.")
        except Exception as e:
            logger.error(f"Ошибка при выполнении SQL очистки: {e}", exc_info=True)
            await cq.message.edit_text("❌ Ошибка при очистке базы данных.")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Ошибка в cleanup_tasks_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при очистке", show_alert=True)

@dp.callback_query(F.data == "cleanup_all")
async def cleanup_all_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка полного удаления базы без прав: {cq.from_user.id}")
            return
        
        logger.info(f"Запрос на ПОЛНОЕ УДАЛЕНИЕ базы данных от {cq.from_user.id}")
        import os
        if os.path.exists("tasks.db"):
            os.remove("tasks.db")
            logger.warning("Файл базы данных tasks.db удален")
            await cq.message.edit_text(
                "⚠️ База данных полностью удалена\n"
                "Перезапустите бота для создания новой базы"
            )
        else:
            logger.warning("Попытка удаления базы, но файл tasks.db не найден")
            await cq.message.edit_text("База данных не найдена")
    except Exception as e:
        logger.error(f"Ошибка в cleanup_all_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при удалении базы", show_alert=True)

@dp.callback_query(F.data == "cleanup_stats")
async def cleanup_stats_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка просмотра статистики БД без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос статистики БД от {cq.from_user.id}")
        conn = db()
        try:
            tables = ['tasks', 'shifts', 'territories', 'settings', 'reminders']
            stats = []
            
            for table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                stats.append(f"• {table}: {count} записей")
            
            stats_text = "\n".join(stats)
            await cq.message.edit_text(
                f"📊 Текущая статистика базы данных:\n\n{stats_text}"
            )
            logger.info("Статистика БД успешно сформирована и отправлена")
        except Exception as e:
            logger.error(f"Ошибка при получении статистики из БД: {e}", exc_info=True)
            await cq.message.edit_text(f"Ошибка при получении статистики: {e}")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Ошибка в cleanup_stats_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при получении статистики", show_alert=True)

@dp.message(Command(commands=["admin"]))
async def admin_cmd(message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            logger.warning(f"Попытка доступа к /admin без прав: {message.from_user.id}")
            return
        
        logger.info(f"Команда /admin от {message.from_user.id}")
        kb = InlineKeyboardBuilder()
        kb.button(text="Территории", callback_data="admin_terr")
        kb.button(text="Настройки", callback_data="admin_settings")
        kb.button(text="🧹 Очистка данных", callback_data="cleanup_menu")
        kb.button(text="📊 Статистика", callback_data="admin_stats")
        kb.button(text="Закрыть", callback_data="admin_close")
        kb.adjust(1)
        await message.answer("Админ-панель", reply_markup=kb.as_markup())
    except Exception as e:
        logger.error(f"Ошибка в admin_cmd: {e}", exc_info=True)
        await message.answer("❌ Ошибка при открытии админ-панели")

@dp.callback_query(F.data == "admin_menu")
async def admin_menu_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка доступа к admin_menu без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Переход в главное админ-меню от {cq.from_user.id}")
        kb = InlineKeyboardBuilder()
        kb.button(text="🏢 Управление территориями", callback_data="admin_terr")
        kb.button(text="⚙️ Настройки бота", callback_data="admin_settings")
        kb.button(text="📊 Статистика", callback_data="admin_stats")
        kb.button(text="❌ Закрыть панель", callback_data="admin_close")
        kb.adjust(1)
        
        text = (
            "📋 <b>Административная панель</b>\n\n"
            "Здесь вы можете управлять всеми аспектами работы бота:\n\n"
            "🏢 <b>Территории</b> - добавляйте и удаляйте зоны обслуживания\n"
            "⚙️ <b>Настройки</b> - настройте SLA, канал для постов и доступ\n"
            "📊 <b>Статистика</b> - просматривайте эффективность работы\n\n"
            "Выберите нужный раздел:"
        )
        
        await cq.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка в admin_menu_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при открытии меню", show_alert=True)

@dp.callback_query(F.data == "cleanup_menu")
async def cleanup_menu_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка доступа к cleanup_menu без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Переход в меню очистки от {cq.from_user.id}")
        kb = InlineKeyboardBuilder()
        kb.button(text="🧹 Очистить задачи и смены", callback_data="cleanup_tasks")
        kb.button(text="⚠️ Удалить ВСЮ базу", callback_data="cleanup_all")
        kb.button(text="📊 Показать статистику", callback_data="cleanup_stats")
        kb.button(text="🔙 Назад", callback_data="admin_menu")
        kb.adjust(1)
        
        await cq.message.edit_text(
            "🧹 Очистка тестовых данных\n\n"
            "Выберите действие:\n"
            "• 🧹 Очистить задачи и смены - удалит все задачи, смены и напоминания, но сохранит территории и настройки\n"
            "• ⚠️ Удалить ВСЮ базу - полный сброс, потребуется перезапуск бота",
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка в cleanup_menu_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при открытии меню очистки", show_alert=True)

@dp.callback_query(F.data == "admin_close")
async def admin_close_cb(cq: CallbackQuery):
    try:
        logger.debug(f"Закрытие админ-панели пользователем {cq.from_user.id}")
        await cq.message.delete()
    except Exception as e:
        logger.error(f"Ошибка при закрытии админ-панели: {e}", exc_info=True)
        await cq.answer()

@dp.callback_query(F.data == "admin_terr")
async def admin_terr_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка доступа к управлению территориями без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос управления территориями от {cq.from_user.id}")
        conn = db()
        ts = get_territories(conn)
        conn.close()
        
        kb = InlineKeyboardBuilder()
        
        if ts:
            text = (
                "🏢 <b>Управление территориями</b>\n\n"
                f"📍 Всего территорий: {len(ts)}\n\n"
                "Нажмите на территорию, чтобы <b>удалить</b> её:\n"
                "(используйте кнопку \"➕ Добавить\" для создания новой)"
            )
            
            for i in range(0, len(ts), 2):
                if i + 1 < len(ts):
                    kb.button(text=f"🗑️ {ts[i]}", callback_data=f"del_terr:{ts[i]}")
                    kb.button(text=f"🗑️ {ts[i+1]}", callback_data=f"del_terr:{ts[i+1]}")
                else:
                    kb.button(text=f"🗑️ {ts[i]}", callback_data=f"del_terr:{ts[i]}")
            kb.adjust(2)
        else:
            text = (
                "🏢 <b>Управление территориями</b>\n\n"
                "❗ У вас ещё нет ни одной территории\n\n"
                "Территории нужны для распределения задач по зонам обслуживания"
            )
        
        kb.button(text="➕ Добавить территорию", callback_data="add_terr")
        kb.button(text="🔙 Назад в меню", callback_data="admin_menu")
        
        await cq.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка в admin_terr_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при загрузке территорий", show_alert=True)

@dp.callback_query(F.data.startswith("del_terr:"))
async def del_terr_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка удаления территории без прав: {cq.from_user.id}")
            return
        
        t = cq.data.split(":", 1)[1]
        logger.info(f"Удаление территории '{t}' пользователем {cq.from_user.id}")
        delete_territory_db(t)
        await cq.answer(f"Территория '{t}' удалена")
        await admin_terr_cb(cq)
    except Exception as e:
        logger.error(f"Ошибка в del_terr_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при удалении территории", show_alert=True)

@dp.callback_query(F.data == "add_terr")
async def add_terr_cb(cq: CallbackQuery, state: FSMContext):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка добавления территории без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Начало добавления новой территории от {cq.from_user.id}")
        await state.set_state(AdminInput.waiting_territory_name)
        await cq.message.answer("Введите название новой территории:")
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в add_terr_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.message(AdminInput.waiting_territory_name)
async def save_terr(message: Message, state: FSMContext):
    try:
        logger.info(f"Сохранение новой территории '{message.text}' от {message.from_user.id}")
        add_territory_db(message.text)
        await state.clear()
        await message.answer(f"Территория '{message.text}' добавлена.")
        
        kb = InlineKeyboardBuilder()
        kb.button(text="Территории", callback_data="admin_terr")
        kb.button(text="Настройки", callback_data="admin_settings")
        kb.button(text="Закрыть", callback_data="admin_close")
        kb.adjust(1)
        await message.answer("Админ-панель", reply_markup=kb.as_markup())
    except Exception as e:
        logger.error(f"Ошибка в save_terr: {e}", exc_info=True)
        await message.answer("❌ Ошибка при сохранении территории")

@dp.callback_query(F.data == "admin_settings")
async def admin_settings_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка доступа к настройкам без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос настроек от {cq.from_user.id}")
        kb = InlineKeyboardBuilder()
        kb.button(text="⏱️ Время на принятие задачи", callback_data="set:sla_accept")
        kb.button(text="⏱️ Время на выполнение задачи", callback_data="set:sla_fix")
        kb.button(text="📢 Канал для уведомлений", callback_data="set:post_channel_id")
        kb.button(text="👥 Кто может создавать задачи", callback_data="set:new_mode")
        kb.button(text="🔙 Назад", callback_data="admin_menu")
        kb.adjust(1)
        
        sla_a = get_setting("sla_accept", "10")
        sla_f = get_setting("sla_fix", "30")
        pcid = get_setting("post_channel_id", "Не задан")
        nmode = get_setting("new_mode", "admin_private")
        nmode_label = get_new_mode_label(nmode)
        
        text = (
            "⚙️ <b>Настройки бота</b>\n\n"
            "Здесь вы настраиваете ключевые параметры работы бота:\n\n"
            f"⏱️ <b>Время на принятие:</b> {sla_a} минут\n"
            "├ Сколько времени дается сотруднику на принятие задачи\n"
            "└ После истечения времени - автоматическое напоминание\n\n"
            f"⏱️ <b>Время на выполнение:</b> {sla_f} минут\n"
            "├ Общее время на выполнение задачи\n"
            "└ Используется для контроля сроков и статистики\n\n"
            f"📢 <b>Канал уведомлений:</b> {pcid}\n"
            "├ ID канала для публикации выполненных задач\n"
            "└ Оставьте \"Не задан\" чтобы отключить публикацию\n\n"
            f"👥 <b>Создание задач:</b> {nmode_label}\n"
            "└ Кто может создавать новые задачи в боте\n\n"
            "Нажмите на параметр, чтобы изменить его"
        )
        await cq.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка в admin_settings_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при загрузке настроек", show_alert=True)


@dp.callback_query(F.data == "set:new_mode")
async def set_new_mode_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка изменения режима создания задач без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос выбора режима создания задач от {cq.from_user.id}")
        current_mode = get_setting("new_mode", "admin_private")
        
        kb = InlineKeyboardBuilder()
        
        # Добавляем кнопки для каждого режима
        modes = [
            ("group", "Только в группе"),
            ("admin_private", "Группа + админ в личку"), 
            ("any_private", "Группа + все в личку")
        ]
        
        for mode_value, mode_label in modes:
            if mode_value == current_mode:
                # Текущий режим - отмечаем галочкой
                kb.button(text=f"✅ {mode_label}", callback_data=f"new_mode:{mode_value}")
            else:
                kb.button(text=mode_label, callback_data=f"new_mode:{mode_value}")
        
        kb.button(text="🔙 Назад", callback_data="admin_settings")
        kb.adjust(1)
        
        await cq.message.edit_text(
            "Выберите режим создания задач:\n\n"
            "• <b>Только в группе</b> - команда /new доступна только в групповом чате\n"
            "• <b>Группа + админ в личку</b> - доступна в группе и администратору в личные сообщения\n"
            "• <b>Группа + все в личку</b> - доступна в группе и всем пользователям в личные сообщения",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка в set_new_mode_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при выборе режима", show_alert=True)

@dp.callback_query(F.data.startswith("new_mode:"))
async def save_new_mode_cb(cq: CallbackQuery):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка сохранения режима создания задач без прав: {cq.from_user.id}")
            return
        
        new_mode = cq.data.split(":")[1]
        logger.info(f"Смена режима создания задач на '{new_mode}' пользователем {cq.from_user.id}")
        set_setting("new_mode", new_mode)
        
        # Возвращаемся к настройкам
        await admin_settings_cb(cq)
        await cq.answer("Режим создания задач обновлен")
    except Exception as e:
        logger.error(f"Ошибка в save_new_mode_cb: {e}", exc_info=True)
        await cq.answer("Ошибка при сохранении режима", show_alert=True)

@dp.callback_query(F.data == "set:sla_accept")
async def set_sla_accept_cb(cq: CallbackQuery, state: FSMContext):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка изменения SLA (принятие) без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос изменения SLA (принятие) от {cq.from_user.id}")
        current_value = get_setting("sla_accept", "10")
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Отмена", callback_data="admin_settings")
        
        await cq.message.edit_text(
            f"⏱️ <b>Время на принятие задачи</b>\n\n"
            f"Текущее значение: <b>{current_value} минут</b>\n\n"
            "Сколько времени дается сотруднику на принятие задачи после её создания?\n\n"
            "💡 <b>Рекомендации:</b>\n"
            "• 5-10 минут - для срочных задач\n"
            "• 15-30 минут - для стандартных задач\n"
            "• 60+ минут - для несрочных задач\n\n"
            "Введите число (в минутах):",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )
        
        await state.update_data(key="sla_accept", back_callback="admin_settings")
        await state.set_state(AdminInput.waiting_setting_value)
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в set_sla_accept_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data == "set:sla_fix")
async def set_sla_fix_cb(cq: CallbackQuery, state: FSMContext):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка изменения SLA (выполнение) без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос изменения SLA (выполнение) от {cq.from_user.id}")
        current_value = get_setting("sla_fix", "30")
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Отмена", callback_data="admin_settings")
        
        await cq.message.edit_text(
            f"⏱️ <b>Время на выполнение задачи</b>\n\n"
            f"Текущее значение: <b>{current_value} минут</b>\n\n"
            "Общее время, отведенное на выполнение задачи от момента принятия\n\n"
            "💡 <b>Рекомендации:</b>\n"
            "• 15-30 минут - для простых задач (уборка, проверка)\n"
            "• 60-120 минут - для средних задач (ремонт, доставка)\n"
            "• 240+ минут - для сложных задач (установка, настройка)\n\n"
            "Введите число (в минутах):",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )
        
        await state.update_data(key="sla_fix", back_callback="admin_settings")
        await state.set_state(AdminInput.waiting_setting_value)
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в set_sla_fix_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data == "set:post_channel_id")
async def set_post_channel_cb(cq: CallbackQuery, state: FSMContext):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка изменения канала уведомлений без прав: {cq.from_user.id}")
            return
        
        logger.debug(f"Запрос изменения канала уведомлений от {cq.from_user.id}")
        current_value = get_setting("post_channel_id", "Не задан")
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Отмена", callback_data="admin_settings")
        
        await cq.message.edit_text(
            f"📢 <b>Канал для уведомлений</b>\n\n"
            f"Текущее значение: <b>{current_value}</b>\n\n"
            "ID канала, куда будут публиковаться уведомления о выполненных задачах\n\n"
            "💡 <b>Как получить ID канала:</b>\n"
            "1. Добавьте бота в канал как администратора\n"
            "2. Отправьте в канал любое сообщение\n"
            "3. Перешлите это сообщение боту @userinfobot\n"
            "4. Скопируйте число после 'id:' и вставьте сюда\n\n"
            "❗ Оставьте \"0\" или \"Не задан\" чтобы отключить публикацию\n\n"
            "Введите ID канала:",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )
        
        await state.update_data(key="post_channel_id", back_callback="admin_settings")
        await state.set_state(AdminInput.waiting_setting_value)
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в set_post_channel_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("set:"))
async def set_val_cb(cq: CallbackQuery, state: FSMContext):
    try:
        if cq.from_user.id != OWNER_ID:
            logger.warning(f"Попытка изменения настройки без прав: {cq.from_user.id}")
            return
            
        key = cq.data.split(":")[1]
        logger.debug(f"Запрос изменения настройки '{key}' от {cq.from_user.id}")
        await state.update_data(key=key)
        await state.set_state(AdminInput.waiting_setting_value)
        await cq.message.answer(f"Введите новое значение для {key}:")
        await cq.answer()
    except Exception as e:
        logger.error(f"Ошибка в set_val_cb: {e}", exc_info=True)
        await cq.answer("Ошибка", show_alert=True)

@dp.message(AdminInput.waiting_setting_value)
async def save_val(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        key = data.get("key")
        back_callback = data.get("back_callback", "admin_menu")
        val = message.text.strip()
        
        if not key:
            logger.warning(f"Попытка сохранения настройки, но ключ не найден в state. Пользователь: {message.from_user.id}")
            await state.clear()
            return
            
        logger.info(f"Сохранение настройки '{key}' со значением '{val}' от {message.from_user.id}")
        set_setting(key, val)
        await state.clear()
        
        # Показываем подтверждение
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Понятно", callback_data=back_callback)
        
        await message.answer(
            f"✅ Настройка обновлена!\n\n"
            f"<b>{key}</b> теперь равно: <code>{val}</code>",
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка в save_val: {e}", exc_info=True)
        await message.answer("❌ Ошибка при сохранении настройки")


@dp.callback_query(F.data.startswith("task_done_"))
async def executor_done_cb(cq: CallbackQuery, state: FSMContext):
    task_id = int(cq.data.split("_")[-1])
    await state.set_state(ProofInput.waiting_proof)
    await state.update_data(task_id=task_id, prompt_message_id=cq.message.message_id)
    
    # Check if it's a TMA task or bot task
    # For now, just ask for proof
    sent = await cq.message.answer("📸 Пришлите фото-отчет или текст о выполнении задачи")
    await state.update_data(prompt_message_id=sent.message_id)
    await cq.answer()

@dp.message(ProofInput.waiting_proof)
async def process_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("task_id")
    prompt_message_id = data.get("prompt_message_id")
    
    # Check if this task exists in TMA (tma_server.py logic)
    # We'll use a simple API call to tma_server to complete it
    # or handle it locally if it's a bot task.
    # To keep it simple and robust, let's try to update TMA first.
    
    report_text = message.text or message.caption or "Отчет в виде фото"
    
    # For TMA integration:
    # We need to find if task_id belongs to TMA. 
    # Let's check the database.
    conn = db()
    t = conn.execute("SELECT creator_user_id, seq_no, territory, assigned_to FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    
    if t and t[3] == message.from_user.id:
        # This is a delegated task from TMA
        # Update TMA status
        # Note: In a real production app, we'd use a secret token for internal API calls.
        # Here we'll just update the DB directly since they share the same tasks.db.
        
        photo_path = None
        if message.photo:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            ext = file_info.file_path.split('.')[-1]
            filename = f"report_bot_{task_id}_{datetime.now().timestamp()}.{ext}"
            photo_path = filename
            upload_path = os.path.join(os.path.dirname(__file__), "uploads", filename)
            if not os.path.exists(os.path.dirname(upload_path)):
                os.makedirs(os.path.dirname(upload_path))
            await bot.download_file(file_info.file_path, upload_path)

        conn = db()
        conn.execute(
            "UPDATE tasks SET status='completed', fixed_at=?, report_text=?, report_photo=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), report_text, photo_path, task_id)
        )
        conn.commit()
        conn.close()
        
        await message.reply("✅ Отчет принят! Задача помечена как выполненная.")
        
        # Notify creator
        creator_id = t[0]
        msg = f"✅ <b>Задача #{t[1]} выполнена!</b>\n📍 Территория: {t[2]}\n👤 Исполнитель: {message.from_user.full_name}\n📝 Отчет: {report_text}"
        
        # Send notification to creator
        from tma_server import send_telegram_msg # Import helper
        await send_telegram_msg(creator_id, msg, photo_path=photo_path)
        
        if prompt_message_id:
            try: await bot.delete_message(message.chat.id, prompt_message_id)
            except: pass
            
        await state.clear()
        return

    # Fallback to original bot task logic if it wasn't a TMA delegated task
    logger.debug(f"Получено подтверждение для задачи {task_id} от {message.from_user.id}")

    if not task_id:
        logger.warning("task_id не найден в state при обработке подтверждения")
        await state.clear()
        return

    try:
        conn = db()
        t = get_task(conn, task_id)
        if not t:
            conn.close()
            logger.warning(f"Задача {task_id} не найдена при обработке подтверждения")
            await state.clear()
            return
        
        sh = conn.execute("SELECT leader_user_id, leader_username, leader_full_name FROM shifts WHERE id=?", (t[2],)).fetchone()
        if not sh or sh[0] != message.from_user.id:
            conn.close()
            logger.warning(f"Пользователь {message.from_user.id} не является руководителем смены {t[2]} при отправке подтверждения")
            msg = await message.reply("Только руководитель смены может закрыть задачу")
            asyncio.create_task(auto_delete_message(message.chat.id, msg.message_id, 5))
            await state.clear()
            return
        
        fix_task(conn, task_id, message.message_id)
        t = get_task(conn, task_id)
        kb = build_keyboard_for_status(t)
        text = card_text(t, sh[1] if sh else None, sh[2] if sh else None)
        confirm = await message.reply("✅ Карточка закрыта")
        asyncio.create_task(auto_delete_message(message.chat.id, confirm.message_id, 5))
        
        if t[6]:
            try:
                await bot.edit_message_text(text=text, chat_id=t[5], message_id=t[6], reply_markup=kb.as_markup())
                logger.info(f"Карточка задачи #{t[1]} (ID: {task_id}) обновлена на 'closed'")
            except Exception as e:
                logger.error(f"Ошибка при обновлении карточки {t[6]}: {e}")
        
        conn.close()
        
        if prompt_message_id:
            try:
                await bot.delete_message(message.chat.id, prompt_message_id)
            except Exception as e:
                logger.debug(f"Не удалось удалить промпт подтверждения {prompt_message_id}: {e}")
        
        logger.info(f"Задача #{t[1]} (ID: {task_id}) успешно закрыта")
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка в process_proof: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при сохранении подтверждения.")
        await state.clear()

async def auto_unpin_at_midnight():
    """Фоновая задача для открепления сообщений в полночь"""
    while True:
        try:
            now = datetime.now()
            # Вычисляем время до следующей полночи
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (next_midnight - now).total_seconds()
            
            logger.debug(f"Планировщик открепления: до полночи осталось {wait_seconds} секунд")
            await asyncio.sleep(wait_seconds)
            
            # Наступила полночь
            logger.info("Наступила полночь. Выполняю открепление сообщений...")
            
            main_group_id = get_setting("main_group_id")
            if main_group_id:
                try:
                    chat_id = int(main_group_id)
                    await bot.unpin_all_chat_messages(chat_id)
                    logger.info(f"Все сообщения в чате {chat_id} откреплены")
                except Exception as e:
                    logger.warning(f"Не удалось открепить сообщения в чате {main_group_id}: {e}")
            else:
                logger.debug("main_group_id не задан, открепление пропущено")
                
        except Exception as e:
            logger.error(f"Ошибка в auto_unpin_at_midnight: {e}", exc_info=True)
            await asyncio.sleep(60) # В случае ошибки ждем минуту и пробуем снова

async def main():
    init_db()
    # Запускаем фоновую задачу открепления
    asyncio.create_task(auto_unpin_at_midnight())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
