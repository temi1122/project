# ================= matplotlib =================
import os
import json
from dotenv import load_dotenv

load_dotenv()

import matplotlib
matplotlib.use("Agg")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
)
from google.cloud import firestore
from datetime import datetime, timedelta, timezone
import matplotlib.pyplot as plt

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("FIREBASE_KEY"))
COLLECTION = "sensors"

# ================= Firestore =================
db = firestore.Client.from_service_account_info(SERVICE_ACCOUNT_INFO)

# ================= Telegram =================
updater = Updater(token=BOT_TOKEN, use_context=True)
dispatcher = updater.dispatcher
job_queue = updater.job_queue

# ================= State =================
user_selected_sensor = {}   # chat_id -> sensor_id
alert_sent = {}             # sensor_id -> bool
subscribers = set()         # chat_id ที่รับแจ้งเตือน
no_data_alert_sent = {}
waiting_for_rename = {}    # chat_id -> True/False
no_data_alert_sent = {}
ADMINS = {}

# ================= Firestore Helpers =================
def get_all_sensors_with_name():
    sensors = []
    for doc in db.collection(COLLECTION).stream():
        data = doc.to_dict()
        name = data.get("name", doc.id)
        sensors.append((doc.id, name))
    return sensors

def is_admin(chat_id):
    doc = db.collection("admins").document(str(chat_id)).get()
    return doc.exists

def delete_sensor(sensor_id):
    print("Deleting:", sensor_id)
    ref = db.collection(COLLECTION).document(sensor_id)
    # ลบ history
    for d in ref.collection("history").stream():
        d.reference.delete()
    ref.delete()
    print("Deleted done")

def get_sensor_info(sensor_id):
    doc = db.collection(COLLECTION).document(sensor_id).get()
    return doc.to_dict() if doc.exists else {}

def set_room_name(sensor_id, room_name):
    db.collection(COLLECTION).document(sensor_id).set({
        "name": room_name
    }, merge=True)

def check_no_data(sensor_id, minutes=5):
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=minutes)

    docs = (
        db.collection(COLLECTION)
        .document(sensor_id)
        .collection("history")
        .order_by("updatedAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )

    for d in docs:
        data = d.to_dict()
        updated = data.get("updatedAt")
        if updated and updated < threshold:
            return True
        return False
    return True  # ไม่มีข้อมูลเลย


def send_no_data_alert(context, chat_id, sensor_id):
    sensor = get_sensor_info(sensor_id)
    room = sensor.get("name", sensor_id)

    msg = (
        "⚠️ แจ้งเตือนอุปกรณ์ ⚠️\n\n"
        f"🏠 ห้อง: {room}\n"
        f"📟 ID: {sensor_id}\n"
        "❌ ไม่มีข้อมูลส่งเข้ามา\n"
        "⏱ เกิน 10 นาที\n"
        "🔌 อาจออฟไลน์ / เน็ตหลุด"
    )

    context.bot.send_message(chat_id=chat_id, text=msg)
# ================= History / Graph =================
def get_history(sensor_id, field, limit=20):
    docs = (
        db.collection(COLLECTION)
        .document(sensor_id)
        .collection("history")
        .order_by("updatedAt")
        .limit(limit)
        .stream()
    )

    times, values = [], []
    for d in docs:
        data = d.to_dict()
        times.append(data["updatedAt"].strftime("%H:%M"))
        values.append(data[field])

    return times, values

def plot_graph(sensor_id, field, unit, title):
    times, values = get_history(sensor_id, field)
    if not values:
        return None

    filename = f"{sensor_id.replace(':','_')}_{field}.png"

    plt.figure()
    plt.plot(times, values, marker="o")
    plt.xlabel("Time")
    plt.ylabel(unit)
    plt.title(title)
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()
    return filename

# ================= Average Dust =================
def get_average_dust(sensor_id, limit=30):
    docs = (
        db.collection(COLLECTION)
        .document(sensor_id)
        .collection("history")
        .order_by("updatedAt", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )

    values = [d.to_dict().get("dust") for d in docs if d.to_dict().get("dust") is not None]
    if not values:
        return None

    return round(sum(values)/len(values), 2), len(values)

# ================= Dust Alert =================
def check_dust_over_30min(sensor_id, limit=50):
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=10)

    docs = (
        db.collection(COLLECTION)
        .document(sensor_id)
        .collection("history")
        .order_by("updatedAt", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )

    for d in docs:
        data = d.to_dict()
        dust = data.get("dust")
        updated = data.get("updatedAt")
        if dust is None or updated is None:
            continue
        if dust <= 40:
            return False
        if updated <= threshold:
            return True
    return False

def send_dust_alert(context, chat_id, sensor_id):
    sensor = get_sensor_info(sensor_id)
    room = sensor.get("name", sensor_id)

    msg = (
        "🚨 แจ้งเตือนคุณภาพอากาศ 🚨\n\n"
        f"🏠 ห้อง: {room}\n"
        f"📟 ID: {sensor_id}\n"
        "🌫 ค่าฝุ่นเกิน 40 µg/m³\n"
        "⏱ ต่อเนื่องมากกว่า 30 นาที"
    )
    context.bot.send_message(chat_id=chat_id, text=msg)

def dust_monitor(context):
    now = datetime.now(timezone.utc)
    for sensor_id, _ in get_all_sensors_with_name():
        sensor = get_sensor_info(sensor_id)
        room = sensor.get("name", sensor_id)
        
        # ===== ไม่มีข้อมูล =====
        if check_no_data(sensor_id):
            last_sent = no_data_alert_sent.get(sensor_id)
            if (last_sent is None) or (now - last_sent > timedelta(minutes=10)):
                for chat_id in subscribers:
                    send_no_data_alert(context, chat_id, sensor_id)
                no_data_alert_sent[sensor_id] = now
        else:
            if sensor_id in no_data_alert_sent:
                for chat_id in subscribers:
                    context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ {room} กลับมาส่งข้อมูลแล้ว"
                    )
                del no_data_alert_sent[sensor_id]

# ================= Menu =================
def show_room_menu(context, chat_id):
    keyboard = [
        [InlineKeyboardButton(f"🏠 {name}", callback_data=f"select:{sid}")]
        for sid, name in get_all_sensors_with_name()
    ]

    context.bot.send_message(
        chat_id=chat_id,
        text="🔌 เลือกห้อง:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def add_admin(update, context):
    chat_id = update.message.chat.id
    # ให้เฉพาะ admin เพิ่มได้
    if not is_admin(chat_id):
        update.message.reply_text("❌ ไม่มีสิทธิ์")
        return
    try:
        new_admin = context.args[0]
        db.collection("admins").document(new_admin).set({
            "added_by": chat_id
        })
        update.message.reply_text(f"✅ เพิ่มแอดมิน {new_admin} แล้ว")
    except:
        update.message.reply_text("❗ ใช้แบบนี้: /addadmin 123456789")

def myid(update, context):
    chat_id = update.message.chat.id

    update.message.reply_text(
        f"🆔 Chat ID ของคุณ\n\n{chat_id}"
    )

def list_admins(update, context):
    if not is_admin(update.message.chat.id):
        update.message.reply_text("❌ ไม่มีสิทธิ์")
        return

    docs = db.collection("admins").stream()

    text = "👑 รายชื่อแอดมิน\n\n"

    count = 0
    for doc in docs:
        text += f"• {doc.id}\n"
        count += 1

    if count == 0:
        text += "ไม่มีข้อมูล"

    update.message.reply_text(text)

def remove_admin(update, context):
    if not is_admin(update.message.chat.id):
        update.message.reply_text("❌ ไม่มีสิทธิ์")
        return

    try:
        admin_id = context.args[0]

        db.collection("admins").document(admin_id).delete()

        update.message.reply_text(
            f"🗑 ลบแอดมิน {admin_id} แล้ว"
        )

    except:
        update.message.reply_text(
            "❗ ใช้แบบนี้: /removeadmin 123456789"
        )
# ================= Handlers =================
def start(update, context):
    chat_id = update.message.chat.id
    print("CHAT ID:", chat_id)
    subscribers.add(chat_id)
    show_room_menu(context, chat_id)

def button_handler(update, context):
    query = update.callback_query
    query.answer()
    chat_id = query.message.chat.id
    data = query.data

    # ================= SELECT ROOM =================
    if data.startswith("select:"):
        sid = data.replace("select:", "")
        user_selected_sensor[chat_id] = sid

        sensor = get_sensor_info(sid)
        room = sensor.get("name", sid) if sensor else sid

        keyboard = [
            [InlineKeyboardButton("🌫 ค่าฝุ่น", callback_data="dust")],
            [InlineKeyboardButton("📊 ค่าเฉลี่ยฝุ่น", callback_data="avg")],
            [InlineKeyboardButton("📈 กราฟค่าฝุ่น", callback_data="graph")],
            [InlineKeyboardButton("🌡 อุณหภูมิ", callback_data="temp")],
            [InlineKeyboardButton("💧 ความชื้น", callback_data="hum")]
        ]

        # 👑 แอดมินเท่านั้น
        if is_admin(chat_id):
            keyboard.append([InlineKeyboardButton("✏ เปลี่ยนชื่อห้อง", callback_data="rename")])
            keyboard.append([InlineKeyboardButton("🗑 ลบห้อง", callback_data="confirm_delete")])

        keyboard.append([InlineKeyboardButton("🔄 เปลี่ยนห้อง", callback_data="change")])

        query.edit_message_text(
            f"✅ เลือกห้องแล้ว\n🏠 {room}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ================= CONFIRM DELETE =================
    if data == "confirm_delete":
        query.edit_message_text(
            "⚠ ยืนยันการลบห้อง?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ ยืนยัน", callback_data="delete")],
                [InlineKeyboardButton("❌ ยกเลิก", callback_data="change")]
            ])
        )
        return

    # ================= CHANGE ROOM =================
    if data == "change":
        show_room_menu(context, chat_id)
        return

    # ================= RENAME =================
    if data == "rename":
        if chat_id not in ADMINS:
            query.answer("❌ ไม่มีสิทธิ์", show_alert=True)
            return

        waiting_for_rename[chat_id] = True
        context.bot.send_message(chat_id=chat_id, text="✏ กรุณาพิมพ์ชื่อห้องใหม่:")
        return

    # ================= CHECK SELECT =================
    if chat_id not in user_selected_sensor:
        query.edit_message_text("❗ กรุณาเลือกห้องก่อน (/start)")
        return

    sid = user_selected_sensor[chat_id]
    sensor = get_sensor_info(sid) or {}
    room = sensor.get("name", sid)

    # ================= DELETE =================
    if data == "delete":
        if chat_id not in ADMINS:
            query.answer("❌ ไม่มีสิทธิ์", show_alert=True)
            return

        delete_sensor(sid)

        query.edit_message_text("🗑 ลบห้องเรียบร้อยแล้ว")
        show_room_menu(context, chat_id)
        return

    # ================= DATA =================
    if data == "dust":
        query.edit_message_text(f"🌫 {room}\n{sensor.get('dust')} µg/m³")

    elif data == "avg":
        res = get_average_dust(sid)
        if res:
            avg, count = res
            query.edit_message_text(
                f"📊 ค่าเฉลี่ยฝุ่น\n🏠 {room}\n🌫 {avg} µg/m³\n({count} รายการ)"
            )

    elif data == "graph":
        file = plot_graph(sid, "dust", "µg/m³", f"Dust History - {room}")
        if file:
            context.bot.send_photo(chat_id=chat_id, photo=open(file, "rb"))
            os.remove(file)
    
    elif data == "temp":
        query.edit_message_text(f"🌡 {room}\n{sensor.get('temperature')} °C")

    elif data == "hum":
        query.edit_message_text(f"💧 {room}\n{sensor.get('humidity')} %")

def text_handler(update, context):
    chat_id = update.message.chat.id
    print("Received text from", chat_id, ":", update.message.text)
    # 👇 add ทุกครั้ง (ชัวร์สุด)
    subscribers.add(chat_id)
    text = update.message.text.strip().lower()

    # ===== rename =====
    if waiting_for_rename.get(chat_id):
        if chat_id not in user_selected_sensor:
            update.message.reply_text("❗ กรุณาเลือกห้องก่อน")
            waiting_for_rename[chat_id] = False
            return

        sid = user_selected_sensor[chat_id]
        set_room_name(sid, text)
        waiting_for_rename[chat_id] = False

        update.message.reply_text(f"✅ เปลี่ยนชื่อห้องเป็น '{text}' แล้ว")
        show_room_menu(context, chat_id)
        return

    # ===== ต้องเลือกห้องก่อน =====
    if chat_id not in user_selected_sensor:
        update.message.reply_text("❗ กรุณาเลือกห้องก่อน (/start)")
        return

    sid = user_selected_sensor[chat_id]
    sensor = get_sensor_info(sid)
    room = sensor.get("name", sid)

    # ===== รับคำสั่งพิมพ์ =====
    if "ฝุ่น" in text or "dust" in text:
        update.message.reply_text(f"🌫 {room}\n{sensor.get('dust')} µg/m³")

    elif "อุณหภูมิ" in text or "temp" in text:
        update.message.reply_text(f"🌡 {room}\n{sensor.get('temperature')} °C")

    elif "ความชื้น" in text or "hum" in text:
        update.message.reply_text(f"💧 {room}\n{sensor.get('humidity')} %")
    
    elif "เปลี่ยนห้อง" in text:
        show_room_menu(context, chat_id)
    
    elif "เปลี่ยนชื่อ" in text:
        waiting_for_rename[chat_id] = True
        update.message.reply_text("✏ กรุณาพิมพ์ชื่อห้องใหม่:")
    
    elif "กราฟ" in text:
        file = plot_graph(sid, "dust", "µg/m³", f"Dust History - {room}")
        if file:
            context.bot.send_photo(chat_id=chat_id, photo=open(file, "rb"))
            os.remove(file)

    elif "ทั้งหมด" in text:
        update.message.reply_text(
            f"🏠 {room}\n"
            f"🌫 ฝุ่น: {sensor.get('dust')} µg/m³\n"
            f"🌡 อุณหภูมิ: {sensor.get('temperature')} °C\n"
            f"💧 ความชื้น: {sensor.get('humidity')} %"
        )

    else:
        update.message.reply_text("❓ พิมพ์ เช่น: ฝุ่น / อุณหภูมิ / ความชื้น / ทั้งหมด")
        
def test_command(update, context):
    print("ได้รับข้อความจาก Telegram:", update.message.text)
    update.message.reply_text("✅ บอทตอบกลับแล้ว!")
# ================= Register =================
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CallbackQueryHandler(button_handler))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, text_handler))
dispatcher.add_handler(CommandHandler("myid", myid))
dispatcher.add_handler(CommandHandler("admins", list_admins))
dispatcher.add_handler(CommandHandler("addadmin", add_admin))
dispatcher.add_handler(CommandHandler("removeadmin", remove_admin))

job_queue.run_repeating(dust_monitor, interval=60, first=10)

print("🤖 Bot is running...")
try:
    bot_info = updater.bot.get_me()
    print("เชื่อมต่อ Telegram สำเร็จ")
    print("Bot username:", bot_info.username)
except Exception as e:
    print("Telegram connection error:", repr(e))
    raise
updater.bot.delete_webhook()
dispatcher.add_handler(CommandHandler("test", test_command))
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("telegram").setLevel(logging.DEBUG)

def error_handler(update, context):
    print("❌ เกิดข้อผิดพลาดในบอท:")
    print(repr(context.error))

dispatcher.add_error_handler(error_handler)
updater.start_polling()
updater.idle()
