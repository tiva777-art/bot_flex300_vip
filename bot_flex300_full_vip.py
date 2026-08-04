

import asyncio
import json
import logging
import os
import random
import sqlite3
import string
import threading
import time
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

import aiohttp
import requests
from requests.adapters import HTTPAdapter

# ================== طبقة تسريع واستقرار الشبكة ==================
_HTTP_ADAPTER = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
HTTP_SESSION = requests.Session()
HTTP_SESSION.mount("https://", _HTTP_ADAPTER)
HTTP_SESSION.mount("http://", _HTTP_ADAPTER)

# جلسة مستقلة للطلبات التي تحتاج WebsiteConsumer لتقليل إعادة إنشاء الاتصالات
WEB_HTTP_SESSION = requests.Session()
WEB_HTTP_SESSION.mount("https://", _HTTP_ADAPTER)
WEB_HTTP_SESSION.mount("http://", _HTTP_ADAPTER)


# قفل خفيف للعمليات الحساسة: يمنع ضغط الزر مرتين وتشغيل نفس العملية بالتوازي

# كاش قصير لاسم النظام لتجنب طلب الشبكة المتكرر عند فتح القوائم بسرعة
SYSTEM_CACHE: Dict[str, Tuple[float, str]] = {}
SYSTEM_CACHE_TTL = 30

USER_OPERATION_LOCKS: Dict[int, asyncio.Lock] = {}

def get_user_operation_lock(user_id: int) -> asyncio.Lock:
    lock = USER_OPERATION_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_OPERATION_LOCKS[user_id] = lock
    return lock

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import re

# ================== الإعدادات العامة ==================
BOT_TOKEN = "8750833576:AAF8-UqIdOvUjGVfkpgun_XrdgRZ8rimBfk"
ADMIN_ID = 1444139300  # ضع Telegram ID الخاص بالأدمن هنا
DB_PATH = Path("family_bot.db")
LOG_FILE = Path("bot_actions.log")
MAINTENANCE_MODE = False
TOKEN_REFRESH_THRESHOLD = 300
SEND_METHOD = 1  # 1 = aiohttp, 2 = threading

SYSTEM_TRANSLATIONS = {
    '14pt_Raya7Balak': 'ريح بالك كله 14 قرش',
    'Flex_40': 'فليكس 40', 'Flex_60': 'فليكس 60', 'Flex_90': 'فليكس 90',
    'Flex_130': 'فليكس 130', 'Flex_260': 'فليكس 260',
    'Flex_45': 'فليكس 45', 'Flex_70': 'فليكس 70', 'Flex_100': 'فليكس 100',
    'Flex_150': 'فليكس 150', 'Flex_300': 'فليكس 300',
    'Flex_Family_Member': 'فليكس فاميلي', 'Flex_Family': 'فليكس فاميلي', 'Family_Flex': 'فليكس فاميلي',
}
SUPPORTED_SYSTEMS = []  # تسجيل الدخول غير مقيد بنوع الباقة/النظام

# ================== إعداد التسجيل ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger("family_bot")

def log_action(user_id: int, action: str, details: str = "") -> None:
    logger.info(f"User {user_id}: {action} - {details}")

def admin_log(action: str, details: str = "") -> None:
    logger.info(f"ADMIN: {action} - {details}")

# ================== دوال مساعدة عامة ==================
def generate_digital_id() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=13))

def generate_device_id() -> str:
    return ''.join(random.choices('0123456789abcdef', k=16))

def get_random_device_info() -> Tuple[str, str]:
    return random.choice(["Samsung SM-A515F", "OPPO CPH2473"]), random.choice(["13", "14"])

def convert_to_local(phone: str) -> str:
    if phone.startswith("20") and len(phone) == 12:
        return "0" + phone[2:]
    return phone

def translate_system(system_name: Optional[str]) -> str:
    if not system_name:
        return "غير محدد"
    for key, value in SYSTEM_TRANSLATIONS.items():
        if key in system_name:
            return value
    return system_name

def validate_phone(number: str) -> Tuple[bool, str]:
    if not number.isdigit():
        return False, "❌ الرقم يجب أن يحتوي على أرقام فقط"
    if len(number) != 11:
        return False, "❌ الرقم يجب أن يكون 11 رقماً"
    if not number.startswith('01'):
        return False, "❌ الرقم يجب أن يبدأ بـ 01"
    return True, "✅"

# ================== تشفير وإدارة كلمات المرور ==================
def encrypt_password(password: str) -> str:
    return base64.b64encode(password.encode()).decode()

def decrypt_password(encrypted: str) -> str:
    return base64.b64decode(encrypted.encode()).decode()

def save_password(user_id: int, number: str, password: str, expiry_hours: int = 24):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS saved_passwords 
                 (telegram_id INTEGER, number TEXT, password_encrypted TEXT, expiry TIMESTAMP,
                 PRIMARY KEY (telegram_id, number))''')
    expiry = datetime.now() + timedelta(hours=expiry_hours)
    encrypted = encrypt_password(password)
    c.execute("INSERT OR REPLACE INTO saved_passwords (telegram_id, number, password_encrypted, expiry) VALUES (?, ?, ?, ?)",
              (user_id, number, encrypted, expiry.isoformat()))
    conn.commit()
    conn.close()

def get_saved_password(user_id: int, number: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT password_encrypted, expiry FROM saved_passwords WHERE telegram_id = ? AND number = ?", (user_id, number))
    row = c.fetchone()
    conn.close()
    if row:
        encrypted, expiry_str = row
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry > datetime.now():
                return decrypt_password(encrypted)
        except:
            # إذا فشل تحويل التاريخ، نحذف المدخل لأنه قد يكون فاسداً
            delete_saved_password(user_id, number)
            return None
    return None

def delete_saved_password(user_id: int, number: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM saved_passwords WHERE telegram_id = ? AND number = ?", (user_id, number))
    conn.commit()
    conn.close()

# ================== قاعدة البيانات (مع جداول الأدمن والاشتراكات) ==================
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # الجداول الأصلية
    c.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, phone TEXT, access_token TEXT, token_expiry TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_numbers (telegram_id INTEGER, number TEXT, PRIMARY KEY (telegram_id, number))''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (telegram_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (telegram_id INTEGER PRIMARY KEY, first_seen TIMESTAMP, last_seen TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_tokens (user_id INTEGER, number TEXT, token TEXT, expiry TIMESTAMP, PRIMARY KEY (user_id, number))''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_passwords (telegram_id INTEGER, number TEXT, password_encrypted TEXT, expiry TIMESTAMP, PRIMARY KEY (telegram_id, number))''')
    
    # جداول جديدة للأدمن والاشتراكات
    c.execute('''CREATE TABLE IF NOT EXISTS admins (telegram_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (telegram_id INTEGER PRIMARY KEY, expiry TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    
    # إضافة الأدمن الرئيسي إذا لم يكن موجوداً
    c.execute("INSERT OR IGNORE INTO admins (telegram_id) VALUES (?)", (ADMIN_ID,))
    # إضافة الوضع المجاني افتراضياً (1 = مجاني، 0 = اشتراك)
    c.execute("INSERT OR IGNORE INTO bot_config (key, value) VALUES ('free_mode', '1')")
    conn.commit()
    conn.close()

init_db()

# ================== دوال الأدمن الجديدة ==================
def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE telegram_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def add_admin(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO admins (telegram_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    return True

def remove_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False  # لا يمكن إزالة الأدمن الرئيسي
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE telegram_id = ?", (user_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0

def list_admins() -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM admins")
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def is_free_mode() -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_config WHERE key = 'free_mode'")
    row = c.fetchone()
    conn.close()
    return row is not None and row[0] == '1'

def set_free_mode(enabled: bool):
    value = '1' if enabled else '0'
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES ('free_mode', ?)", (value,))
    conn.commit()
    conn.close()

# ================== دوال الاشتراكات ==================
def get_subscription_expiry(user_id: int) -> Optional[datetime]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT expiry FROM subscriptions WHERE telegram_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row[0])
    return None

def is_subscription_active(user_id: int) -> bool:
    expiry = get_subscription_expiry(user_id)
    if expiry is None:
        return False
    return expiry > datetime.now()

def add_subscription(user_id: int, days: int) -> datetime:
    new_expiry = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO subscriptions (telegram_id, expiry) VALUES (?, ?)", (user_id, new_expiry.isoformat()))
    conn.commit()
    conn.close()
    return new_expiry

def remove_subscription(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM subscriptions WHERE telegram_id = ?", (user_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0

def list_active_subscriptions() -> List[Tuple[int, datetime]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT telegram_id, expiry FROM subscriptions WHERE expiry > ?", (now,))
    rows = c.fetchall()
    conn.close()
    return [(row[0], datetime.fromisoformat(row[1])) for row in rows]

# ================== دالة التحقق من الاشتراك (تستثني الأدمن) ==================
async def require_subscription_or_free(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool = False, query=None) -> bool:
    # تحديد user_id بشكل صحيح
    if is_callback and query is not None:
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id
    
    # الأدمن يستخدم البوت دائماً
    if is_admin(user_id):
        return True
    if is_free_mode():
        return True
    if is_subscription_active(user_id):
        return True
    
    text = "🔒 **هذا البوت يعمل بنظام الاشتراك.**\n\nلم يعد لديك اشتراك نشط.\nيرجى التواصل مع الأدمن للحصول على اشتراك.\n📞 تواصل مع المالك: @tiva7777"
    if is_callback and query:
        await query.edit_message_text(text, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, parse_mode='Markdown')
    return False

# ================== دوال المستخدم الأساسية (بدون تغيير) ==================
def is_user_banned(telegram_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM banned_users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def ban_user(telegram_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO banned_users (telegram_id) VALUES (?)", (telegram_id,))
    conn.commit()
    conn.close()

def unban_user(telegram_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def record_user_activity(telegram_id: int) -> None:
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO user_activity (telegram_id, first_seen, last_seen) VALUES (?, ?, ?)", (telegram_id, now, now))
    c.execute("UPDATE user_activity SET last_seen = ? WHERE telegram_id = ?", (now, telegram_id))
    conn.commit()
    conn.close()

def get_all_users() -> List[Tuple[int, datetime, datetime]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_id, first_seen, last_seen FROM user_activity ORDER BY last_seen DESC")
    rows = c.fetchall()
    conn.close()
    user_list = []
    for uid, first, last in rows:
        first_dt = datetime.fromisoformat(first) if isinstance(first, str) else first
        last_dt = datetime.fromisoformat(last) if isinstance(last, str) else last
        user_list.append((uid, first_dt, last_dt))
    return user_list

def get_stats() -> Dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM user_activity")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users")
    active_sessions = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM saved_numbers")
    saved_numbers = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM banned_users")
    banned_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM subscriptions WHERE expiry > datetime('now')")
    active_subs = c.fetchone()[0]
    conn.close()
    return {'total_users': total_users, 'active_sessions': active_sessions,
            'saved_numbers': saved_numbers, 'banned_count': banned_count, 'active_subs': active_subs}

def save_number(telegram_id: int, number: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO saved_numbers (telegram_id, number) VALUES (?, ?)", (telegram_id, number))
    conn.commit()
    conn.close()

def get_saved_numbers(telegram_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT number FROM saved_numbers WHERE telegram_id = ?", (telegram_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def delete_saved_number(telegram_id: int, number: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM saved_numbers WHERE telegram_id = ? AND number = ?", (telegram_id, number))
    conn.commit()
    conn.close()
    delete_saved_password(telegram_id, number)

def save_token(user_id: int, number: str, token: str, expiry: datetime) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO saved_tokens (user_id, number, token, expiry) VALUES (?, ?, ?, ?)",
              (user_id, number, token, expiry.isoformat()))
    conn.commit()
    conn.close()

def get_valid_token(user_id: int, number: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token, expiry FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, number))
    row = c.fetchone()
    conn.close()
    if row:
        token, expiry_str = row
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry > datetime.now():
                return token
        except:
            pass
    return None

def save_user(telegram_id: int, phone: str, token: str, expiry: datetime) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO users (telegram_id, phone, access_token, token_expiry) VALUES (?, ?, ?, ?)',
              (telegram_id, phone, token, expiry.isoformat()))
    conn.commit()
    conn.close()
    save_token(telegram_id, phone, token, expiry)

def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT phone, access_token, token_expiry FROM users WHERE telegram_id = ?', (telegram_id,))
    row = c.fetchone()
    conn.close()
    if row:
        phone, token, expiry_str = row
        expiry = datetime.fromisoformat(expiry_str)
        return {'phone': phone, 'access_token': token, 'token_expiry': expiry}
    return None

def delete_user(telegram_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
    conn.commit()
    conn.close()

# ================== API فودافون (وظائف البوت الأساسية) ==================
def build_base_headers(additional: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    digital_id = generate_digital_id()
    device_id = generate_device_id()
    device_model, os_version = get_random_device_info()
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Connection': 'Keep-Alive',
        'Accept': 'application/json',
        'device-id': device_id,
        'x-agent-operatingsystem': os_version,
        'clientId': 'AnaVodafoneAndroid',
        'x-agent-device': device_model,
        'x-agent-version': '2026.4.1',
        'x-agent-build': '1139',
        'Accept-Language': 'ar',
        'Content-Type': 'application/json; charset=UTF-8',
        'digitalId': digital_id,
    }
    if additional:
        headers.update(additional)
    return headers

async def login_vodafone(phone: str, password: str) -> Tuple[str, datetime]:
    headers = build_base_headers({
        'Content-Type': 'application/x-www-form-urlencoded',
        'silentLogin': 'true',
        'msisdn': phone,
    })
    data = {
        'grant_type': 'password',
        'username': phone,
        'password': password,
        'client_secret': 'dca0pbLUWXVhXR266Gw1iT5rqwvvJQoN',
        'client_id': 'AnaVF',
    }
    url = 'https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token'
    def sync_login():
        resp = HTTP_SESSION.post(url, headers=headers, data=data, timeout=30)
        if resp.status_code == 200:
            token = resp.json().get('access_token')
            expires_in = resp.json().get('expires_in', 3600)
            return token, expires_in
        else:
            raise Exception(f"فشل تسجيل الدخول: {resp.status_code}")
    token, exp_in = await asyncio.to_thread(sync_login)
    expiry = datetime.now() + timedelta(seconds=exp_in)
    SYSTEM_CACHE.pop(phone, None)
    return token, expiry

async def get_current_system(token: str, phone: str) -> str:
    cached = SYSTEM_CACHE.get(phone)
    now = time.time()
    if cached and (now - cached[0]) < SYSTEM_CACHE_TTL:
        return cached[1]
    url = f"https://web.vodafone.com.eg/services/dxl/sam/serviceAccountManagement/v1/serviceAccount?@type=userProfile&$.resources%5B?(@resourceType%3D%3D%27MSISDN%27)%5D.IDs%5B0%5D.value={phone}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13; SM-A515F) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ar-EG,ar;q=0.9,en;q=0.8',
        'Authorization': f'Bearer {token}',
        'api-version': 'v3',
        'clientId': 'AnaVodafoneAndroid',
        'device-id': '7be546fe335911d2',
        'x-agent-build': '1063',
        'x-agent-device': 'Samsung SM-A515F',
        'x-agent-operatingsystem': '13',
        'x-agent-version': '2025.11.1',
        'msisdn': phone,
        'Content-Type': 'application/json',
        'Connection': 'keep-alive',
        'Referer': 'https://web.vodafone.com.eg/',
        'Origin': 'https://web.vodafone.com.eg'
    }
    def sync_get():
        resp = HTTP_SESSION.get(url, headers=headers, timeout=15)
        return resp.status_code, resp.text
    status, text = await asyncio.to_thread(sync_get)
    if status != 200:
        return f"خطأ في جلب الباقة (كود {status})"
    try:
        data = json.loads(text)
        if data and len(data) > 0:
            subscriptions = data[0].get('subscriptions', [])
            for sub in subscriptions:
                if sub.get('productType') == 'ServiceClass':
                    system_name = sub.get('name', 'غير معروف')
                    translated = translate_system(system_name)
                    SYSTEM_CACHE[phone] = (time.time(), translated)
                    return translated
        return "لا توجد باقة نشطة"
    except:
        return "خطأ في تحليل البيانات"

async def get_family_members(access_token: str, msisdn: str) -> Dict[str, List[Dict[str, Any]]]:
    headers = build_base_headers({
        'Authorization': f'Bearer {access_token}',
        'api-version': 'v2',
        'msisdn': msisdn,
    })
    params = {'type': 'Family'}
    def sync_get():
        resp = HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', params=params, headers=headers, timeout=30)
        return resp.status_code, resp.text
    status, text = await asyncio.to_thread(sync_get)
    if status == 404:
        return {'active': [], 'pending': []}
    if status != 200:
        raise Exception(f"فشل جلب العائلة: {status}")
    result = json.loads(text)
    if not result:
        return {'active': [], 'pending': []}
    family = result[0]
    members = family.get('parts', {}).get('member', [])
    active = []
    pending_raw = []
    for m in members:
        if m.get('type') == 'Owner':
            continue
        ids = m.get('id', [])
        phone = None
        for id_obj in ids:
            if id_obj.get('schemeName') == 'MSISDN':
                phone = id_obj.get('value')
                break
        if not phone:
            continue
        phone_local = convert_to_local(phone)
        status_code = str(m.get('status'))
        chars = m.get('characteristic', {}).get('characteristicsValue', [])
        flex_value = None
        for ch in chars:
            if ch.get('characteristicName') == 'flex':
                flex_value = ch.get('value')
        if status_code == "1":
            active.append({'phone': phone_local, 'flex': flex_value})
        elif status_code == "5":
            pending_raw.append({'phone': phone_local, 'flex': flex_value})
    pending_dict = defaultdict(list)
    for p in pending_raw:
        pending_dict[p['phone']].append(p['flex'])
    pending = []
    for phone, flexes in pending_dict.items():
        pending.append({'phone': phone, 'flex': flexes[0], 'count': len(flexes)})
    return {'active': active, 'pending': pending}

async def send_invitation(token: str, owner_msisdn: str, member_phone: str, percentage: int) -> bool:
    headers = build_base_headers({
        'Authorization': f'Bearer {token}',
        'msisdn': owner_msisdn,
    })
    json_data = {
        'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}],
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}],
        },
        'type': 'SendInvitation',
    }
    def sync_post():
        resp = HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
        return resp.status_code, resp.text
    status, _ = await asyncio.to_thread(sync_post)
    return status in (200, 201)

async def accept_invitation(member_token: str, member_phone: str, owner_msisdn: str) -> bool:
    headers = build_base_headers({
        'api_id': 'APP',
        'Authorization': f'Bearer {member_token}',
        'msisdn': member_phone,
    })
    json_data = {
        'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}],
        'name': 'FlexFamily',
        'parts': {'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]},
        'type': 'AcceptInvitation',
    }
    def sync_patch():
        resp = WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
        return resp.status_code, resp.text
    status, _ = await asyncio.to_thread(sync_patch)
    return status in (200, 201)

async def update_quota(token: str, owner_msisdn: str, member_phone: str, percentage: int) -> bool:
    headers = build_base_headers({
        'Authorization': f'Bearer {token}',
        'msisdn': owner_msisdn,
    })
    json_data = {
        'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}],
        'createdBy': {'value': 'MobileApp'},
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}],
        },
        'type': 'QuotaRedistribution',
    }
    def sync_patch():
        resp = WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
        return resp.status_code, resp.text
    status, _ = await asyncio.to_thread(sync_patch)
    return status in (200, 201)

async def remove_member(token: str, owner_msisdn: str, member_phone: str) -> bool:
    headers = build_base_headers({
        'Authorization': f'Bearer {token}',
        'msisdn': owner_msisdn,
    })
    json_data = {
        'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}],
        'createdBy': {'value': 'MobileApp'},
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'Disconnect', 'value': '0'}, {'characteristicName': 'LastMemberDeletion', 'value': '1'}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}],
        },
        'type': 'FamilyRemoveMember',
    }
    def sync_patch():
        resp = WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
        return resp.status_code, resp.text
    status, _ = await asyncio.to_thread(sync_patch)
    return status in (200, 201)

async def cancel_invitation(token: str, owner_msisdn: str, member_phone: str) -> bool:
    headers = build_base_headers({
        'Authorization': f'Bearer {token}',
        'msisdn': owner_msisdn,
    })
    json_data = {
        'category': [{'listHierarchyId': 'TemplateID', 'value': '0'}],
        'createdBy': {'value': 'MobileApp'},
        'name': 'FlexFamily',
        'parts': {'member': [{'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}, {'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}]},
        'type': 'CancelInvitation',
    }
    def sync_patch():
        resp = WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
        return resp.status_code, resp.text
    status, _ = await asyncio.to_thread(sync_patch)
    return status in (200, 201)

async def get_owner_remaining(token: str, phone: str) -> Optional[str]:
    headers = build_base_headers({
        'api-host': 'usageConsumptionHost',
        'useCase': 'aggregated',
        'Authorization': f'Bearer {token}',
        'msisdn': phone,
    })
    params = {'@type': 'aggregated', 'bucket.product.publicIdentifier': phone}
    def sync_get():
        resp = HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/usage/usageConsumptionReport', params=params, headers=headers, timeout=15)
        return resp.status_code, resp.text
    status, text = await asyncio.to_thread(sync_get)
    if status != 200:
        return None
    try:
        data = json.loads(text)
        def find_limit(obj):
            if isinstance(obj, dict):
                if obj.get("usageType") == "limit":
                    balances = obj.get("bucketBalance", [])
                    for bal in balances:
                        if bal.get("@type") == "Remaining":
                            remaining = bal.get("remainingValue", {})
                            amount = remaining.get("amount")
                            units = remaining.get("units")
                            if amount is not None and units is not None:
                                return amount, units
                for v in obj.values():
                    res = find_limit(v)
                    if res:
                        return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_limit(item)
                    if res:
                        return res
            return None
        res = find_limit(data)
        if res:
            amount, units = res
            if isinstance(amount, (int, float)) and amount.is_integer():
                amount = int(amount)
            return f"{amount} {units}"
        return None
    except:
        return None

async def get_member_consumption_from_owner(owner_token: str, owner_phone: str, member_phone: str) -> Optional[Dict[str, str]]:
    headers = build_base_headers({
        'api-host': 'usageConsumptionHost',
        'useCase': 'familyDetailed',
        'Authorization': f'Bearer {owner_token}',
        'msisdn': owner_phone,
    })
    params = {'@type': 'familyDetailed', 'bucket.product.publicIdentifier': member_phone}
    def sync_get():
        resp = HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/usage/usageConsumptionReport', params=params, headers=headers, timeout=15)
        return resp.status_code, resp.text
    status, text = await asyncio.to_thread(sync_get)
    if status != 200:
        return None
    try:
        data = json.loads(text)
        for item in data:
            if item.get('name') == 'Flex_Family_Main_Bundle':
                for bucket in item.get('bucket', []):
                    if bucket.get('usageType') == 'FLEX':
                        remaining = None
                        used = None
                        for bal in bucket.get('bucketBalance', []):
                            if bal.get('@type') == 'Remaining':
                                remaining = bal.get('remainingValue', {})
                        for counter in bucket.get('bucketCounter', []):
                            if counter.get('@type') == 'Used' and counter.get('level') == 'Local':
                                used = counter.get('value', {})
                        return {
                            'remaining': f"{remaining.get('amount', 0)} {remaining.get('units', 'FLEX')}" if remaining else "غير متاح",
                            'used': f"{used.get('amount', 0)} {used.get('units', 'FLEX')}" if used else "غير متاح"
                        }
        return None
    except:
        return None

# ================== إدارة الرسائل والحالات ==================
user_messages: Dict[int, List[int]] = {}
user_states: Dict[int, Dict[str, Any]] = {}

async def clear_previous_messages(update: Update, user_id: int) -> None:
    if user_id not in user_messages:
        return
    remaining = []
    for msg_id in user_messages[user_id]:
        try:
            await update.effective_bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except:
            remaining.append(msg_id)
    if remaining:
        user_messages[user_id] = remaining
    else:
        user_messages[user_id] = []

async def send_message(update: Update, user_id: int, text: str,
                       keyboard: Optional[InlineKeyboardMarkup] = None,
                       clear_before: bool = False) -> None:
    if clear_before:
        await clear_previous_messages(update, user_id)
    if update.callback_query:
        msg = await update.callback_query.message.reply_text(text, reply_markup=keyboard, parse_mode=None)
    else:
        msg = await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=None)
    user_messages.setdefault(user_id, []).append(msg.message_id)

async def edit_and_track(query, user_id: int, text: str,
                         reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=None)
    user_messages.setdefault(user_id, []).append(query.message.message_id)

async def ensure_valid_session(update: Update, user_id: int) -> Optional[Dict[str, Any]]:
    user = get_user(user_id)
    if not user:
        return None
    if datetime.now() < (user['token_expiry'] - timedelta(seconds=30)):
        return user

    saved_password = get_saved_password(user_id, user['phone'])
    if saved_password:
        try:
            token, expiry = await login_vodafone(user['phone'], saved_password)
            save_user(user_id, user['phone'], token, expiry)
            refreshed = get_user(user_id)
            if refreshed:
                return refreshed
        except Exception as e:
            log_action(user_id, "AUTO_SESSION_REFRESH_FAIL", str(e))

    delete_user(user_id)
    await send_message(update, user_id, "⏰ انتهت صلاحية جلستك. برجاء تسجيل الدخول مجدداً.")
    await show_login_screen(update, user_id, None)
    return None

# ================== شاشة تسجيل الدخول ==================
async def show_login_screen(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    saved_numbers = get_saved_numbers(user_id)
    if saved_numbers:
        keyboard = []
        for num in saved_numbers:
            token_valid = get_valid_token(user_id, num) is not None
            display_num = num + (" 🔑" if token_valid else "")
            keyboard.append([InlineKeyboardButton(display_num, callback_data=f"use_num_{num}")])
        keyboard.append([InlineKeyboardButton("📞 إدخال رقم جديد", callback_data="new_manual_login")])
        await send_message(update, user_id, "📱 اختر رقمًا لتسجيل الدخول أو قم بإدخال رقم جديد:", InlineKeyboardMarkup(keyboard))
    else:
        await send_message(update, user_id, "يرجى تسجيل الدخول أولاً.\nأرسل رقم هاتفك (مثال: 01012345678)")
        user_states[user_id] = {'state': 'login_phone'}

# ================== تنظيف العائلة ==================
# -*- coding: utf-8 -*-
# ... جميع الأكواد السابقة كما هي دون تغيير حتى دالة perform_full_cleaning_silent ...

# نستبدل دالة perform_full_cleaning_silent بالآتي:

async def perform_full_cleaning_silent(update: Update, user_id: int, owner_num: str, owner_pass: str,
                                        member_num: str, member_pass: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    تنظيف العائلة مع شريط تقدم (progress bar) ونسبة مئوية.
    لا تظهر رسائل خطأ تفصيلية، فقط رسالة فشل عامة في النهاية.
    """
    # نرسل رسالة البداية (سيتم تحديثها لاحقاً)
    progress_msg = await update.message.reply_text("🧹 **تنظيف العائلة**\n[░░░░░░░░░░░░░░░░░░░░] 0%", parse_mode='Markdown')
    
    def update_progress(percent: int, bar_length: int = 20):
        filled = int(bar_length * percent // 100)
        bar = "█" * filled + "░" * (bar_length - filled)
        text = f"🧹 **تنظيف العائلة**\n[{bar}] {percent}%"
        asyncio.create_task(progress_msg.edit_text(text, parse_mode='Markdown'))
    
    try:
        # الخطوة 1: تسجيل المالك (0% -> 10%)
        update_progress(10)
        owner_token, _ = await login_vodafone(owner_num, owner_pass)
        await asyncio.sleep(0.5)
        
        # الخطوة 2: دعوة أولى 10% (10% -> 20%)
        update_progress(20)
        await send_invitation(owner_token, owner_num, member_num, 10)
        await asyncio.sleep(7)
        
        # الخطوة 3: حذف أولي (20% -> 30%)
        update_progress(30)
        await remove_member(owner_token, owner_num, member_num)
        await asyncio.sleep(7)
        
        # الخطوة 4: دعوة جديدة (30% -> 50%)
        update_progress(50)
        if not await send_invitation(owner_token, owner_num, member_num, 10):
            await progress_msg.edit_text("❌ لم يتم تنظيف العائلة", parse_mode='Markdown')
            return
        await asyncio.sleep(7)
        
        # الخطوة 5: تسجيل العضو وقبول الدعوة (50% -> 70%)
        update_progress(70)
        member_token, _ = await login_vodafone(member_num, member_pass)
        if not await accept_invitation(member_token, member_num, owner_num):
            await progress_msg.edit_text("❌ لم يتم تنظيف العائلة", parse_mode='Markdown')
            return
        await asyncio.sleep(7)
        
        # الخطوة 6: تعديل النسبة إلى 40% (70% -> 85%)
        update_progress(85)
        if not await update_quota(owner_token, owner_num, member_num, 40):
            await progress_msg.edit_text("❌ لم يتم تنظيف العائلة", parse_mode='Markdown')
            return
        await asyncio.sleep(7)
        
        # الخطوة 7: حذف نهائي (85% -> 100%)
        update_progress(95)
        for attempt in range(3):
            if await remove_member(owner_token, owner_num, member_num):
                break
            await asyncio.sleep(7)
        else:
            await progress_msg.edit_text("❌ لم يتم تنظيف العائلة", parse_mode='Markdown')
            return
        
        update_progress(100)
        await asyncio.sleep(2)
        await progress_msg.edit_text("✅ **تم تنظيف العائلة بنجاح!**", parse_mode='Markdown')
        
    except Exception:
        await progress_msg.edit_text("❌ لم يتم تنظيف العائلة", parse_mode='Markdown')




# ================== تزويد يومين ==================
async def add_two_days(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    operation_lock = get_user_operation_lock(user_id)
    if operation_lock.locked():
        await send_message(update, user_id, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.", None)
        return
    """إضافة يومين لباقة فليكس باستخدام الجلسة الحالية."""
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return

    phone = user['phone']
    token = user['access_token']
    url = "https://mobile.vodafone.com.eg/services/dxl/pom/productOrder"

    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Connection': 'Keep-Alive',
        'Accept': 'application/json',
        'api-host': 'ProductOrderingManagement',
        'useCase': 'FlexACPRenewal',
        'Authorization': f'Bearer {token}',
        'api-version': 'v2',
        'device-id': '7be546fe335911d2',
        'x-agent-operatingsystem': '13',
        'clientId': 'AnaVodafoneAndroid',
        'x-agent-device': 'Samsung SM-A515F',
        'x-agent-version': '2025.11.1',
        'x-agent-build': '1063',
        'msisdn': phone,
        'Accept-Language': 'ar',
        'Content-Type': 'application/json; charset=UTF-8',
        'X-Request-ID': f'FLEX_{int(datetime.now().timestamp())}'
    }

    payload = {
        "channel": {"name": "MobileApp"},
        "orderItem": [{
            "action": "insert",
            "id": "FLEX_ROLLOVER",
            "product": {
                "characteristic": [
                    {"name": "PaymentMethod", "value": "ACP"},
                    {"name": "ACP", "value": "True"},
                    {"name": "SMSID", "value": "MUTE_SMS"}
                ],
                "encProductId": "8UaJf2TAFkQncd2YduZwRAbXrixRX0FbzsLM/WakGJhTeK0tEE5sc5f4a8TXyo9FYlvdb1bT75cEIo+D1y5rkWQUjSwKtd0EJRxzp9wIrbuKtupFTfAhNLcfTdjt7hDOMkiZSWlblb1Ne9CrZgq2/ptclZCRIlstBSrTme3YLhxCUzhbMrwOuYQoJaMg6uXx/FrIO9Dd3tgz2kTemb36IO5s7N0rSYVzyzPOoHJb5gGC69r3InrZ9JJql/vr5cS+BP33wKGpUNg9P207RLPthMUVyTZSrCnGQMQ+fXXMocCGTEwzOH/D7DRf2GCGg2OHZLL/DfL1cc0Sh6Yoc1hb",
                "id": "Flex_2021_523",
                "relatedParty": [{
                    "id": phone,
                    "name": "MSISDN",
                    "role": "Subscriber"
                }]
            },
            "eCode": 0
        }],
        "@type": "FlexACPRenewal"
    }

    try:
        await query_or_message(update, "⏳ جاري محاولة تزويد الباقة يومين...")
        def sync_request():
            return HTTP_SESSION.post(url, headers=headers, json=payload, timeout=30)
        response = await asyncio.to_thread(sync_request)

        if 200 <= response.status_code < 300:
            try:
                result = response.json()
            except Exception:
                result = {}
            if result.get("state") == "Completed":
                msg = "✅ تم إضافة يومين لباقة فليكس بنجاح 🎉"
            else:
                msg = f"✅ تم قبول الطلب بنجاح. كود الحالة: {response.status_code}"
        elif response.status_code == 400:
            try:
                error_data = response.json()
            except Exception:
                error_data = {}
            code = str(error_data.get("code", ""))
            if code == "2037":
                msg = "⚠️ تم استخدام تزويد اليومين من قبل لهذا الشهر."
            elif code == "1001":
                msg = "🔒 انتهت صلاحية الجلسة. سجل الدخول مرة أخرى."
            elif code == "1005":
                msg = "📵 رقم الهاتف غير صالح."
            else:
                reason = error_data.get("reason", "الطلب غير متاح حالياً")
                msg = f"❌ لم يتم تزويد اليومين.\nالسبب: {reason}"
        elif response.status_code == 401:
            msg = "🔒 الجلسة غير صالحة أو انتهت. سجل الدخول مرة أخرى."
        elif response.status_code == 403:
            msg = "⛔ الخدمة غير متاحة لهذا الحساب حالياً."
        else:
            msg = f"❌ لم يتم تزويد اليومين. كود الخادم: {response.status_code}"
    except requests.exceptions.Timeout:
        msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
    except Exception as e:
        log_action(user_id, "ADD_TWO_DAYS_FAIL", str(e))
        msg = "❌ حدث خطأ أثناء تنفيذ تزويد اليومين."

    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, msg, InlineKeyboardMarkup(keyboard))


async def query_or_message(update: Update, text: str) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=None)
            return
        except Exception:
            pass
        await update.callback_query.message.reply_text(text, parse_mode=None)
    elif update.message:
        await update.message.reply_text(text, parse_mode=None)



# ================== تنقيص فليكسات ==================
FLEX_DEDUCTION_PER_OPERATION = 225

async def deduct_flexes(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    """بدء واجهة تنقيص الفليكسات للحساب المفتوح حالياً."""
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return

    context.user_data.pop('awaiting_flex_deduction_count', None)
    context.user_data['awaiting_flex_deduction_count'] = True
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(
        update,
        user_id,
        "➖ تنقيص فليكسات\n\n"
        f"كل عملية = {FLEX_DEDUCTION_PER_OPERATION} فليكس.\n"
        "ابعت عدد العمليات المطلوبة (مثال: 3):",
        InlineKeyboardMarkup(keyboard)
    )


async def execute_flex_deduction(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, times: int) -> None:
    operation_lock = get_user_operation_lock(user_id)
    if operation_lock.locked():
        await update.message.reply_text("⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.", parse_mode=None)
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return

    phone = user['phone']
    token = user['access_token']
    purchase_option = 3
    url = "https://web.vodafone.com.eg/services/dxl/promo/promotion/"

    payload = {
        "@type": "Promo",
        "channel": {"id": "5"},
        "context": {"type": "takhmeenPaymentAction"},
        "pattern": [{"characteristics": [{"name": "purchaseOption", "value": purchase_option}]}]
    }
    headers = {
        'User-Agent': 'vodafoneandroid',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
        'Accept-Language': 'AR',
        'msisdn': phone,
        'clientId': 'WebsiteConsumer',
        'client-journey-page': 'takhmeen_home',
        'channel': 'APP_PORTAL',
        'Origin': 'https://web.vodafone.com.eg',
        'X-Requested-With': 'com.emeint.android.myservices',
        'Referer': 'https://web.vodafone.com.eg/portal/afcon/home',
    }

    total = times * FLEX_DEDUCTION_PER_OPERATION
    await update.message.reply_text(
        f"⏳ جاري تنفيذ {times} عملية...\n"
        f"إجمالي التنقيص المتوقع: {total} فليكس.",
        parse_mode=None
    )

    success_count = 0
    failed_status = None

    for i in range(1, times + 1):
        try:
            def sync_request():
                return WEB_HTTP_SESSION.patch(url, json=payload, headers=headers, timeout=30)

            response = await asyncio.to_thread(sync_request)

            if response.status_code == 204:
                success_count += 1
                if i < times:
                    await asyncio.sleep(3)
            else:
                failed_status = response.status_code
                # لا نكرر بلا نهاية داخل البوت؛ محاولة إضافية واحدة بعد 5 ثوانٍ.
                await asyncio.sleep(5)
                response = await asyncio.to_thread(sync_request)
                if response.status_code == 204:
                    success_count += 1
                    if i < times:
                        await asyncio.sleep(3)
                else:
                    failed_status = response.status_code
                    break
        except requests.exceptions.Timeout:
            failed_status = "TIMEOUT"
            break
        except Exception as e:
            log_action(user_id, "FLEX_DEDUCTION_FAIL", str(e))
            failed_status = "ERROR"
            break

    actual = success_count * FLEX_DEDUCTION_PER_OPERATION
    if success_count == times:
        msg = (
            f"✅ تمت العملية بنجاح.\n"
            f"عدد العمليات: {success_count}/{times}\n"
            f"إجمالي التنقيص: {actual} فليكس."
        )
    else:
        msg = (
            f"⚠️ تم تنفيذ {success_count} من {times} عملية.\n"
            f"إجمالي ما تم تنقيصه: {actual} فليكس.\n"
            f"آخر حالة: {failed_status}"
        )

    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)



# ================== خصم فليكس ==================
class FlexDiscountService:
    """استخراج وتفعيل أفضل عرض خصم فليكس باستخدام الجلسة الحالية."""

    def __init__(self, token: str, phone: str):
        self.token = token
        self.phone = phone
        self.session = requests.Session()

    def login_for_discount(self, password: str) -> bool:
        """تسجيل دخول مخصص لخدمة الخصم بنفس إعدادات الملف المصدر."""
        if not password:
            return False
        url = "https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token"
        payload = {
            'grant_type': "password",
            'username': self.phone,
            'password': password,
            'client_secret': "95fd95fb-7489-4958-8ae6-d31a525cd20a",
            'client_id': "ana-vodafone-app"
        }
        headers = {
            'User-Agent': "okhttp/4.12.0",
            'Accept': "application/json, text/plain, */*",
            'Accept-Encoding': "gzip",
            'Content-Type': "application/x-www-form-urlencoded",
            'silentLogin': "true",
            'x-agent-operatingsystem': "15",
            'clientId': "AnaVodafoneAndroid",
            'Accept-Language': "ar",
            'x-agent-device': "Samsung SM-A165F",
            'x-agent-version': "2025.12.2",
            'x-agent-build': "1080",
            'digitalId': "25VT5Q5QWG8DK",
            'device-id': "b26ba335813fad21"
        }
        try:
            response = self.session.post(url, data=payload, headers=headers, timeout=20)
            if response.status_code == 200:
                token = response.json().get('access_token')
                if token:
                    self.token = token
                    return True
        except Exception:
            pass
        return False

    def get_discount_offers(self) -> List[Dict]:
        url = "https://mobile.vodafone.com.eg/services/dxl/epo/eligibleProductOffering"
        params = {
            'customerAccountId': self.phone,
            'parts.customerAccount.type': "Consumer",
            'Accept-Language': "ar",
            'type': "Tarrifs"
        }
        headers = {
            'User-Agent': "okhttp/4.12.0",
            'Connection': "Keep-Alive",
            'Accept': "application/json",
            'Accept-Encoding': "gzip",
            'api-host': "EligibleProductOfferingHost",
            'useCase': "Tarrifs",
            'Authorization': f"Bearer {self.token}",
            'api-version': "v2",
            'device-id': "b26ba335813fad21",
            'x-agent-operatingsystem': "15",
            'clientId': "AnaVodafoneAndroid",
            'x-agent-device': "Samsung SM-A165F",
            'x-agent-version': "2025.12.2",
            'x-agent-build': "1080",
            'msisdn': self.phone,
            'Content-Type': "application/json",
            'Accept-Language': "ar"
        }
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=20)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data
                return []
            logger.warning(f"Flex discount offers failed: status={response.status_code}, body={response.text[:300]}")
        except Exception as e:
            logger.warning(f"Flex discount offers exception: {e}")
        return []

    @staticmethod
    def extract_price_from_desc(description: str) -> Optional[Dict]:
        patterns = [
            r'جدد ب(\d+) بدل (\d+)',
            r'ب(\d+) بدل (\d+)',
            r'بسعر (\d+) بدل (\d+)',
            r'و جدد ب(\d+) بدل (\d+)',
            r'خصم (\d+) بدل (\d+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, description or "")
            if match:
                discounted = int(match.group(1))
                original = int(match.group(2))
                percentage = ((original - discounted) / original * 100) if original > 0 else 0
                return {'original': original, 'discounted': discounted, 'discount_percentage': percentage}
        return None

    @staticmethod
    def _char_value(line_item: Dict, name: str, default: str = "") -> str:
        characteristics = line_item.get('characteristic', {}) or {}
        values = characteristics.get('characteristicsValue', []) or []
        for char in values:
            if isinstance(char, dict) and char.get('characteristicName') == name:
                return str(char.get('value', default))
        return default

    @staticmethod
    def extract_bundle_name(name: str, description: str) -> str:
        for pattern in [r'فليكس (\d+)', r'باقة (\d+)', r'على (\d+)', r'في (\d+)']:
            match = re.search(pattern, description or "")
            if match:
                return f"فليكس {match.group(1)}"
        return name or ("فليكس" if "فليكس" in (description or "") else "الباقة الحالية")

    def extract_all_discount_offers(self, offers_data: List) -> List[Dict]:
        all_offers = []
        for offer_group in offers_data or []:
            if not isinstance(offer_group, dict):
                continue
            products = (offer_group.get('parts', {}) or {}).get('productOffering', []) or []
            for product in products:
                for line_item in product.get('lineItem', []) or []:
                    if not isinstance(line_item, dict):
                        continue
                    offer_type = line_item.get('type', '')
                    description = line_item.get('desc', '')
                    if offer_type not in ('Access fees Discount', 'Usage fees Discount'):
                        continue
                    if not any(k in description for k in ('جدد', 'خصم', 'بدل', 'خليك')):
                        continue
                    price = self.extract_price_from_desc(description)
                    if not price:
                        continue
                    tariff_id = self._char_value(line_item, 'TariffID')
                    product_id = self._char_value(line_item, 'TibcoID')
                    if not tariff_id or not product_id:
                        continue
                    tariff_rank = '1'
                    for cat in line_item.get('category', []) or []:
                        if isinstance(cat, dict) and cat.get('listHierarchyId') == 'TariffRank':
                            tariff_rank = str(cat.get('value', '1'))
                            break
                    offer = {
                        'name': line_item.get('name', ''),
                        'bundle_name': self.extract_bundle_name(line_item.get('name', ''), description),
                        'desc': description,
                        'original_price': price['original'],
                        'discounted_price': price['discounted'],
                        'discount_amount': price['original'] - price['discounted'],
                        'discount_percentage': price['discount_percentage'],
                        'type': offer_type,
                        'tariff_id': tariff_id,
                        'product_id': product_id,
                        'tariff_rank': tariff_rank,
                        'offer_rank': self._char_value(line_item, 'OfferRank', '1'),
                        'cohort_id': self._char_value(line_item, 'CohortId', '30'),
                    }
                    all_offers.append(offer)
        return sorted(all_offers, key=lambda x: x['discount_percentage'], reverse=True)

    def purchase_offer(self, offer: Dict) -> Tuple[bool, str]:
        url = "https://mobile.vodafone.com.eg/services/dxl/pom/productOrder"
        payload = {
            "channel": {"name": "MobileApp"},
            "orderItem": [{
                "action": "add",
                "id": offer['product_id'],
                "itemPrice": [
                    {"name": "OriginalPrice", "price": {"taxIncludedAmount": {"unit": "LE", "value": str(offer['discounted_price'])}}},
                    {"name": "MigrationFees", "price": {"taxIncludedAmount": {"unit": "LE", "value": "0.0"}}}
                ],
                "product": {
                    "characteristic": [
                        {"name": "TariffRank", "value": offer.get('tariff_rank', '1')},
                        {"name": "TariffID", "value": offer['tariff_id']},
                        {"name": "Quota"},
                        {"name": "Validity", "@type": "MONTH", "value": "1"},
                        {"name": "MaxAdjustmentNumber", "value": "1"},
                        {"name": "offerRank", "value": offer.get('offer_rank', '1')},
                        {"name": "MigrationDesc", "value": "Intervention Offer Migration"},
                        {"name": "CohortId", "value": offer.get('cohort_id', '30')}
                    ],
                    "productSpecification": [
                        {"id": "Retention With Offer", "name": "Category"},
                        {"id": "Upon Renewal / Repurchase", "name": "MigrationRule"},
                        {"id": "0", "name": "RatePlanType"},
                        {"id": "Flex Family", "name": "BundleType"}
                    ],
                    "relatedParty": [
                        {"id": self.phone, "name": "MSISDN", "@referredType": "prepaid", "role": "Subscriber"},
                        {"id": offer['tariff_id'], "name": "TariffID", "@referredType": "prepaid", "role": "TariffID"}
                    ]
                },
                "@type": offer.get('type', 'Access fees Discount')
            }],
            "@type": "InterventionTariff"
        }
        headers = {
            'User-Agent': "okhttp/4.12.0",
            'Connection': "Keep-Alive",
            'Accept': "application/json",
            'Accept-Encoding': "gzip",
            'Content-Type': "application/json; charset=UTF-8",
            'api-host': "ProductOrderingManagement",
            'useCase': "InterventionTariff",
            'Authorization': f"Bearer {self.token}",
            'api-version': "v2",
            'device-id': "b26ba335813fad21",
            'x-agent-operatingsystem': "15",
            'clientId': "AnaVodafoneAndroid",
            'x-agent-device': "Samsung SM-A165F",
            'x-agent-version': "2025.12.2",
            'x-agent-build': "1080",
            'msisdn': self.phone,
            'Accept-Language': "ar"
        }
        try:
            response = self.session.post(url, data=json.dumps(payload, ensure_ascii=False), headers=headers, timeout=20)
            try:
                data = response.json()
            except Exception:
                data = {}
            status_value = str(data.get('status', '')).upper() if isinstance(data, dict) else ''
            if response.status_code in (200, 201) or (response.status_code == 400 and data.get('code') == "1008") or status_value == "SUCCESS":
                return True, f"✅ تم تفعيل خصم {offer['discount_amount']} جنيه على {offer['bundle_name']}"
            if data.get('code') == "1001":
                return False, "❌ تمت معالجة الطلب مسبقاً"
            if data.get('code') == "1002":
                return False, "❌ العرض غير متاح حالياً"
            return False, "❌ العرض غير متاح لخطك"
        except requests.exceptions.Timeout:
            return False, "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception:
            return False, "❌ خطأ في الاتصال"


async def flex_discount_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return

    context.user_data.pop('awaiting_flex_deduction_count', None)
    await query_or_message(update, "⏳ جاري البحث عن أفضل عرض خصم فليكس متاح...")

    service = FlexDiscountService(user['access_token'], user['phone'])

    # خدمة الخصم في الملف الأصلي تعمل بتسجيل دخول مستقل بتوكن ana-vodafone-app.
    password = get_saved_password(user_id, user['phone'])
    if password:
        discount_login_ok = await asyncio.to_thread(service.login_for_discount, password)
        if not discount_login_ok:
            keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
            await send_message(
                update, user_id,
                "❌ تعذر تسجيل الدخول لخدمة خصم فليكس.\nجرّب تسجيل الخروج والدخول مرة أخرى لتحديث كلمة المرور المحفوظة.",
                InlineKeyboardMarkup(keyboard)
            )
            return

    try:
        offers = await asyncio.wait_for(asyncio.to_thread(service.get_discount_offers), timeout=25)
        all_offers = await asyncio.to_thread(service.extract_all_discount_offers, offers)
    except asyncio.TimeoutError:
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, "⏰ سيرفر عروض الخصم اتأخر في الرد. جرّب تاني بعد لحظات.", InlineKeyboardMarkup(keyboard))
        return
    except Exception as e:
        logger.exception("Flex discount menu failed")
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, f"❌ حصل خطأ أثناء جلب الخصم: {str(e)[:120]}", InlineKeyboardMarkup(keyboard))
        return

    if not all_offers:
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, "❌ لم أجد عروض خصم فليكس متاحة على الخط حالياً.", InlineKeyboardMarkup(keyboard))
        return

    # خزّن التوكن المخصص للخصم مع العرض حتى نستخدم نفس الجلسة وقت التأكيد.
    context.user_data['flex_discount_token'] = service.token

    best = all_offers[0]
    context.user_data['flex_discount_offer'] = best
    text = (
        "💰 عرض خصم فليكس متاح\n\n"
        f"📦 الباقة: {best['bundle_name']}\n"
        f"💰 السعر الأصلي: {best['original_price']} جنيه\n"
        f"💸 سعر الخصم: {best['discounted_price']} جنيه\n"
        f"✨ نسبة الخصم: {best['discount_percentage']:.1f}%\n"
        f"📝 الوصف: {best['desc']}\n\n"
        "⚠️ هل تريد تفعيل العرض؟"
    )
    keyboard = [
        [
            InlineKeyboardButton("✅ تأكيد التفعيل", callback_data="flex_discount_confirm"),
            InlineKeyboardButton("❌ إلغاء", callback_data="flex_discount_cancel")
        ],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))


async def flex_discount_confirm(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    offer = context.user_data.get('flex_discount_offer')
    if not offer:
        await query_or_message(update, "⚠️ بيانات العرض انتهت. افتح خصم فليكس من جديد.")
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        return

    lock = get_user_operation_lock(user_id)
    if lock.locked():
        await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.")
        return

    async with lock:
        await query_or_message(update, "⏳ جاري تفعيل عرض خصم فليكس...")
        discount_token = context.user_data.get('flex_discount_token') or user['access_token']
        service = FlexDiscountService(discount_token, user['phone'])
        success, message = await asyncio.to_thread(service.purchase_offer, offer)

    context.user_data.pop('flex_discount_offer', None)
    context.user_data.pop('flex_discount_token', None)
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, message, InlineKeyboardMarkup(keyboard))



# ================== خدمات الباقات والتحويل ==================
FLEX_BUNDLES = [
    {"number": "1", "name": "فليكس 40", "id": "Flex_2021_511", "price": 40},
    {"number": "2", "name": "فليكس 45", "id": "Flex_2024_627", "price": 45},
    {"number": "3", "name": "فليكس 60", "id": "Flex_2021_513", "price": 60},
    {"number": "4", "name": "فليكس 70", "id": "Flex_2024_629", "price": 70},
    {"number": "5", "name": "فليكس 90", "id": "Flex_2021_515", "price": 90},
    {"number": "6", "name": "فليكس 100", "id": "Flex_2024_631", "price": 100},
    {"number": "7", "name": "فليكس 130", "id": "Flex_2021_517", "price": 130},
    {"number": "8", "name": "فليكس 150", "id": "Flex_2024_633", "price": 150},
    {"number": "9", "name": "فليكس 260", "id": "Flex_2021_523", "price": 260},
    {"number": "10", "name": "فليكس 300", "id": "Flex_2024_637", "price": 300},
]

FLEX_ACP_ENC_PRODUCT_ID = "SBWbw/gsvm1cU1nPBj7HCg6MNEaAfyY56Kxz53nXBwpe6Z4c2t1DgiO2OM2hZwGVJaztwhZu7DWZiE2Ic5evFLqZfV/QaAOWQcS3m8bZCVD/wmRvbEvtfv16FTwgzWMjUQErPqXuYIMnePuK3H+MwQ8iFKqpvQ1d7qrPz05JlpUXKn2GM14uKA=="

def get_flex_bundle_by_number(number: str) -> Optional[Dict[str, Any]]:
    return next((b for b in FLEX_BUNDLES if b['number'] == str(number)), None)

async def packages_services_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return
    keyboard = [
        [InlineKeyboardButton("⚡ باقات فليكس", callback_data="packages_flex")],
        [InlineKeyboardButton("🔄 تحويل 14 قرش", callback_data="packages_convert14")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    await query_or_message(update, "🔄 تحويل انظمه\n\nاختر الخدمة المطلوبة:")
    await send_message(update, user_id, "اختر من الأزرار:", InlineKeyboardMarkup(keyboard))

async def flex_bundles_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    bundle_2021 = [b for b in FLEX_BUNDLES if "2021" in b['id']]
    bundle_2024 = [b for b in FLEX_BUNDLES if "2024" in b['id']]
    keyboard = [[
        InlineKeyboardButton("⚡ نظام 2021", callback_data="packages_ignore"),
        InlineKeyboardButton("🆕 نظام 2024", callback_data="packages_ignore"),
    ]]
    for i in range(max(len(bundle_2021), len(bundle_2024))):
        row = []
        if i < len(bundle_2021):
            b = bundle_2021[i]
            row.append(InlineKeyboardButton(f"{b['name']} - {b['price']}ج", callback_data=f"packages_flex_{b['number']}"))
        if i < len(bundle_2024):
            b = bundle_2024[i]
            row.append(InlineKeyboardButton(f"{b['name']} - {b['price']}ج", callback_data=f"packages_flex_{b['number']}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")])
    await query_or_message(update, "⚡ باقات فليكس\n\nاختر الباقة المناسبة:")
    await send_message(update, user_id, "نظام 2021 ونظام 2024:", InlineKeyboardMarkup(keyboard))

async def flex_bundle_confirm_screen(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, bundle_number: str) -> None:
    bundle = get_flex_bundle_by_number(bundle_number)
    user = await ensure_valid_session(update, user_id)
    if not user or not bundle:
        await query_or_message(update, "❌ حدث خطأ في اختيار الباقة.")
        return
    context.user_data['packages_pending_flex'] = bundle_number
    keyboard = [
        [InlineKeyboardButton("✅ تأكيد التفعيل", callback_data=f"packages_flex_confirm_{bundle_number}"),
         InlineKeyboardButton("❌ إلغاء", callback_data="packages_services")],
    ]
    text = (f"⚡ تأكيد تفعيل الباقة\n\n📱 الرقم: {user['phone']}\n"
            f"📦 الباقة: {bundle['name']}\n💰 السعر: {bundle['price']} جنيه\n\nهل تريد تفعيل هذه الباقة؟")
    await query_or_message(update, text)
    await send_message(update, user_id, "اضغط تأكيد للتنفيذ:", InlineKeyboardMarkup(keyboard))

async def execute_flex_bundle_service(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, bundle_number: str) -> None:
    bundle = get_flex_bundle_by_number(bundle_number)
    user = await ensure_valid_session(update, user_id)
    if not user or not bundle:
        await query_or_message(update, "❌ بيانات الباقة غير متاحة.")
        return
    lock = get_user_operation_lock(user_id)
    if lock.locked():
        await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.")
        return
    async with lock:
        await query_or_message(update, f"⏳ جاري تفعيل {bundle['name']}...")
        payload = {
            "channel": {"name": "MobileApp"},
            "orderItem": [{"action": "insert", "id": bundle['id'], "product": {
                "characteristic": [
                    {"name": "PaymentMethod", "value": "ACP"}, {"name": "ACP", "value": "True"},
                    {"name": "SMSID", "value": "MUTE_SMS"}, {"name": "LangId", "value": "en"},
                    {"name": "ExecutionType", "value": "Sync"}],
                "encProductId": FLEX_ACP_ENC_PRODUCT_ID, "id": bundle['id'],
                "relatedParty": [{"id": user['phone'], "name": "MSISDN", "role": "Subscriber"}]}, "eCode": 0}],
            "@type": "FlexACPRenewal"
        }
        headers = build_base_headers({
            'api-host': 'ProductOrderingManagement', 'useCase': 'FlexACPRenewal',
            'Authorization': f"Bearer {user['access_token']}", 'api-version': 'v2', 'msisdn': user['phone']
        })
        def req():
            return HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/pom/productOrder', headers=headers, json=payload, timeout=30)
        try:
            response = await asyncio.to_thread(req)
            try: result = response.json()
            except Exception: result = {}
            code = str(result.get('code', '')) if isinstance(result, dict) else ''
            if response.status_code in (200, 201) or code == '3999':
                msg = f"✅ تم تحويل {bundle['name']} بنجاح!\n\n📱 الرقم: {user['phone']}\n💰 السعر: {bundle['price']} جنيه"
                if code == '3999': msg += "\n\n✅ برجاء التحقق من اشتراكاتك للتأكيد"
            else:
                reason = result.get('reason', 'الطلب غير متاح حالياً') if isinstance(result, dict) else 'الطلب غير متاح حالياً'
                msg = f"❌ فشل تحويل {bundle['name']}\nكود الحالة: {response.status_code}\nالسبب: {reason}"
        except requests.exceptions.Timeout:
            msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e:
            log_action(user_id, 'FLEX_BUNDLE_FAIL', str(e))
            msg = "❌ حدث خطأ أثناء تفعيل الباقة."
    context.user_data.pop('packages_pending_flex', None)
    await send_message(update, user_id, msg, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")]]))

async def convert14_confirm_screen(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    context.user_data['packages_convert14_confirm'] = True
    keyboard = [[InlineKeyboardButton("✅ تأكيد التحويل", callback_data="packages_convert14_confirm"),
                 InlineKeyboardButton("❌ إلغاء", callback_data="packages_services")]]
    text = f"🔄 تحويل 14 قرش\n\n📱 الرقم: {user['phone']}\n📦 الباقة: ريح بالك كله ب14 قرش\n\n⚠️ هل تريد تنفيذ عملية التحويل؟"
    await query_or_message(update, text)
    await send_message(update, user_id, "اضغط تأكيد للتنفيذ:", InlineKeyboardMarkup(keyboard))

async def execute_convert14_service(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get('packages_convert14_confirm'):
        await query_or_message(update, "⚠️ بيانات العملية انتهت. افتح الخدمة من جديد.")
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    lock = get_user_operation_lock(user_id)
    if lock.locked():
        await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.")
        return
    async with lock:
        await query_or_message(update, "⏳ جاري تحويل الرقم إلى باقة 14 قرش...")
        phone, token = user['phone'], user['access_token']
        params = {'customerAccountId': phone, 'parts.customerAccount.type': 'Consumer', 'Accept-Language': 'ar', 'type': 'Tarrifs'}
        headers = build_base_headers({'api-host': 'EligibleProductOfferingHost', 'useCase': 'Tarrifs', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone})
        def get_offer():
            return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/epo/eligibleProductOffering', params=params, headers=headers, timeout=30)
        try:
            r = await asyncio.to_thread(get_offer)
            if r.status_code != 200:
                raise RuntimeError(f'EPO status {r.status_code}')
            data = r.json()
            target_name = 'ريح بالك كله ب14 قرش'
            enc_product_id = tibco_id = tariff_id = None
            rank = None
            for item in data if isinstance(data, list) else []:
                for product in (item.get('parts', {}) or {}).get('productOffering', []) or []:
                    if product.get('name') != target_name:
                        continue
                    for id_item in product.get('id', []) or []:
                        if id_item.get('schemeName') == 'EncProductID': enc_product_id = id_item.get('value')
                        elif id_item.get('schemeID') == 'ProductID': tibco_id = id_item.get('value')
                        elif id_item.get('schemeID') == 'TariffID': tariff_id = id_item.get('value')
                    for ch in (product.get('specification', {}) or {}).get('characteristicsValue', []) or []:
                        if ch.get('characteristicName') == 'Rank': rank = ch.get('value')
                    break
                if enc_product_id: break
            if not enc_product_id:
                msg = "❌ باقة ريح بالك كله ب14 قرش غير متاحة على الخط حالياً."
            else:
                payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "add", "id": tibco_id,
                    "itemPrice": [{"name": "OriginalPrice", "price": {"taxIncludedAmount": {"unit": "LE", "value": "0"}}}],
                    "product": {"characteristic": [{"name": "TariffRank", "value": str(rank or '1')}, {"name": "TariffID", "value": tariff_id or ''},
                        {"name": "Quota", "value": ""}, {"name": "Validity", "@type": "MONTH", "value": ""}, {"name": "InterventionFlag", "value": "0"}, {"name": "DestNoOfSeats", "value": ""}],
                        "encProductId": enc_product_id, "productSpecification": [{"id": "ConsumerType", "name": "Category"}, {"id": "0", "name": "RatePlanType"}, {"id": "Other", "name": "BundleType"}],
                        "relatedParty": [{"id": phone, "name": "MSISDN", "@referredType": "prepaid", "role": "Subscriber"}, {"id": tariff_id or '470', "name": "TariffID", "@referredType": "prepaid", "role": "TariffID"}]}, "eCode": 0}], "@type": "Tariff"}
                post_headers = build_base_headers({'api-host': 'ProductOrderingManagement', 'useCase': 'Tariff', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone})
                def post_order():
                    return HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/pom/productOrder', headers=post_headers, json=payload, timeout=30)
                rp = await asyncio.to_thread(post_order)
                if rp.status_code in (200, 201, 400):
                    msg = f"✅ تم إرسال طلب التحويل إلى باقة 14 قرش بنجاح!\n\n📱 الرقم: {phone}\n📦 الباقة: {target_name}"
                else:
                    msg = f"❌ فشل التحويل - كود الخادم: {rp.status_code}"
        except requests.exceptions.Timeout:
            msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e:
            log_action(user_id, 'CONVERT14_FAIL', str(e))
            msg = "❌ فشل في جلب أو تنفيذ تحويل 14 قرش."
    context.user_data.pop('packages_convert14_confirm', None)
    await send_message(update, user_id, msg, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")]]))

# ================== القائمة الرئيسية (معدلة للتحقق من الاشتراك) ==================
async def show_main_menu(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    # التحقق من الاشتراك (يتم استدعاؤها قبل عرض القائمة)
    if not await require_subscription_or_free(update, context, is_callback=False):
        return
    await clear_previous_messages(update, user_id)
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return
    if context and context.user_data.get('rocket_members') is None:
        context.user_data['rocket_members'] = []
    text = "🏠 القائمة الرئيسية\nاختر أحد الخيارات:"
    keyboard = [
        [
            InlineKeyboardButton("🏠 إدارة عائلة فليكس 260", callback_data="show_family"),
            InlineKeyboardButton("📅 تزويد يومين", callback_data="add_two_days")
        ],
        [
            InlineKeyboardButton("➖ تنقيص فليكسات", callback_data="deduct_flexes"),
            InlineKeyboardButton("💰 خصم فليكس", callback_data="flex_discount")
        ],
        [
            InlineKeyboardButton("📊 نسبة الأونر", callback_data="show_owner_remaining"),
            InlineKeyboardButton("📊 استهلاك الأعضاء", callback_data="show_consumption")
        ],
        [
            InlineKeyboardButton("📞 أرقامي المحفوظة", callback_data="show_saved"),
            InlineKeyboardButton("🧹 تنظيف العائلة", callback_data="clean_family")
        ],
        [InlineKeyboardButton("🔄 تحويل انظمه", callback_data="packages_services")],
        [
            InlineKeyboardButton("📨 تعليق دعوتين", callback_data="sendonly_menu"),
            InlineKeyboardButton("🔄 حساب آخر", callback_data="another_account")
        ]
    ]
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))

# ================== إدارة العائلة ==================
async def show_family_screen(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    # تحديد إذا كان هناك callback_query
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return
    await clear_previous_messages(update, user_id)
    try:
        members = await get_family_members(user['access_token'], user['phone'])
    except Exception as e:
        await send_message(update, user_id, f"خطأ: {str(e)}")
        return
    active = members.get('active', [])
    pending = members.get('pending', [])
    active_phones = [a['phone'] for a in active]
    filtered_pending = [p for p in pending if p['phone'] not in active_phones]
    MAX_ACTIVE = 2
    rocket_list = context.user_data.get('rocket_members', []) if context else []
    for m in active:
        phone_display = m['phone'] + (" 🚀" if m['phone'] in rocket_list else "")
        info = f"📞 الفرد: {phone_display}\n✅ مفعل\n📊 النسبة: {m['flex']} فلكس\n✏️ تعديل نسبة الفرد"
        others = [f for f in ["1300","2600","5200"] if f != m['flex']]
        keyboard = [[InlineKeyboardButton(f"فلكس {f}", callback_data=f"edit_{m['phone']}_{f}") for f in others],
                    [InlineKeyboardButton("🗑️ حذف الفرد", callback_data=f"del_{m['phone']}")]]
        await send_message(update, user_id, info, InlineKeyboardMarkup(keyboard))
    for p in filtered_pending:
        phone_display = p['phone'] + (" 🚀" if p['count'] >= 2 else "")
        info = f"📞 الفرد: {phone_display}\n⏳ بانتظار القبول\n📊 النسبة: {p['flex']} فلكس"
        keyboard = [
            [InlineKeyboardButton("قبول الدعوة ✅", callback_data=f"accept_{p['phone']}"),
             InlineKeyboardButton("إلغاء الدعوة ❌", callback_data=f"cancel_{p['phone']}")],
            [InlineKeyboardButton("🗑️ حذف الفرد", callback_data=f"del_{p['phone']}")]
        ]
        await send_message(update, user_id, info, InlineKeyboardMarkup(keyboard))
    if len(active) < MAX_ACTIVE:
        general_text = "📝 دعوة فرد جديد: اختر نسبة من الأزرار أدناه 📞"
        general_keyboard = [
            [InlineKeyboardButton("فلكس 5200", callback_data="invite_40"),
             InlineKeyboardButton("فلكس 2600", callback_data="invite_20"),
             InlineKeyboardButton("فلكس 1300", callback_data="invite_10")],
            [InlineKeyboardButton("🔄 تحديث", callback_data="refresh_family")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
        ]
        await send_message(update, user_id, general_text, InlineKeyboardMarkup(general_keyboard))
    else:
        general_keyboard = [
            [InlineKeyboardButton("🔄 تحديث", callback_data="refresh_family")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
        ]
        await send_message(update, user_id, "العائلة ممتلئة (2 أعضاء). لا يمكن إضافة المزيد.", InlineKeyboardMarkup(general_keyboard))

async def show_owner_remaining(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return
    await send_message(update, user_id, "📊 جاري جلب النسبة المتبقية...")
    try:
        remaining = await get_owner_remaining(user['access_token'], user['phone'])
        if remaining:
            text = f"📊 نسبة الأونر المتبقية:\n{remaining}"
        else:
            text = "❌ لم نتمكن من جلب النسبة المتبقية."
    except Exception as e:
        text = f"❌ خطأ: {str(e)}"
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))

async def show_consumption(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return
    try:
        members = await get_family_members(user['access_token'], user['phone'])
        active_members = members.get('active', [])
        if not active_members:
            await send_message(update, user_id, "ℹ️ لا يوجد أعضاء نشطون في العائلة لعرض استهلاكهم.")
            return
    except Exception as e:
        await send_message(update, user_id, f"خطأ: {str(e)}")
        return
    await send_message(update, user_id, "📊 جاري جلب استهلاك الأعضاء...")
    results = []
    for member in active_members:
        number = member['phone']
        consumption = await get_member_consumption_from_owner(user['access_token'], user['phone'], number)
        if consumption:
            results.append(f"📞 {number}\n✅ المتبقي: {consumption['remaining']}\n📉 المستخدم: {consumption['used']}")
        else:
            results.append(f"📞 {number}\n❌ لم نتمكن من جلب استهلاكه (قد لا يكون مفعلاً أو هناك خطأ).")
    if results:
        text = "📊 استهلاك الفليكسات للأعضاء:\n\n" + "\n\n".join(results)
    else:
        text = "❌ حدث خطأ أثناء جلب الاستهلاك."
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))

async def show_saved_numbers(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    numbers = get_saved_numbers(user_id)
    if not numbers:
        text = "📭 لا توجد أرقام محفوظة. يمكنك إضافة رقم باستخدام /savenumber <رقم>"
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))
        return
    keyboard = []
    for num in numbers:
        token_valid = get_valid_token(user_id, num) is not None
        display_num = num + (" 🔑" if token_valid else "")
        keyboard.append([InlineKeyboardButton(display_num, callback_data=f"use_num_{num}"),
                         InlineKeyboardButton("🗑 حذف", callback_data=f"del_num_{num}")])
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    await send_message(update, user_id, "📞 أرقامي المحفوظة (🔑=توكن صالح):\nاختر رقم لتسجيل الدخول به أو حذفه:", InlineKeyboardMarkup(keyboard))

# ================== دوال تعليق دعوتين (Send Only) ==================
sendonly_stats = {
    "total_success": 0,
    "total_attempts": 0,
    "user_logs": {}
}
SENDONLY_STATS_FILE = "sendonly_stats.json"

def load_sendonly_stats():
    global sendonly_stats
    if os.path.exists(SENDONLY_STATS_FILE):
        with open(SENDONLY_STATS_FILE, "r", encoding="utf-8") as f:
            sendonly_stats = json.load(f)

def save_sendonly_stats():
    with open(SENDONLY_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(sendonly_stats, f, indent=4, ensure_ascii=False)

load_sendonly_stats()

# حالات محادثة تعليق دعوتين
SENDONLY_SELECT_OWNER, SENDONLY_OWNER_PASSWORD, SENDONLY_OWNER_NUM, SENDONLY_OWNER_PASS, SENDONLY_MEMBER_NUM, SENDONLY_PERCENTAGE, SENDONLY_ATTEMPTS = range(20, 27)

# دوال الحصول على توكنات متعددة (للمالك)
def get_authorization_sendonly(number: str, password: str) -> dict:
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/x-www-form-urlencoded',
        'silentLogin': 'true',
        'x-agent-operatingsystem': '13',
        'clientId': 'AnaVodafoneAndroid',
        'Accept-Language': 'ar',
        'x-agent-device': 'Samsung SM-A515F',
        'x-agent-version': '2026.2.3',
        'x-agent-build': '1117',
        'digitalId': generate_digital_id(),
        'device-id': generate_device_id(),
    }
    data = {
        'grant_type': 'password',
        'username': number,
        'password': password,
        'client_secret': '95fd95fb-7489-4958-8ae6-d31a525cd20a',
        'client_id': 'ana-vodafone-app',
    }
    try:
        resp = HTTP_SESSION.post(
            'https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token',
            headers=headers, data=data, timeout=10
        )
        if resp.status_code == 200:
            token = resp.json().get('access_token')
            if token:
                return {"status": "success", "token": "Bearer " + token}
        return {"status": "error", "message": f"كود {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def get_multiple_tokens_sendonly(owner_number: str, owner_password: str, count: int, stop_event: asyncio.Event) -> List[str]:
    tokens = []
    def login_task(idx):
        if stop_event.is_set():
            return
        result = get_authorization_sendonly(owner_number, owner_password)
        if result["status"] == "success":
            tokens.append(result["token"])
    threads = []
    for i in range(count):
        if stop_event.is_set():
            break
        t = threading.Thread(target=login_task, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=30)
    return tokens

# ------------------- دوال الإرسال بالطريقة الأولى (aiohttp) -------------------
async def send_invitation_mobile_aiohttp(session: aiohttp.ClientSession, owner_number: str, member_number: str, token: str, percentage: int) -> dict:
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Connection': 'Keep-Alive',
        'Accept': 'application/json',
        'Authorization': token,
        'api-version': 'v2',
        'device-id': generate_device_id(),
        'x-agent-operatingsystem': '13',
        'clientId': 'AnaVodafoneAndroid',
        'x-agent-device': 'Samsung SM-A515F',
        'x-agent-version': '2026.2.3',
        'x-agent-build': '1117',
        'msisdn': owner_number,
        'Accept-Language': 'ar',
        'Content-Type': 'application/json; charset=UTF-8',
    }
    json_data = {
        'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}],
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}],
        },
        'type': 'SendInvitation',
    }
    try:
        async with session.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=10) as resp:
            status = resp.status
            return {"success": status in (200, 201), "status_code": status, "source": "mobile"}
    except Exception as e:
        return {"success": False, "status_code": 0, "source": "mobile", "error": str(e)}

async def send_invitation_web_aiohttp(session: aiohttp.ClientSession, owner_number: str, member_number: str, token: str, percentage: int) -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'msisdn': owner_number,
        'Accept-Language': 'AR',
        'sec-ch-ua-mobile': '?1',
        'Authorization': token,
        'clientId': 'WebsiteConsumer',
        'sec-ch-ua-platform': '"Android"',
        'Origin': 'https://web.vodafone.com.eg',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'Referer': 'https://web.vodafone.com.eg/spa/familySharing',
    }
    json_data = {
        'name': 'FlexFamily',
        'type': 'SendInvitation',
        'category': [
            {'value': '523', 'listHierarchyId': 'PackageID'},
            {'value': '47', 'listHierarchyId': 'TemplateID'},
            {'value': '523', 'listHierarchyId': 'TierID'},
            {'value': 'percentage', 'listHierarchyId': 'familybehavior'},
        ],
        'parts': {
            'member': [
                {'id': [{'value': owner_number, 'schemeName': 'MSISDN'}], 'type': 'Owner'},
                {'id': [{'value': member_number, 'schemeName': 'MSISDN'}], 'type': 'Member'},
            ],
            'characteristicsValue': {
                'characteristicsValue': [
                    {'characteristicName': 'quotaDist1', 'value': str(percentage), 'type': 'percentage'},
                ],
            },
        },
    }
    try:
        async with session.post('https://web.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=10) as resp:
            status = resp.status
            return {"success": status in (200, 201), "status_code": status, "source": "web"}
    except Exception as e:
        return {"success": False, "status_code": 0, "source": "web", "error": str(e)}

async def send_four_invites_aiohttp(owner_number: str, member_number: str, tokens: List[str], percentage: int) -> List[dict]:
    async with aiohttp.ClientSession() as session:
        tasks = []
        for i, tok in enumerate(tokens[:2]):
            tasks.append(send_invitation_mobile_aiohttp(session, owner_number, member_number, tok, percentage))
        for i, tok in enumerate(tokens[2:4]):
            tasks.append(send_invitation_web_aiohttp(session, owner_number, member_number, tok, percentage))
        while len(tasks) < 4 and tokens:
            tasks.append(send_invitation_mobile_aiohttp(session, owner_number, member_number, tokens[-1], percentage))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        final = []
        for r in results:
            if isinstance(r, Exception):
                final.append({"success": False, "status_code": 0, "error": str(r)})
            else:
                final.append(r)
        return final

# ------------------- دوال الإرسال بالطريقة الثانية (threading + requests) -------------------
def send_invitation_mobile_thread(owner_number: str, member_number: str, token: str, percentage: int) -> dict:
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Connection': 'Keep-Alive',
        'Accept': 'application/json',
        'Authorization': token,
        'api-version': 'v2',
        'device-id': generate_device_id(),
        'x-agent-operatingsystem': '13',
        'clientId': 'AnaVodafoneAndroid',
        'x-agent-device': 'Samsung SM-A515F',
        'x-agent-version': '2026.2.3',
        'x-agent-build': '1117',
        'msisdn': owner_number,
        'Accept-Language': 'ar',
        'Content-Type': 'application/json; charset=UTF-8',
    }
    json_data = {
        'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}],
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}],
        },
        'type': 'SendInvitation',
    }
    try:
        resp = HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=10)
        return {"success": resp.status_code in (200, 201), "status_code": resp.status_code, "source": "mobile"}
    except Exception as e:
        return {"success": False, "status_code": 0, "source": "mobile", "error": str(e)}

def send_invitation_web_thread(owner_number: str, member_number: str, token: str, percentage: int) -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'msisdn': owner_number,
        'Accept-Language': 'AR',
        'sec-ch-ua-mobile': '?1',
        'Authorization': token,
        'clientId': 'WebsiteConsumer',
        'sec-ch-ua-platform': '"Android"',
        'Origin': 'https://web.vodafone.com.eg',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'Referer': 'https://web.vodafone.com.eg/spa/familySharing',
    }
    json_data = {
        'name': 'FlexFamily',
        'type': 'SendInvitation',
        'category': [
            {'value': '523', 'listHierarchyId': 'PackageID'},
            {'value': '47', 'listHierarchyId': 'TemplateID'},
            {'value': '523', 'listHierarchyId': 'TierID'},
            {'value': 'percentage', 'listHierarchyId': 'familybehavior'},
        ],
        'parts': {
            'member': [
                {'id': [{'value': owner_number, 'schemeName': 'MSISDN'}], 'type': 'Owner'},
                {'id': [{'value': member_number, 'schemeName': 'MSISDN'}], 'type': 'Member'},
            ],
            'characteristicsValue': {
                'characteristicsValue': [
                    {'characteristicName': 'quotaDist1', 'value': str(percentage), 'type': 'percentage'},
                ],
            },
        },
    }
    try:
        resp = HTTP_SESSION.post('https://web.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=10)
        return {"success": resp.status_code in (200, 201), "status_code": resp.status_code, "source": "web"}
    except Exception as e:
        return {"success": False, "status_code": 0, "source": "web", "error": str(e)}

def send_four_invites_thread(owner_number: str, member_number: str, tokens: List[str], percentage: int) -> List[dict]:
    results = [None] * 4
    def task(idx, func, token):
        results[idx] = func(owner_number, member_number, token, percentage)
    threads = []
    for i in range(min(2, len(tokens))):
        t = threading.Thread(target=task, args=(i, send_invitation_mobile_thread, tokens[i]))
        threads.append(t)
        t.start()
    for i in range(2, min(4, len(tokens))):
        t = threading.Thread(target=task, args=(i, send_invitation_web_thread, tokens[i]))
        threads.append(t)
        t.start()
    for i in range(len(tokens), 4):
        if tokens:
            t = threading.Thread(target=task, args=(i, send_invitation_mobile_thread, tokens[-1]))
            threads.append(t)
            t.start()
    for t in threads:
        t.join(timeout=30)
    for i in range(4):
        if results[i] is None:
            results[i] = {"success": False, "status_code": 0, "error": "لم يكتمل"}
    return results

async def send_four_invites(owner_number: str, member_number: str, tokens: List[str], percentage: int) -> List[dict]:
    global SEND_METHOD
    if SEND_METHOD == 1:
        return await send_four_invites_aiohttp(owner_number, member_number, tokens, percentage)
    else:
        return await asyncio.to_thread(send_four_invites_thread, owner_number, member_number, tokens, percentage)

# دالة حذف العضو في تعليق دعوتين
def remove_member_sendonly(owner_number: str, token: str, member_number: str) -> bool:
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Connection': 'Keep-Alive',
        'Accept': 'application/json',
        'Authorization': token,
        'api-version': 'v2',
        'device-id': generate_device_id(),
        'x-agent-operatingsystem': '13',
        'clientId': 'AnaVodafoneAndroid',
        'x-agent-device': 'Samsung SM-A515F',
        'x-agent-version': '2026.2.3',
        'x-agent-build': '1117',
        'msisdn': owner_number,
        'Accept-Language': 'ar',
        'Content-Type': 'application/json; charset=UTF-8',
    }
    payload = {
        'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}],
        'createdBy': {'value': 'MobileApp'},
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'Disconnect', 'value': '0'}, {'characteristicName': 'LastMemberDeletion', 'value': '1'}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}],
        },
        'type': 'FamilyRemoveMember',
    }
    try:
        resp = HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=payload, timeout=10)
        return resp.status_code in (200, 201)
    except:
        return False

# ================== واجهة تعليق دعوتين ==================
async def sendonly_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    # التحقق من الاشتراك
    if not await require_subscription_or_free(update, context, is_callback=False):
        return ConversationHandler.END
    keyboard = []
    current = get_user(user_id)
    if current:
        keyboard.append([InlineKeyboardButton(f"📱 استخدام الجلسة الحالية: {current['phone']}", callback_data="sendonly_use_current")])
    saved = get_saved_numbers(user_id)
    for num in saved:
        if not current or num != current['phone']:
            token_valid = get_valid_token(user_id, num) is not None
            display = num + (" 🔑" if token_valid else "")
            keyboard.append([InlineKeyboardButton(display, callback_data=f"sendonly_owner_{num}")])
    keyboard.append([InlineKeyboardButton("➕ إدخال رقم جديد", callback_data="sendonly_manual")])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("📨 تعليق دعوتين\n\nاختر رقم المالك:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    else:
        await update.message.reply_text("📨 تعليق دعوتين\n\nاختر رقم المالك:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    return SENDONLY_SELECT_OWNER

async def sendonly_select_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    if data == "sendonly_use_current":
        current = get_user(user_id)
        if current:
            context.user_data['sendonly_owner'] = current['phone']
            saved_pass = get_saved_password(user_id, current['phone'])
            if saved_pass:
                context.user_data['sendonly_owner_password'] = saved_pass
                await query.edit_message_text(f"✅ تم استخدام الجلسة الحالية للرقم {current['phone']}\n📱 أرسل رقم العضو:", parse_mode=None)
                return SENDONLY_MEMBER_NUM
            else:
                await query.edit_message_text(f"🔐 لا توجد كلمة مرور مخزنة للرقم {current['phone']}\nأدخل كلمة المرور:", parse_mode=None)
                return SENDONLY_OWNER_PASSWORD
        else:
            await query.edit_message_text("❌ لا توجد جلسة نشطة.", parse_mode=None)
            return await sendonly_menu(update, context)
    
    elif data.startswith("sendonly_owner_"):
        phone = data[16:]
        context.user_data['sendonly_owner'] = phone
        saved_pass = get_saved_password(user_id, phone)
        if saved_pass:
            context.user_data['sendonly_owner_password'] = saved_pass
            await query.edit_message_text(f"✅ تم اختيار الرقم {phone}\n📱 أرسل رقم العضو:", parse_mode=None)
            return SENDONLY_MEMBER_NUM
        else:
            await query.edit_message_text(f"🔐 أدخل كلمة مرور الرقم {phone}:", parse_mode=None)
            return SENDONLY_OWNER_PASSWORD
    
    elif data == "sendonly_manual":
        await query.edit_message_text("📱 أرسل رقم المالك (مثال: 01012345678):", parse_mode=None)
        return SENDONLY_OWNER_NUM
    
    return SENDONLY_SELECT_OWNER

async def sendonly_owner_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    owner = context.user_data['sendonly_owner']
    user_id = update.effective_user.id
    result = get_authorization_sendonly(owner, password)
    if result["status"] != "success":
        await update.message.reply_text(f"❌ فشل تسجيل الدخول: {result.get('message')}\nحاول مرة أخرى أو اختر رقماً آخر.", parse_mode=None)
        return await sendonly_menu(update, context)
    save_password(user_id, owner, password, 24)
    context.user_data['sendonly_owner_password'] = password
    await update.message.reply_text("✅ تم تسجيل الدخول.\n📱 الآن أرسل رقم العضو:", parse_mode=None)
    return SENDONLY_MEMBER_NUM

async def sendonly_owner_num_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    valid, msg = validate_phone(text)
    if not valid:
        await update.message.reply_text(f"{msg}\nأعد إرسال رقم المالك:", parse_mode=None)
        return SENDONLY_OWNER_NUM
    context.user_data['sendonly_owner'] = text
    await update.message.reply_text("🔑 أدخل كلمة مرور المالك:", parse_mode=None)
    return SENDONLY_OWNER_PASS

async def sendonly_owner_pass_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    owner = context.user_data['sendonly_owner']
    user_id = update.effective_user.id
    result = get_authorization_sendonly(owner, password)
    if result["status"] != "success":
        await update.message.reply_text(f"❌ فشل تسجيل الدخول: {result.get('message')}\nحاول مرة أخرى.", parse_mode=None)
        return ConversationHandler.END
    save_password(user_id, owner, password, 24)
    context.user_data['sendonly_owner_password'] = password
    await update.message.reply_text("✅ تم تسجيل الدخول.\n📱 الآن أرسل رقم العضو:", parse_mode=None)
    return SENDONLY_MEMBER_NUM

async def sendonly_member_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    valid, msg = validate_phone(text)
    if not valid:
        await update.message.reply_text(f"{msg}\nأعد إرسال رقم العضو:", parse_mode=None)
        return SENDONLY_MEMBER_NUM
    owner = context.user_data.get('sendonly_owner')
    if text == owner:
        await update.message.reply_text("❌ رقم العضو لا يمكن أن يساوي رقم المالك. أرسل رقماً آخر:", parse_mode=None)
        return SENDONLY_MEMBER_NUM
    context.user_data['sendonly_member'] = text
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("10%", callback_data="sendonly_perc_10")],
        [InlineKeyboardButton("20%", callback_data="sendonly_perc_20")],
        [InlineKeyboardButton("40%", callback_data="sendonly_perc_40")]
    ])
    await update.message.reply_text("📊 اختر نسبة الدعوة:", reply_markup=keyboard, parse_mode=None)
    return SENDONLY_PERCENTAGE

async def sendonly_percentage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "sendonly_perc_10":
        context.user_data['sendonly_percentage'] = 10
    elif data == "sendonly_perc_20":
        context.user_data['sendonly_percentage'] = 20
    else:
        context.user_data['sendonly_percentage'] = 40
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="sendonly_att_1")],
        [InlineKeyboardButton("3", callback_data="sendonly_att_3")],
        [InlineKeyboardButton("5", callback_data="sendonly_att_5")]
    ])
    await query.edit_message_text("🔄 اختر عدد المحاولات:", reply_markup=keyboard, parse_mode=None)
    return SENDONLY_ATTEMPTS

async def sendonly_attempts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "sendonly_att_1":
        attempts = 1
    elif data == "sendonly_att_3":
        attempts = 3
    else:
        attempts = 5
    context.user_data['sendonly_attempts'] = attempts

    owner_number = context.user_data['sendonly_owner']
    owner_password = context.user_data.get('sendonly_owner_password')
    member_number = context.user_data['sendonly_member']
    percentage = context.user_data['sendonly_percentage']
    max_attempts = attempts

    if not owner_password:
        await query.edit_message_text("❌ لا توجد كلمة مرور للرقم. أعد استخدام الأمر /sendonly واختر الرقم مرة أخرى.", parse_mode=None)
        return ConversationHandler.END

    await query.edit_message_text("✅ تم التحقق. سيتم بدء العملية الآن...\nسيتم تحديثك بالنتائج.", parse_mode=None)
    stop_keyboard = ReplyKeyboardMarkup([[KeyboardButton("⏹️ إيقاف العملية")]], resize_keyboard=True)
    await context.bot.send_message(chat_id=query.message.chat_id, text="يمكنك إيقاف العملية في أي وقت بالضغط على الزر أدناه.", reply_markup=stop_keyboard)
    asyncio.create_task(run_sendonly_process(update, context, owner_number, owner_password, member_number, percentage, max_attempts))
    return ConversationHandler.END

async def run_sendonly_process(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_number: str, owner_password: str, member_number: str, percentage: int, max_attempts: int):
    user_id = update.effective_user.id
    stop_event = asyncio.Event()
    context.user_data['sendonly_stop_event'] = stop_event

    if update.callback_query:
        chat_id = update.callback_query.message.chat_id
        status_msg = await context.bot.send_message(chat_id=chat_id, text="🔄 جاري تجهيز العملية...", parse_mode=None)
    else:
        chat_id = update.message.chat_id
        status_msg = await update.message.reply_text("🔄 جاري تجهيز العملية...", parse_mode=None)

    async def update_status(text: str):
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=text, parse_mode=None)
        except:
            pass

    success = False
    for attempt in range(1, max_attempts+1):
        if stop_event.is_set():
            await update_status("⏹️ تم إيقاف العملية من قبل المستخدم.")
            break
        
        await update_status(f"🔄 المحاولة {attempt}/{max_attempts}\n⏳ جلب التوكنات...")
        tokens = await get_multiple_tokens_sendonly(owner_number, owner_password, 4, stop_event)
        if not tokens:
            await update_status(f"❌ المحاولة {attempt} فشلت: لم نحصل على أي توكن.\n⏳ انتظار 60 ثانية...")
            if attempt == max_attempts:
                break
            await asyncio.sleep(60)
            continue
        
        await update_status(f"🔄 المحاولة {attempt}\n✅ تم جلب {len(tokens)} توكن\n📡 إرسال 4 دعوات بنسبة {percentage}%...")
        results = await send_four_invites(owner_number, member_number, tokens, percentage)
        successful = sum(1 for r in results if r.get("success"))

        # بناء نص النتائج
        result_text = f"📊 نتائج المحاولة {attempt}:\n"
        for i, r in enumerate(results[:2]):
            src = "📱 موبايل"
            status_code = r.get('status_code', '?')
            icon = '✅' if r.get('success') else '❌'
            result_text += f"{src} (كود {status_code}) {icon}\n"
        for i, r in enumerate(results[2:]):
            src = "🌐 ويب"
            status_code = r.get('status_code', '?')
            icon = '✅' if r.get('success') else '❌'
            result_text += f"{src} (كود {status_code}) {icon}\n"
        result_text += f"\n✅ نجحت {successful}/4 دعوات."

        if successful >= 2:
            await update_status(result_text + "\n🎉 تم تحقيق المطلوب!\n✅ العملية ناجحة.")
            success = True
            sendonly_stats["total_success"] += 1
            sendonly_stats["total_attempts"] += 1
            if user_id not in sendonly_stats["user_logs"]:
                sendonly_stats["user_logs"][user_id] = {"success": 0, "attempts": 0}
            sendonly_stats["user_logs"][user_id]["success"] += 1
            sendonly_stats["user_logs"][user_id]["attempts"] += 1
            save_sendonly_stats()
            break
        else:
            if attempt == max_attempts:
                await update_status(result_text + "\n❌ انتهت المحاولات دون تحقيق دعوتين ناجحتين.")
            else:
                await update_status(result_text + "\n⚠️ لم تصل إلى دعوتين ناجحتين.\n🗑️ جاري حذف العضو وإعادة المحاولة...")
                delete_token = tokens[0] if tokens else None
                if delete_token:
                    delete_success = False
                    delays = [5, 10, 10]  # 5، 10، 10 ثوانٍ
                    for del_attempt, delay in enumerate(delays):
                        if stop_event.is_set():
                            break
                        await asyncio.sleep(delay)
                        del_result = await asyncio.to_thread(remove_member_sendonly, owner_number, delete_token, member_number)
                        if del_result:
                            delete_success = True
                            await update_status(result_text + f"\n✅ تم حذف العضو (محاولة {del_attempt+1})")
                            break
                        else:
                            await update_status(result_text + f"\n⚠️ فشل الحذف {del_attempt+1}، إعادة المحاولة...")
                    if not delete_success:
                        await update_status(result_text + "\n❌ فشلت جميع محاولات الحذف، متابعة المحاولات رغم ذلك...")
                else:
                    await update_status(result_text + "\n⚠️ لا يوجد توكن للحذف، قد يفشل لاحقاً.")
                await asyncio.sleep(60)

    if not success:
        sendonly_stats["total_attempts"] += 1
        if user_id not in sendonly_stats["user_logs"]:
            sendonly_stats["user_logs"][user_id] = {"success": 0, "attempts": 0}
        sendonly_stats["user_logs"][user_id]["attempts"] += 1
        save_sendonly_stats()

    await context.bot.send_message(chat_id=chat_id, text="ℹ️ انتهت العملية.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
    context.user_data.pop('sendonly_stop_event', None)

async def stop_sendonly_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stop_event = context.user_data.get('sendonly_stop_event')
    if stop_event:
        stop_event.set()
        await update.message.reply_text("⏹️ تم إرسال طلب إيقاف العملية... سيتم إيقافها قريباً.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
    else:
        await update.message.reply_text("ℹ️ لا توجد عملية 'تعليق دعوتين' جارية حالياً.", parse_mode=None)

async def sendonly_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ هذا الأمر للأدمن فقط.", parse_mode=None)
        return
    text = f"📊 إحصائيات تعليق دعوتين\n\n✅ عمليات ناجحة: {sendonly_stats['total_success']}\n🔄 إجمالي المحاولات: {sendonly_stats['total_attempts']}\n\n👥 المستخدمون:\n"
    for uid, logs in sendonly_stats["user_logs"].items():
        text += f"🆔 {uid}: نجاح {logs['success']} | محاولات {logs['attempts']}\n"
    await update.message.reply_text(text, parse_mode=None)

async def reset_sendonly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global sendonly_stats
    sendonly_stats = {"total_success": 0, "total_attempts": 0, "user_logs": {}}
    save_sendonly_stats()
    await update.message.reply_text("✅ تم تصفير إحصائيات تعليق دعوتين.", parse_mode=None)

# ================== أوامر الأدمن الجديدة ==================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="admin_users")],
        [InlineKeyboardButton("📢 بث رسالة", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎟️ تبديل الوضع (مجاني/اشتراك)", callback_data="admin_toggle_mode")],
        [InlineKeyboardButton("➕ إضافة اشتراك", callback_data="admin_add_sub")],
        [InlineKeyboardButton("➖ إلغاء اشتراك", callback_data="admin_remove_sub")],
        [InlineKeyboardButton("📋 المشتركين النشطين", callback_data="admin_list_subs")],
        [InlineKeyboardButton("👑 إدارة الأدمن", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("🔧 إعدادات أخرى", callback_data="admin_settings")],
    ]
    await update.message.reply_text("👑 لوحة تحكم الأدمن", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    st = get_stats()
    free_mode = "مجاني" if is_free_mode() else "مدفوع (اشتراك)"
    text = f"📊 إحصائيات البوت\n\n👥 إجمالي المستخدمين: {st['total_users']}\n🔐 جلسات نشطة: {st['active_sessions']}\n📞 أرقام محفوظة: {st['saved_numbers']}\n🚫 محظورون: {st['banned_count']}\n🎟️ مشتركون نشطون: {st['active_subs']}\n⚙️ الوضع الحالي: {free_mode}"
    await query.edit_message_text(text, parse_mode=None)

async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    users = get_all_users()
    if not users:
        await query.edit_message_text("لا يوجد مستخدمون حتى الآن.")
        return
    text = "👥 قائمة المستخدمين:\n\n"
    for uid, first, last in users[:50]:
        first_str = first.strftime("%Y-%m-%d %H:%M")
        last_str = last.strftime("%Y-%m-%d %H:%M")
        text += f"🆔 {uid}\n📅 أول ظهور: {first_str}\n🕒 آخر نشاط: {last_str}\n\n"
    if len(users) > 50:
        text += f"... و {len(users)-50} مستخدم آخر. استخدم الأمر /users لرؤية الكل."
    await query.edit_message_text(text, parse_mode=None)

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    context.user_data['awaiting_broadcast'] = True
    await query.edit_message_text("📢 أرسل الرسالة التي تريد بثها لجميع المستخدمين:")

async def admin_toggle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    current = is_free_mode()
    set_free_mode(not current)
    new_status = "مجاني" if not current else "مدفوع (اشتراك)"
    await query.edit_message_text(f"✅ تم تبديل وضع البوت إلى: {new_status}")

async def admin_add_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    context.user_data['awaiting_add_sub'] = True
    await query.edit_message_text("➕ أرسل معرف المستخدم وعدد الأيام مفصولة بمسافة\nمثال: `123456789 30`", parse_mode='Markdown')

async def admin_remove_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    context.user_data['awaiting_remove_sub'] = True
    await query.edit_message_text("➖ أرسل معرف المستخدم لإلغاء اشتراكه\nمثال: `123456789`", parse_mode='Markdown')

async def admin_list_subs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    subs = list_active_subscriptions()
    if not subs:
        await query.edit_message_text("📭 لا يوجد مشتركون نشطون حالياً.")
        return
    text = "✅ المشتركون النشطون:\n\n"
    for uid, expiry in subs[:50]:
        text += f"🆔 {uid} : ينتهي {expiry.strftime('%Y-%m-%d')}\n"
    if len(subs) > 50:
        text += f"\n... و {len(subs)-50} آخر. استخدم الأمر /list_subscriptions لرؤية الكل."
    await query.edit_message_text(text, parse_mode=None)

async def admin_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ هذا القسم للأدمن الرئيسي فقط.")
        return
    keyboard = [
        [InlineKeyboardButton("➕ إضافة أدمن", callback_data="admin_add_admin")],
        [InlineKeyboardButton("➖ إزالة أدمن", callback_data="admin_remove_admin")],
        [InlineKeyboardButton("📋 قائمة الأدمن", callback_data="admin_list_admins")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]
    ]
    await query.edit_message_text("👑 إدارة الأدمن", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ غير مصرح.")
        return
    context.user_data['awaiting_add_admin'] = True
    await query.edit_message_text("➕ أرسل معرف المستخدم لإضافته كأدمن مساعد:\nمثال: `123456789`", parse_mode='Markdown')

async def admin_remove_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ غير مصرح.")
        return
    context.user_data['awaiting_remove_admin'] = True
    await query.edit_message_text("➖ أرسل معرف المستخدم لإزالة صلاحيات الأدمن عنه:\nمثال: `123456789`", parse_mode='Markdown')

async def admin_list_admins_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    admins = list_admins()
    if not admins:
        await query.edit_message_text("لا يوجد أدمن.")
        return
    text = "👑 قائمة الأدمن:\n"
    for aid in admins:
        if aid == ADMIN_ID:
            text += f"👑 {aid} (رئيسي)\n"
        else:
            text += f"👤 {aid}\n"
    await query.edit_message_text(text, parse_mode=None)

async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    keyboard = [
        [InlineKeyboardButton("🔄 تبديل طريقة الإرسال", callback_data="admin_switch_method")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]
    ]
    await query.edit_message_text("🔧 إعدادات أخرى", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_switch_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SEND_METHOD
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    SEND_METHOD = 2 if SEND_METHOD == 1 else 1
    await query.edit_message_text(f"✅ تم التبديل إلى الطريقة {SEND_METHOD} (1=aiohttp, 2=threading)")

async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ غير مصرح.")
        return
    await admin_panel(update, context)

# ================== أوامر الأدمن النصية ==================
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None)
        return
    admin_log("USERS_COMMAND")
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون حتى الآن.", parse_mode=None)
        return
    text = "👥 قائمة المستخدمين:\n\n"
    for uid, first, last in users:
        first_str = first.strftime("%Y-%m-%d %H:%M")
        last_str = last.strftime("%Y-%m-%d %H:%M")
        text += f"🆔 {uid}\n📅 أول ظهور: {first_str}\n🕒 آخر نشاط: {last_str}\n\n"
        if len(text) > 3800:
            await update.message.reply_text(text, parse_mode=None)
            text = ""
    if text:
        await update.message.reply_text(text, parse_mode=None)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None)
        return
    admin_log("STATS_COMMAND")
    st = get_stats()
    free_mode = "مجاني" if is_free_mode() else "مدفوع (اشتراك)"
    text = f"📊 إحصائيات البوت\n\n👥 إجمالي المستخدمين: {st['total_users']}\n🔐 جلسات نشطة: {st['active_sessions']}\n📞 أرقام محفوظة: {st['saved_numbers']}\n🚫 مستخدمون محظورون: {st['banned_count']}\n🎟️ مشتركون نشطون: {st['active_subs']}\n⚙️ الوضع الحالي: {free_mode}"
    await update.message.reply_text(text, parse_mode=None)

async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None)
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /block <user_id>", parse_mode=None)
        return
    try:
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("لا يمكن حظر المطور نفسه.", parse_mode=None)
            return
        ban_user(uid)
        admin_log("BLOCK_USER", str(uid))
        await update.message.reply_text(f"✅ تم حظر المستخدم {uid}.", parse_mode=None)
    except:
        await update.message.reply_text("معرف غير صالح.", parse_mode=None)

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None)
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /unblock <user_id>", parse_mode=None)
        return
    try:
        uid = int(context.args[0])
        unban_user(uid)
        admin_log("UNBLOCK_USER", str(uid))
        await update.message.reply_text(f"✅ تم رفع الحظر عن المستخدم {uid}.", parse_mode=None)
    except:
        await update.message.reply_text("معرف غير صالح.", parse_mode=None)

async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE_MODE
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None)
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /maintenance on  أو  /maintenance off", parse_mode=None)
        return
    arg = context.args[0].lower()
    if arg == "on":
        MAINTENANCE_MODE = True
        admin_log("MAINTENANCE_ON")
        await update.message.reply_text("🛠️ تم تفعيل وضع الصيانة. البوت لا يستقبل أوامر من المستخدمين العاديين.", parse_mode=None)
    elif arg == "off":
        MAINTENANCE_MODE = False
        admin_log("MAINTENANCE_OFF")
        await update.message.reply_text("✅ تم إلغاء وضع الصيانة. البوت يعمل بشكل طبيعي.", parse_mode=None)
    else:
        await update.message.reply_text("الاستخدام: /maintenance on  أو  /maintenance off", parse_mode=None)

async def set_method_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SEND_METHOD
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ أمر الأدمن فقط.", parse_mode=None)
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /setmethod 1  أو  /setmethod 2\n1: aiohttp (محسن)، 2: threading (قديم)", parse_mode=None)
        return
    try:
        method = int(context.args[0])
        if method in (1, 2):
            SEND_METHOD = method
            admin_log("SET_METHOD", f"الطريقة {method}")
            await update.message.reply_text(f"✅ تم التبديل إلى الطريقة {method} (1=aiohttp، 2=threading).", parse_mode=None)
        else:
            await update.message.reply_text("❌ اختر 1 أو 2 فقط.", parse_mode=None)
    except:
        await update.message.reply_text("❌ قيمة غير صحيحة.", parse_mode=None)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None)
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: /broadcast <الرسالة>", parse_mode=None)
        return
    message = ' '.join(context.args)
    admin_log("BROADCAST", message[:100])
    users = get_all_users()
    sent = 0
    for uid, _, _ in users:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 إشعار من الإدارة:\n{message}", parse_mode=None)
            sent += 1
        except:
            pass
    await update.message.reply_text(f"✅ تم إرسال الإشعار إلى {sent} مستخدم.", parse_mode=None)

# أوامر إدارة الأدمن والاشتراكات النصية
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للأدمن الرئيسي فقط.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /add_admin <user_id>")
        return
    try:
        uid = int(context.args[0])
        if add_admin(uid):
            await update.message.reply_text(f"✅ تم إضافة المستخدم {uid} كأدمن مساعد.")
        else:
            await update.message.reply_text(f"⚠️ المستخدم {uid} هو بالفعل أدمن أو معرف غير صالح.")
    except:
        await update.message.reply_text("❌ معرف غير صالح.")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للأدمن الرئيسي فقط.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /remove_admin <user_id>")
        return
    try:
        uid = int(context.args[0])
        if remove_admin(uid):
            await update.message.reply_text(f"✅ تم إزالة صلاحيات الأدمن عن المستخدم {uid}.")
        else:
            await update.message.reply_text(f"⚠️ فشل الإزالة (قد يكون المعرف هو الأدمن الرئيسي أو ليس أدمن).")
    except:
        await update.message.reply_text("❌ معرف غير صالح.")

async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    admins = list_admins()
    text = "👑 قائمة الأدمن:\n"
    for aid in admins:
        if aid == ADMIN_ID:
            text += f"👑 {aid} (رئيسي)\n"
        else:
            text += f"👤 {aid}\n"
    await update.message.reply_text(text)

async def add_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("الاستخدام: /add_subscription <user_id> <days>")
        return
    try:
        uid = int(context.args[0])
        days = int(context.args[1])
        if days <= 0:
            await update.message.reply_text("⚠️ عدد الأيام يجب أن يكون موجباً.")
            return
        expiry = add_subscription(uid, days)
        await update.message.reply_text(f"✅ تم إضافة اشتراك للمستخدم {uid} لمدة {days} يوماً.\nينتهي في: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
    except:
        await update.message.reply_text("❌ معرف أو عدد أيام غير صالح.")

async def remove_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /remove_subscription <user_id>")
        return
    try:
        uid = int(context.args[0])
        if remove_subscription(uid):
            await update.message.reply_text(f"✅ تم إلغاء اشتراك المستخدم {uid}.")
        else:
            await update.message.reply_text(f"⚠️ المستخدم {uid} ليس لديه اشتراك نشط.")
    except:
        await update.message.reply_text("❌ معرف غير صالح.")

async def check_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /check_subscription <user_id>")
        return
    try:
        uid = int(context.args[0])
        expiry = get_subscription_expiry(uid)
        if expiry:
            await update.message.reply_text(f"📅 اشتراك المستخدم {uid} ينتهي في: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            await update.message.reply_text(f"ℹ️ المستخدم {uid} ليس لديه اشتراك نشط.")
    except:
        await update.message.reply_text("❌ معرف غير صالح.")

async def list_subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    subs = list_active_subscriptions()
    if not subs:
        await update.message.reply_text("📭 لا يوجد مشتركون نشطون حالياً.")
        return
    text = "✅ المشتركون النشطون:\n\n"
    for uid, expiry in subs:
        text += f"🆔 {uid} : ينتهي {expiry.strftime('%Y-%m-%d')}\n"
        if len(text) > 4000:
            await update.message.reply_text(text)
            text = ""
    await update.message.reply_text(text)

async def toggle_free_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح.")
        return
    current = is_free_mode()
    set_free_mode(not current)
    new_status = "مجاني" if not current else "مدفوع (اشتراك)"
    await update.message.reply_text(f"✅ تم تبديل وضع البوت إلى: {new_status}")

# ================== أوامر المساعدة والحفظ ==================
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_states:
        del user_states[user_id]
    await update.message.reply_text("✅ تم إلغاء العملية الحالية.", parse_mode=None)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None)
        return
    if is_admin(user_id):
        text = (
            "🔹 الأوامر المتاحة للمطور:\n"
            "/start - بدء البوت\n/savenumber <رقم> - حفظ رقم هاتف\n/cancel - إلغاء العملية\n/help - هذه المساعدة\n"
            "/users - عرض جميع المستخدمين\n/stats - إحصائيات البوت\n/block <user_id> - حظر مستخدم\n/unblock <user_id> - رفع الحظر\n"
            "/maintenance on/off - تفعيل/إلغاء وضع الصيانة\n/broadcast <رسالة> - إرسال إشعار للجميع\n"
            "/setmethod 1/2 - تبديل طريقة الإرسال (1=aiohttp، 2=threading)\n"
            "/sendonly - تعليق دعوتين\n/sendonly_stats - إحصائيات التعليق\n/resetsendonly - تصفير الإحصائيات\n\n"
            "🔐 أوامر إدارة الأدمن والاشتراكات:\n"
            "/add_admin <user_id>\n/remove_admin <user_id>\n/list_admins\n"
            "/add_subscription <user_id> <days>\n/remove_subscription <user_id>\n/check_subscription <user_id>\n/list_subscriptions\n/toggle_free_mode\n\n"
            "📌 يمكنك استخدام الأزرار التفاعلية أيضًا."
        )
    else:
        text = (
            "🔹 الأوامر المتاحة:\n"
            "/start - بدء البوت\n/savenumber <رقم> - حفظ رقم هاتف\n/cancel - إلغاء العملية\n/help - عرض هذه المساعدة\n"
            "/sendonly - تعليق دعوتين\n\n"
            "📌 يمكنك استخدام الأزرار التفاعلية لإدارة عائلتك."
        )
    await update.message.reply_text(text, parse_mode=None)

async def save_number_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None)
        return
    if len(context.args) != 1:
        await update.message.reply_text("الرجاء استخدام الأمر بالشكل: /savenumber <رقم الهاتف>", parse_mode=None)
        return
    number = context.args[0].strip()
    save_number(user_id, number)
    log_action(user_id, "SAVE_NUMBER", number)
    await update.message.reply_text(f"✅ تم حفظ الرقم {number} في قائمة الأرقام المحفوظة.", parse_mode=None)

# ================== المعالجات الأساسية ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE_MODE
    user_id = update.effective_user.id
    if MAINTENANCE_MODE and not is_admin(user_id):
        await update.message.reply_text("🛠️ البوت في وضع الصيانة حاليًا. عذرًا للإزعاج.", parse_mode=None)
        return
    record_user_activity(user_id)
    log_action(user_id, "START")
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 لقد تم حظرك من استخدام هذا البوت.", parse_mode=None)
        return
    user = get_user(user_id)
    if user:
        await show_main_menu(update, user_id, context)
    else:
        welcome_text = (
            "🐋 حــوت الـمـجـال | HOOT ALMGAL\n\n"
            "⚡ أهلاً بيك في عالم السرعة والتحكم\n"
            "إدارة حسابك وخدمات فليكس بسهولة من مكان واحد.\n\n"
            "🔐 سجّل دخولك للبدء"
        )
        await send_message(update, user_id, welcome_text)
        await show_login_screen(update, user_id, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE_MODE
    user_id = update.effective_user.id
    if MAINTENANCE_MODE and not is_admin(user_id):
        await update.message.reply_text("🛠️ البوت في وضع الصيانة حاليًا. عذرًا للإزعاج.", parse_mode=None)
        return
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None)
        return
    record_user_activity(user_id)
    text = update.message.text.strip()

    if context.user_data.get('awaiting_flex_deduction_count'):
        if not text.isdigit():
            await update.message.reply_text("❌ ابعت عدد صحيح أكبر من صفر.", parse_mode=None)
            return
        times = int(text)
        if times <= 0:
            await update.message.reply_text("❌ العدد لازم يكون أكبر من صفر.", parse_mode=None)
            return
        if times > 50:
            await update.message.reply_text("❌ الحد الأقصى 50 عملية في المرة الواحدة.", parse_mode=None)
            return
        context.user_data['awaiting_flex_deduction_count'] = False
        await execute_flex_deduction(update, user_id, context, times)
        return


    # معالجة البث من الأدمن
    if context.user_data.get('awaiting_broadcast'):
        if not is_admin(user_id):
            context.user_data.pop('awaiting_broadcast', None)
            await update.message.reply_text("⛔ غير مصرح.")
            return
        message = text
        users = get_all_users()
        sent = 0
        for uid, _, _ in users:
            try:
                await context.bot.send_message(chat_id=uid, text=f"📢 إشعار من الإدارة:\n{message}", parse_mode=None)
                sent += 1
            except:
                pass
        await update.message.reply_text(f"✅ تم إرسال الإشعار إلى {sent} مستخدم.", parse_mode=None)
        context.user_data.pop('awaiting_broadcast', None)
        return

    # معالجة إضافة اشتراك من لوحة الأدمن
    if context.user_data.get('awaiting_add_sub'):
        if not is_admin(user_id):
            context.user_data.pop('awaiting_add_sub', None)
            await update.message.reply_text("⛔ غير مصرح.")
            return
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("⚠️ أرسل معرف المستخدم وعدد الأيام مفصولة بمسافة.")
            return
        try:
            uid = int(parts[0])
            days = int(parts[1])
            expiry = add_subscription(uid, days)
            await update.message.reply_text(f"✅ تم إضافة اشتراك للمستخدم {uid} لمدة {days} يوماً.\nينتهي في: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
        except:
            await update.message.reply_text("❌ معرف أو عدد أيام غير صالح.")
        context.user_data.pop('awaiting_add_sub', None)
        return

    # معالجة إلغاء اشتراك من لوحة الأدمن
    if context.user_data.get('awaiting_remove_sub'):
        if not is_admin(user_id):
            context.user_data.pop('awaiting_remove_sub', None)
            await update.message.reply_text("⛔ غير مصرح.")
            return
        try:
            uid = int(text)
            if remove_subscription(uid):
                await update.message.reply_text(f"✅ تم إلغاء اشتراك المستخدم {uid}.")
            else:
                await update.message.reply_text(f"⚠️ المستخدم {uid} ليس لديه اشتراك نشط.")
        except:
            await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_remove_sub', None)
        return

    # معالجة إضافة أدمن من لوحة الأدمن
    if context.user_data.get('awaiting_add_admin'):
        if user_id != ADMIN_ID:
            context.user_data.pop('awaiting_add_admin', None)
            await update.message.reply_text("⛔ غير مصرح.")
            return
        try:
            uid = int(text)
            if add_admin(uid):
                await update.message.reply_text(f"✅ تم إضافة المستخدم {uid} كأدمن مساعد.")
            else:
                await update.message.reply_text(f"⚠️ المستخدم {uid} هو بالفعل أدمن أو معرف غير صالح.")
        except:
            await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_add_admin', None)
        return

    # معالجة إزالة أدمن من لوحة الأدمن
    if context.user_data.get('awaiting_remove_admin'):
        if user_id != ADMIN_ID:
            context.user_data.pop('awaiting_remove_admin', None)
            await update.message.reply_text("⛔ غير مصرح.")
            return
        try:
            uid = int(text)
            if remove_admin(uid):
                await update.message.reply_text(f"✅ تم إزالة صلاحيات الأدمن عن المستخدم {uid}.")
            else:
                await update.message.reply_text(f"⚠️ فشل الإزالة (قد يكون المعرف هو الأدمن الرئيسي أو ليس أدمن).")
        except:
            await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_remove_admin', None)
        return

    # باقي معالجة الحالات العادية
    if user_id not in user_states:
        await show_login_screen(update, user_id, context)
        return
    state_data = user_states[user_id]
    state = state_data.get('state')

    if state == 'login_phone':
        valid, msg = validate_phone(text)
        if not valid:
            await update.message.reply_text(f"{msg}\nأرسل رقم الهاتف الصحيح (مثال: 01012345678):", parse_mode=None)
            return
        user_states[user_id] = {'state': 'login_password', 'phone': text}
        await update.message.reply_text("🔑 أدخل كلمة المرور:", parse_mode=None)
    elif state == 'login_password':
        phone = state_data.get('phone')
        password = text
        await update.message.reply_text("🔄 جاري تسجيل الدخول...", parse_mode=None)
        try:
            token, expiry = await login_vodafone(phone, password)
            save_user(user_id, phone, token, expiry)
            save_number(user_id, phone)
            save_password(user_id, phone, password, 24)
            log_action(user_id, "LOGIN_SUCCESS", phone)
            await update.message.reply_text("✅ تم تسجيل الدخول بنجاح وتم حفظ الرقم.", parse_mode=None)
            del user_states[user_id]
            await show_main_menu(update, user_id, context)
        except Exception as e:
            log_action(user_id, "LOGIN_FAIL", str(e))
            await update.message.reply_text(f"❌ فشل تسجيل الدخول: {str(e)}", parse_mode=None)
            del user_states[user_id]
    elif state == 'accept_password':
        member_phone = state_data.get('member_phone')
        password = text
        owner = get_user(user_id)
        if not owner:
            await show_login_screen(update, user_id, context)
            del user_states[user_id]
            return
        await update.message.reply_text("🔄 جاري قبول الدعوة...", parse_mode=None)
        try:
            member_token, _ = await login_vodafone(member_phone, password)
            current = await get_family_members(owner['access_token'], owner['phone'])
            rocket_add = None
            for p in current.get('pending', []):
                if p['phone'] == member_phone and p.get('count', 0) >= 2:
                    rocket_add = member_phone
                    break
            success = await accept_invitation(member_token, member_phone, owner['phone'])
            if success:
                log_action(user_id, "ACCEPT_INVITE", member_phone)
                await update.message.reply_text(f"✅ تم قبول الدعوة للرقم {member_phone}", parse_mode=None)
                if rocket_add:
                    rocket_list = context.user_data.get('rocket_members', [])
                    if rocket_add not in rocket_list:
                        rocket_list.append(rocket_add)
                        context.user_data['rocket_members'] = rocket_list
                await show_family_screen(update, user_id, context)
            else:
                await update.message.reply_text("❌ فشل قبول الدعوة", parse_mode=None)
            del user_states[user_id]
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ: {str(e)}", parse_mode=None)
            del user_states[user_id]
    elif state == 'invite_phone':
        member_phone = text
        percentage = state_data.get('percentage')
        owner = get_user(user_id)
        if not owner:
            await show_login_screen(update, user_id, context)
            del user_states[user_id]
            return
        await update.message.reply_text(f"🔄 جاري إرسال دعوة إلى {member_phone}...", parse_mode=None)
        success = await send_invitation(owner['access_token'], owner['phone'], member_phone, percentage)
        if success:
            log_action(user_id, "SEND_INVITE", f"{member_phone} ({percentage}%)")
            await update.message.reply_text(f"✅ تم إرسال الدعوة إلى {member_phone}", parse_mode=None)
        else:
            await update.message.reply_text("❌ فشل إرسال الدعوة", parse_mode=None)
        del user_states[user_id]
        await show_family_screen(update, user_id, context)
    elif state == 'switch_account_ask':
        new_phone = state_data.get('new_phone')
        password = text
        await update.message.reply_text("🔄 جاري تسجيل الدخول...", parse_mode=None)
        try:
            token, expiry = await login_vodafone(new_phone, password)
            save_user(user_id, new_phone, token, expiry)
            save_number(user_id, new_phone)
            save_password(user_id, new_phone, password, 24)
            log_action(user_id, "SWITCH_ACCOUNT", new_phone)
            await update.message.reply_text(f"✅ تم التبديل إلى الرقم {new_phone} بنجاح.", parse_mode=None)
            del user_states[user_id]
            await show_main_menu(update, user_id, context)
        except Exception as e:
            await update.message.reply_text(f"❌ فشل التبديل: {str(e)}", parse_mode=None)
            del user_states[user_id]
    elif state == 'cleaning_ask_owner_pass':
        owner_pass = text
        owner_num = state_data.get('owner_num')
        user_states[user_id] = {'state': 'cleaning_ask_member', 'owner_num': owner_num, 'owner_pass': owner_pass}
        await update.message.reply_text("📞 أدخل رقم العضو المراد تنظيفه (مثال: 01012345678):", parse_mode=None)
    elif state == 'cleaning_ask_member':
        member_num = text
        owner_num = state_data.get('owner_num')
        owner_pass = state_data.get('owner_pass')
        user_states[user_id] = {'state': 'cleaning_ask_member_pass', 'owner_num': owner_num, 'owner_pass': owner_pass, 'member_num': member_num}
        await update.message.reply_text("🔐 أدخل كلمة مرور حساب العضو (الذي سيتم تنظيفه):", parse_mode=None)
    elif state == 'cleaning_ask_member_pass':
        member_pass = text
        owner_num = state_data.get('owner_num')
        owner_pass = state_data.get('owner_pass')
        member_num = state_data.get('member_num')
        await perform_full_cleaning_silent(update, user_id, owner_num, owner_pass, member_num, member_pass, context)
        del user_states[user_id]
    else:
        await send_message(update, user_id, "حدث خطأ. استخدم /start للبدء من جديد.")
        del user_states[user_id]

# ================== معالج الأزرار ==================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE_MODE
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if MAINTENANCE_MODE and not is_admin(user_id):
        await query.edit_message_text("🛠️ البوت في وضع الصيانة حاليًا. عذرًا للإزعاج.", parse_mode=None)
        return
    if is_user_banned(user_id):
        await query.edit_message_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None)
        return
    record_user_activity(user_id)

    # معالجة أزرار لوحة الأدمن
    if data.startswith("admin_"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ غير مصرح.")
            return
        if data == "admin_stats":
            await admin_stats_callback(update, context)
        elif data == "admin_users":
            await admin_users_callback(update, context)
        elif data == "admin_broadcast":
            await admin_broadcast_start(update, context)
        elif data == "admin_toggle_mode":
            await admin_toggle_mode_callback(update, context)
        elif data == "admin_add_sub":
            await admin_add_sub_start(update, context)
        elif data == "admin_remove_sub":
            await admin_remove_sub_start(update, context)
        elif data == "admin_list_subs":
            await admin_list_subs_callback(update, context)
        elif data == "admin_manage_admins":
            await admin_manage_admins(update, context)
        elif data == "admin_add_admin":
            await admin_add_admin_start(update, context)
        elif data == "admin_remove_admin":
            await admin_remove_admin_start(update, context)
        elif data == "admin_list_admins":
            await admin_list_admins_callback(update, context)
        elif data == "admin_settings":
            await admin_settings(update, context)
        elif data == "admin_switch_method":
            await admin_switch_method(update, context)
        elif data == "admin_back":
            await admin_back(update, context)
        return

    # أزرار لا تحتاج جلسة
    if data == "another_account":
        delete_user(user_id)
        if user_id in user_states:
            del user_states[user_id]
        if context.user_data.get('rocket_members'):
            context.user_data['rocket_members'] = []
        await clear_previous_messages(update, user_id)
        await query.edit_message_text("✅ تم تسجيل الخروج.", parse_mode=None)
        await show_login_screen(update, user_id, context)
        return

    if data == "main_menu":
        context.user_data.pop('awaiting_flex_deduction_count', None)
        context.user_data.pop('flex_discount_offer', None)
        context.user_data.pop('flex_discount_token', None)
        await show_main_menu(update, user_id, context)
        return

    if data == "refresh_family":
        try:
            await query.delete_message()
        except:
            pass
        await clear_previous_messages(update, user_id)
        await show_family_screen(update, user_id, context)
        return

    if data == "clean_family":
        owner_data = get_user(user_id)
        if not owner_data:
            await query.edit_message_text("❌ يجب تسجيل الدخول أولاً.", parse_mode=None)
            return
        user_states[user_id] = {'state': 'cleaning_ask_owner_pass', 'owner_num': owner_data['phone']}
        await query.edit_message_text("🔐 لتأكيد عملية تنظيف العائلة، أدخل كلمة مرور حساب المالك:", parse_mode=None)
        return

    if data == "show_consumption":
        await show_consumption(update, user_id, context)
        return

    if data == "new_manual_login":
        user_states[user_id] = {'state': 'login_phone'}
        await query.edit_message_text("📱 أرسل رقم هاتفك (مثال: 01012345678):", parse_mode=None)
        return

    # الأرقام المحفوظة
    if data.startswith("use_num_"):
        num = data[8:]
        token = get_valid_token(user_id, num)
        if token:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT expiry FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, num))
            row = c.fetchone()
            conn.close()
            expiry = datetime.fromisoformat(row[0]) if row else datetime.now() + timedelta(hours=1)
            save_user(user_id, num, token, expiry)
            log_action(user_id, "USE_SAVED_NUMBER", num)
            await query.edit_message_text(f"✅ تم التبديل إلى الرقم {num} باستخدام التوكن المخزن.", parse_mode=None)
            await show_main_menu(update, user_id, context)
        else:
            user_states[user_id] = {'state': 'switch_account_ask', 'new_phone': num}
            await query.edit_message_text(f"🔐 أدخل كلمة مرور الرقم {num}:", parse_mode=None)
        return

    if data.startswith("del_num_"):
        num = data[8:]
        delete_saved_number(user_id, num)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, num))
        conn.commit()
        conn.close()
        log_action(user_id, "DELETE_SAVED_NUMBER", num)
        await query.edit_message_text(f"✅ تم حذف الرقم {num}", parse_mode=None)
        await show_saved_numbers(update, user_id, context)
        return

    # أزرار التأكيد
    if data == "confirm_yes":
        confirm_data = user_states.get(user_id, {})
        action = confirm_data.get('confirm_action')
        user = await ensure_valid_session(update, user_id)
        if not user:
            await query.edit_message_text("الرجاء تسجيل الدخول أولاً.", parse_mode=None)
            return
        if action == 'edit':
            member_phone = confirm_data.get('member_phone')
            new_flex = confirm_data.get('new_flex')
            percent = confirm_data.get('percent')
            await query.edit_message_text(f"🔄 جاري تعديل نسبة {member_phone}...", parse_mode=None)
            success = await update_quota(user['access_token'], user['phone'], member_phone, percent)
            if success:
                log_action(user_id, "EDIT_QUOTA", f"{member_phone} -> {new_flex}")
                await query.answer("✅ تم تعديل النسبة بنجاح", show_alert=True)
                await show_family_screen(update, user_id, context)
            else:
                await query.answer("❌ فشل تعديل النسبة", show_alert=True)
                await query.edit_message_text(f"❌ فشل تعديل النسبة", parse_mode=None)
            del user_states[user_id]
        elif action == 'delete':
            member_phone = confirm_data.get('member_phone')
            await query.edit_message_text(f"🔄 جاري حذف {member_phone}...", parse_mode=None)
            success = await remove_member(user['access_token'], user['phone'], member_phone)
            if success:
                log_action(user_id, "DELETE_MEMBER", member_phone)
                await query.answer("✅ تم حذف الفرد بنجاح", show_alert=True)
                await show_family_screen(update, user_id, context)
            else:
                await query.answer("❌ فشل حذف الفرد", show_alert=True)
                await query.edit_message_text(f"❌ فشل حذف {member_phone}", parse_mode=None)
            del user_states[user_id]
        elif action == 'cancel_invite':
            member_phone = confirm_data.get('member_phone')
            await query.edit_message_text(f"🔄 جاري إلغاء دعوة {member_phone}...", parse_mode=None)
            success = await cancel_invitation(user['access_token'], user['phone'], member_phone)
            if success:
                log_action(user_id, "CANCEL_INVITE", member_phone)
                await query.answer("✅ تم إلغاء الدعوة بنجاح", show_alert=True)
                await show_family_screen(update, user_id, context)
            else:
                await query.answer("❌ فشل إلغاء الدعوة", show_alert=True)
                await query.edit_message_text(f"❌ فشل إلغاء الدعوة", parse_mode=None)
            del user_states[user_id]
        else:
            await query.answer("⚠️ إجراء غير معروف", show_alert=True)
            del user_states[user_id]
        return

    if data == "confirm_no":
        if user_id in user_states:
            del user_states[user_id]
        await query.answer("❌ تم إلغاء العملية", show_alert=True)
        await show_family_screen(update, user_id, context)
        return

    # زر تعليق دعوتين
    if data == "sendonly_menu":
        # التحقق من الاشتراك مرة أخرى
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await sendonly_menu(update, context)
        return

    if data == "packages_services":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await packages_services_menu(update, user_id, context)
        return

    if data == "packages_flex":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_bundles_menu(update, user_id, context)
        return

    if data == "packages_ignore":
        return

    if data.startswith("packages_flex_confirm_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await execute_flex_bundle_service(update, user_id, context, data.rsplit("_", 1)[-1])
        return

    if data.startswith("packages_flex_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_bundle_confirm_screen(update, user_id, context, data.rsplit("_", 1)[-1])
        return

    if data == "packages_convert14":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await convert14_confirm_screen(update, user_id, context)
        return

    if data == "packages_convert14_confirm":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await execute_convert14_service(update, user_id, context)
        return

    if data == "deduct_flexes":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await deduct_flexes(update, user_id, context)
        return

    if data == "flex_discount":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_discount_menu(update, user_id, context)
        return

    if data == "flex_discount_confirm":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_discount_confirm(update, user_id, context)
        return

    if data == "flex_discount_cancel":
        context.user_data.pop('flex_discount_offer', None)
        context.user_data.pop('flex_discount_token', None)
        await query.answer("❌ تم إلغاء التفعيل")
        await show_main_menu(update, user_id, context)
        return

    if data == "add_two_days":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await add_two_days(update, user_id, context)
        return

    # باقي الأزرار تتطلب جلسة نشطة وتحقق من الاشتراك
    if not await require_subscription_or_free(update, context, is_callback=True, query=query):
        return

    user = await ensure_valid_session(update, user_id)
    if not user:
        await query.edit_message_text("الرجاء تسجيل الدخول أولاً.", parse_mode=None)
        return

    if data == "show_family":
        context.user_data.pop('awaiting_flex_deduction_count', None)
        await show_family_screen(update, user_id, context)
    elif data == "show_owner_remaining":
        await show_owner_remaining(update, user_id, context)
    elif data == "show_saved":
        await show_saved_numbers(update, user_id, context)
    elif data.startswith("edit_"):
        parts = data.split("_")
        if len(parts) == 3:
            member_phone = parts[1]
            new_flex = parts[2]
            percent = 10 if new_flex == "1300" else 20 if new_flex == "2600" else 40
            confirm_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ لا", callback_data="confirm_no")]
            ])
            user_states[user_id] = {
                'confirm_action': 'edit',
                'member_phone': member_phone,
                'new_flex': new_flex,
                'percent': percent
            }
            await query.edit_message_text(f"⚠️ هل أنت متأكد من تعديل نسبة {member_phone} إلى {new_flex} فليكس؟", reply_markup=confirm_keyboard, parse_mode=None)
    elif data.startswith("del_"):
        member = data[4:]
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"),
             InlineKeyboardButton("❌ لا", callback_data="confirm_no")]
        ])
        user_states[user_id] = {
            'confirm_action': 'delete',
            'member_phone': member
        }
        await query.edit_message_text(f"⚠️ هل أنت متأكد من حذف الفرد {member}؟", reply_markup=confirm_keyboard, parse_mode=None)
    elif data.startswith("cancel_"):
        member = data[7:]
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"),
             InlineKeyboardButton("❌ لا", callback_data="confirm_no")]
        ])
        user_states[user_id] = {
            'confirm_action': 'cancel_invite',
            'member_phone': member
        }
        await query.edit_message_text(f"⚠️ هل أنت متأكد من إلغاء دعوة {member}؟", reply_markup=confirm_keyboard, parse_mode=None)
    elif data.startswith("accept_"):
        member_phone = data[7:]
        user_states[user_id] = {'state': 'accept_password', 'member_phone': member_phone}
        await query.edit_message_text(f"🔐 أدخل كلمة مرور حساب العضو {member_phone}:", parse_mode=None)
    elif data.startswith("invite_"):
        context.user_data.pop('awaiting_flex_deduction_count', None)
        percentage = int(data.split("_")[1])
        user_states[user_id] = {'state': 'invite_phone', 'percentage': percentage}
        await query.edit_message_text("📞 أدخل رقم العضو المراد دعوته (مثال: 01012345678):", parse_mode=None)
    else:
        await query.edit_message_text("حدث خطأ غير متوقع.", parse_mode=None)

# ================== تشغيل البوت ==================
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "بدء البوت"),
        BotCommand("savenumber", "حفظ رقم هاتف"),
        BotCommand("sendonly", "تعليق دعوتين"),
        BotCommand("cancel", "إلغاء العملية الحالية"),
        BotCommand("help", "عرض المساعدة"),
        BotCommand("admin", "لوحة تحكم الأدمن"),
    ])

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    sendonly_conv = ConversationHandler(
        entry_points=[
            CommandHandler('sendonly', sendonly_menu),
            CallbackQueryHandler(sendonly_menu, pattern='^sendonly_menu$')
        ],
        states={
            SENDONLY_SELECT_OWNER: [CallbackQueryHandler(sendonly_select_owner, pattern='^(sendonly_use_current|sendonly_owner_|sendonly_manual)$')],
            SENDONLY_OWNER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_owner_password)],
            SENDONLY_OWNER_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_owner_num_manual)],
            SENDONLY_OWNER_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_owner_pass_manual)],
            SENDONLY_MEMBER_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_member_number)],
            SENDONLY_PERCENTAGE: [CallbackQueryHandler(sendonly_percentage_callback, pattern='^sendonly_perc_')],
            SENDONLY_ATTEMPTS: [CallbackQueryHandler(sendonly_attempts_callback, pattern='^sendonly_att_')],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("savenumber", save_number_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("maintenance", maintenance_command))
    app.add_handler(CommandHandler("setmethod", set_method_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("sendonly_stats", sendonly_stats_command))
    app.add_handler(CommandHandler("resetsendonly", reset_sendonly_stats))
    app.add_handler(CommandHandler("add_admin", add_admin_command))
    app.add_handler(CommandHandler("remove_admin", remove_admin_command))
    app.add_handler(CommandHandler("list_admins", list_admins_command))
    app.add_handler(CommandHandler("add_subscription", add_subscription_command))
    app.add_handler(CommandHandler("remove_subscription", remove_subscription_command))
    app.add_handler(CommandHandler("check_subscription", check_subscription_command))
    app.add_handler(CommandHandler("list_subscriptions", list_subscriptions_command))
    app.add_handler(CommandHandler("toggle_free_mode", toggle_free_mode_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(sendonly_conv)
    app.add_handler(MessageHandler(filters.Text("⏹️ إيقاف العملية"), stop_sendonly_process))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("✅ البوت يعمل مع حفظ كلمة المرور 24 ساعة، طريقتين للإرسال، وآلية حذف 5,10,10 ثوانٍ.")
    print("✅ تمت إضافة نظام الأدمن والاشتراكات مع الوضع المجاني/المدفوع.")
    app.run_polling()

if __name__ == "__main__":
    main()