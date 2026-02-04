import os
import sys
import json
import time
import pickle
import hashlib
import logging
import requests
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


LAST_UPDATE_ID = 0
LAST_SLOTS_CHECK = 0
SLOTS_CHECK_INTERVAL = 180

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
USERNAME = os.getenv("BMSTU_LOGIN")
PASSWORD = os.getenv("BMSTU_PASSWORD")
SEMESTER_UUID = os.getenv("SEMESTER_UUID")

if not all([TELEGRAM_TOKEN, CHAT_ID, USERNAME, PASSWORD, SEMESTER_UUID]):
    logger.critical("Configuration error: Check .env file for missing variables.")
    sys.exit(1)

API_URL = f"https://lks.bmstu.ru/lks-back/api/v1/fv/{SEMESTER_UUID}/groups"
TARGET_URL = "https://lks.bmstu.ru/profile"
COOKIE_DIR = os.path.join(basedir, "cookies")
COOKIE_FILE = os.path.join(COOKIE_DIR, "bmstu_cookies.pkl")

KNOWN_SLOTS = set()


def send_telegram(text, parse_mode=None):
    try:
        data = {"chat_id": CHAT_ID, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
            data["disable_web_page_preview"] = "true"

        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, timeout=10
        )
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def update_cookies_via_selenium():
    """Выполняет авторизацию через Selenium headless-браузер для обновления сессии."""
    logger.info("Session expired. Initiating re-login via Selenium...")

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")

    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    system_driver = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(system_driver) if system_driver and os.path.exists(system_driver) else Service(ChromeDriverManager().install())

    driver = None
    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(TARGET_URL)
        wait = WebDriverWait(driver, 25)

        wait.until(EC.visibility_of_element_located((By.ID, "username"))).send_keys(USERNAME)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "kc-login").click()

        # Ожидание редиректа на профиль как признак успеха
        wait.until(EC.url_contains("lks.bmstu.ru/profile"))

        time.sleep(3) # Небольшая пауза для прогрузки cookies
        if not os.path.exists(COOKIE_DIR):
            os.makedirs(COOKIE_DIR)

        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(driver.get_cookies(), f)

        logger.info("Cookies successfully updated.")
    except Exception as e:
        logger.error(f"Selenium login failed: {e}")
    finally:
        if driver:
            driver.quit()


def get_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "rb") as f:
                for cookie in pickle.load(f):
                    session.cookies.set(cookie['name'], cookie['value'])
        except Exception as e:
            logger.warning(f"Could not load cookies: {e}")
    return session


def generate_slot_id(item):
    """Генерирует уникальный ID слота на основе ID API или хеша параметров."""
    if item.get('id'):
        return str(item.get('id'))

    parts = [
        str(item.get('week', '')),
        str(item.get('time', '')),
        str(item.get('teacherUid', '')),
        str(item.get('section', ''))
    ]
    return hashlib.md5("_".join(parts).encode()).hexdigest()


def format_message(new_items):
    """Формирует читаемое сообщение для Telegram."""
    msg_lines = ["<b>🔥 ДОСТУПНЫ НОВЫЕ СЛОТЫ!</b>\n"]

    for item in new_items:
        name = item.get('section') or "Тренировка"
        day = item.get('week') or "День недели"
        time_slot = item.get('time') or "??"
        place = item.get('place') or "СК МГТУ"
        teacher = item.get('teacherName') or "Преподаватель не указан"
        vacancy = item.get('vacancy', 0)

        card = (
            f"🏟 <b>{name}</b>\n"
            f"🗓  {day} |⏰  {time_slot}\n"
            f"📍  {place}\n"
            f"👨‍🏫  {teacher}\n"
            f"🟢  Свободно мест: <b>{vacancy}</b>"
        )
        msg_lines.append(card)

    return "\n\n".join(msg_lines)


def check_slots():
    global KNOWN_SLOTS
    session = get_session()

    try:
        response = session.get(API_URL, timeout=15)

        if response.status_code in [401, 403]:
            logger.warning("Access denied (401/403). Token expired.")
            update_cookies_via_selenium()
            return

        if response.status_code != 200:
            logger.error(f"API Error: Status {response.status_code}")
            return

        days_list = response.json()
        if not days_list:
            logger.debug("Received empty schedule list.")
            return

        current_slots_map = {}
        new_slots_data = []

        # Парсинг структуры: Список Дней -> Список Групп
        for day_data in days_list:
            groups = day_data.get('groups', [])
            for group in groups:
                slot_id = generate_slot_id(group)
                current_slots_map[slot_id] = group

                vacancy = int(group.get('vacancy', 0))
                if vacancy > 0:
                    if slot_id not in KNOWN_SLOTS:
                        new_slots_data.append(group)
                        KNOWN_SLOTS.add(slot_id)

        # Очистка старых ID из памяти (garbage collection)
        KNOWN_SLOTS.intersection_update(current_slots_map.keys())

        if new_slots_data:
            logger.info(f"Found {len(new_slots_data)} new slots. Sending notification.")
            text = format_message(new_slots_data)
            link = "https://lks.bmstu.ru/fv/new-record"
            full_text = f"{text}\n\n<a href='{link}'><b>ПЕРЕЙТИ К ЗАПИСИ</b></a>"
            send_telegram(full_text, parse_mode="HTML")
        else:
            logger.info("Check completed. No new slots found.")

    except Exception as e:
        logger.error(f"Unexpected error during check: {e}")


def get_all_available_slots():
    """Возвращает список всех доступных для записи слотов (vacancy > 0)."""
    session = get_session()
    slots = []

    try:
        response = session.get(API_URL, timeout=15)

        if response.status_code in [401, 403]:
            logger.warning("Access denied while fetching slots for /start.")
            update_cookies_via_selenium()
            return []

        if response.status_code != 200:
            logger.error(f"API Error while fetching slots: {response.status_code}")
            return []

        days_list = response.json() or []

        for day_data in days_list:
            for group in day_data.get('groups', []):
                if int(group.get('vacancy', 0)) > 0:
                    slots.append(group)

    except Exception as e:
        logger.error(f"Error fetching slots for /start: {e}")

    return slots


def handle_start_command():
    """Обрабатывает команду /start (мгновенный ответ)."""
    logger.info("Processing /start command")
    send_telegram(
        "Привет! Я слежу за свободными местами на физкультуру.\n\n"
        "Чтобы посмотреть список доступных записей прямо сейчас, нажмите /check"
    )


def handle_check_command():
    """Обрабатывает команду /check (запрос актуальных данных)."""
    logger.info("Processing /check command")

    # Можно отправить промежуточное сообщение, чтобы пользователь видел - процесс идет
    # send_telegram("🔍 Проверяю актуальные слоты, подождите...")

    slots = get_all_available_slots()

    if not slots:
        send_telegram("❌ На данный момент доступных записей нет.")
        return

    text = format_message(slots)
    link = "https://lks.bmstu.ru/fv/new-record"
    full_text = f"{text}\n\n<a href='{link}'><b>ПЕРЕЙТИ К ЗАПИСИ</b></a>"
    send_telegram(full_text, parse_mode="HTML")


def check_telegram_commands():
    global LAST_UPDATE_ID
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": LAST_UPDATE_ID + 1, "timeout": 1},
            timeout=5
        )
        if response.status_code == 200:
            updates = response.json().get("result", [])
            for update in updates:
                LAST_UPDATE_ID = update["update_id"]
                if "message" in update and "text" in update["message"]:
                    cmd = update["message"]["text"].strip().lower()

                    if cmd == "/start":
                        handle_start_command()
                    elif cmd == "/check":
                        handle_check_command()

    except Exception as e:
        logger.error(f"Error in commands thread: {e}")


def telegram_loop():
    """Бесконечный цикл для мгновенной обработки команд"""
    logger.info("Telegram command listener started.")
    while True:
        check_telegram_commands()
        time.sleep(0.5) # Минимальная пауза, чтобы не спамить CPU


def main():
    logger.info("Service started. Monitoring BMSTU slots.")

    # 1. Первичная авторизация
    if not os.path.exists(COOKIE_FILE):
        update_cookies_via_selenium()

    # 2. Запускаем обработку команд в ОТДЕЛЬНОМ потоке
    # Это позволит боту отвечать на /start мгновенно,
    # даже если основная программа занята проверкой слотов
    cmd_thread = threading.Thread(target=telegram_loop, daemon=True)
    cmd_thread.start()

    # 3. Основной цикл (только мониторинг слотов)
    while True:
        try:
            check_slots()
        except Exception as e:
            logger.error(f"Main loop error: {e}")

        # Ждем 3 минуты до следующей проверки
        time.sleep(SLOTS_CHECK_INTERVAL)


if __name__ == "__main__":
    main()
