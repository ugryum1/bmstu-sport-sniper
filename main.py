import requests
import time
import json
import os
import pickle
import sys
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# --- КОНФИГУРАЦИЯ ---
# Определяем путь к .env (для надежности в Docker и локально)
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

TELEGRAM_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
USERNAME = os.getenv("BMSTU_LOGIN")
PASSWORD = os.getenv("BMSTU_PASSWORD")
SEMESTER_UUID = os.getenv("SEMESTER_UUID")

# Проверка переменных
if not all([TELEGRAM_TOKEN, CHAT_ID, USERNAME, PASSWORD, SEMESTER_UUID]):
    print("❌ ОШИБКА: Проверь файл .env! Не все переменные заданы.")
    sys.exit(1)

# Настройки путей и URL
API_URL = f"https://lks.bmstu.ru/lks-back/api/v1/fv/{SEMESTER_UUID}/groups"
TARGET_URL = "https://lks.bmstu.ru/profile"  # Ссылка для триггера SSO
COOKIE_DIR = os.path.join(basedir, "cookies")
COOKIE_FILE = os.path.join(COOKIE_DIR, "bmstu_cookies.pkl")

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def update_cookies_via_selenium():
    """Запускает браузер, проходит SSO авторизацию и сохраняет куки"""
    print("🔄 Запускаю обновление кук через Selenium...")

    options = webdriver.ChromeOptions()
    # --- НАСТРОЙКИ ДЛЯ DOCKER ---
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    # ----------------------------

    # --- ИСПРАВЛЕННАЯ ИНИЦИАЛИЗАЦИЯ ДРАЙВЕРА ---

    # 1. Проверяем, задан ли путь к хрому вручную (через Docker env)
    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    # 2. Выбираем драйвер: либо системный (Docker), либо скачиваем (Локально)
    system_driver = os.environ.get("CHROMEDRIVER_PATH")

    if system_driver and os.path.exists(system_driver):
        print(f"🔧 Использую системный драйвер: {system_driver}")
        service = Service(system_driver)
    else:
        print("🔧 Скачиваю драйвер через Manager (Локальный режим)...")
        service = Service(ChromeDriverManager().install())

    # 3. Запускаем
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"❌ Критическая ошибка запуска Chrome: {e}")
        raise e

    try:
        # 1. Идем на защищенную страницу -> нас редиректит на SSO
        print(f"Переход на {TARGET_URL}...")
        driver.get(TARGET_URL)
        wait = WebDriverWait(driver, 20)

        # 2. Ждем форму входа SSO (появление поля username)
        print("Жду форму SSO...")
        username_input = wait.until(EC.visibility_of_element_located((By.ID, "username")))

        # 3. Вводим данные
        username_input.clear()
        username_input.send_keys(USERNAME)

        password_input = driver.find_element(By.ID, "password")
        password_input.clear()
        password_input.send_keys(PASSWORD)

        # 4. Жмем войти
        login_btn = driver.find_element(By.ID, "kc-login")
        login_btn.click()
        print("Данные введены, вход...")

        # 5. Ждем обратного редиректа в ЛК
        wait.until(EC.url_contains("lks.bmstu.ru/profile"))

        # Небольшая пауза для прогрузки JS и кук
        time.sleep(3)

        # 6. Сохраняем куки
        if not os.path.exists(COOKIE_DIR):
            os.makedirs(COOKIE_DIR)

        pickle.dump(driver.get_cookies(), open(COOKIE_FILE, "wb"))
        print("✅ Куки успешно обновлены и сохранены!")

    except Exception as e:
        print(f"❌ Ошибка в процессе Selenium: {e}")
        # Можно отправить алерт в телеграм, если это не первый запуск
        if os.path.exists(COOKIE_FILE):
             send_telegram(f"⚠️ Ошибка обновления кук: {e}")
    finally:
        driver.quit()

def get_session():
    """Создает сессию requests с загрузкой кук"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://lks.bmstu.ru/fv/new-record'
    })

    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "rb") as f:
                cookies = pickle.load(f)
                for cookie in cookies:
                    session.cookies.set(cookie['name'], cookie['value'])
        except Exception as e:
            print(f"Ошибка чтения файла кук: {e}")
    else:
        print("Файл кук не найден.")

    return session

def check_slots():
    session = get_session()
    print(f"[{time.strftime('%H:%M:%S')}] Проверка API...")

    try:
        response = session.get(API_URL, timeout=15)

        # Если 401/403 -> куки протухли
        if response.status_code in [401, 403]:
            print("🔐 Куки истекли. Запускаю обновление...")
            update_cookies_via_selenium()
            return False, "COOKIES_UPDATED"

        if response.status_code == 200:
            data = response.json()
            if len(data) > 0:
                # Нашли места
                info = json.dumps(data, ensure_ascii=False, indent=2)
                return True, info
            else:
                return False, None
        else:
            print(f"⚠️ Странный ответ API: {response.status_code}")
            return False, None

    except Exception as e:
        print(f"Ошибка сети: {e}")
        return False, None

def main():
    print(f"🚀 Бот запущен для: {USERNAME}")
    send_telegram(f"Снайпер запущен. Цель: {SEMESTER_UUID}")

    # При старте, если кук нет, пробуем получить сразу
    if not os.path.exists(COOKIE_FILE):
        update_cookies_via_selenium()

    while True:
        found, msg = check_slots()

        if msg == "COOKIES_UPDATED":
            time.sleep(10) # Даем время на запись файла
            continue

        if found:
            link = "https://lks.bmstu.ru/fv/new-record"
            text = f"🚨 <b>НАЙДЕНЫ МЕСТА!</b> 🚨\n\nДанные: {msg}\n\n👉 <a href='{link}'>ЗАПИСАТЬСЯ</a>"
            # Используем HTML парсинг для красивой ссылки
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
                )
            except:
                send_telegram(f"НАЙДЕНЫ МЕСТА! Ссылка: {link}")

            time.sleep(600) # Пауза 10 мин после успеха

        # Пауза между проверками (чтобы не забанили)
        time.sleep(45)

if __name__ == "__main__":
    main()
