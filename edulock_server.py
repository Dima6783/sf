#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EduLock SERVER — серверная часть (отдельная программа).
Запускается на ЛЮБОМ сервере (VPS, отдельный ПК, Linux/Windows), клиенты подключаются по сети.
Языки интерфейса: русский / English / Türkçe (переключатель в углу страниц, язык по умолчанию — в Настройках).

УСТАНОВКА:
    pip install fastapi uvicorn "qrcode[pil]" pillow websockets

ЗАПУСК С ОКНОМ (Windows / ПК с экраном):
    python edulock_server.py

РЕЖИМ VPS (Linux-сервер без экрана — для блокировки умных досок):
    python3 edulock_server.py --vps
        без окна; при первом запуске создаёт случайные пароль админа и ключ клиентов,
        сохраняет их в edulock_config.json и печатает адреса панели и досок.
    sudo python3 edulock_server.py --vps --install-service
        ставит systemd-службу: сервер стартует вместе с VPS и перезапускается при сбое.
    python3 edulock_server.py --show-config       показать адреса, пароль и ключ
    Параметры: --port 8000  --host 0.0.0.0  --lang ru|en|tr  --public-url https://домен

ЗАПУСК БЕЗ ОКНА (ручной, Linux VPS):
    export ADMIN_PASS=пароль_админа       (Windows: set ADMIN_PASS=...)
    export CLIENT_KEY=ключ_для_клиентов   (этот же ключ вводится в клиенте)
    python edulock_server.py --nogui --port 8000

НАСТРОЙКИ СОХРАНЯЮТСЯ: порт, пароль админа, ключ клиентов, язык и публичный адрес лежат в
edulock_config.json (рядом со скриптом); школа, логотип, преподаватели и т.д. — в edulock.db.
Приоритет: сохранённые настройки > переменные окружения > значения по умолчанию.

    Панель администратора:  http://АДРЕС_СЕРВЕРА:8000/admin
    Доска / ПК учителя:     http://АДРЕС_СЕРВЕРА:8000/board?pc=PC-01
    Вход учителя (QR):      http://АДРЕС_СЕРВЕРА:8000/t/PC-01

Для интернета поставьте перед сервером nginx/Caddy с HTTPS (клиенты тогда указывают https://домен).
"""
import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time

import contextvars

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.abspath(os.environ.get("EDULOCK_CONFIG", os.path.join(BASE_DIR, "edulock_config.json")))
TOKEN_TTL = 30 * 86400  # токен админа живёт 30 дней (хранится в базе, переживает перезапуск)


def load_config():
    """Сохранённые настройки сервера (порт, пароли, язык, адрес) — файл edulock_config.json."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(**kw):
    """Дописывает/меняет значения в файле настроек (None — не трогать)."""
    cfg = load_config()
    cfg.update({k: v for k, v in kw.items() if v is not None})
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)  # в файле пароли — только владельцу
    except OSError:
        pass
    os.replace(tmp, CONFIG_PATH)
    return cfg


_cfg = load_config()
# приоритет: сохранённые настройки (файл) > переменные окружения > значения по умолчанию
DB_PATH = os.path.abspath(os.environ.get("EDULOCK_DB", os.path.join(BASE_DIR, "edulock.db")))
ADMIN_PASS = _cfg.get("admin_pass") or os.environ.get("ADMIN_PASS") or "admin"
CLIENT_KEY = _cfg.get("client_key") or os.environ.get("CLIENT_KEY") or "edulock-key"  # общий ключ клиентов
DEFAULT_LANG = _cfg.get("lang") or os.environ.get("EDULOCK_LANG") or "ru"
PUBLIC_URL = (_cfg.get("public_url") or os.environ.get("EDULOCK_PUBLIC_URL") or "").rstrip("/")
MEDIA_DIR = os.path.abspath(os.environ.get("EDULOCK_MEDIA", os.path.join(BASE_DIR, "edulock_media")))
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VID_EXT = {".mp4", ".webm", ".mov", ".m4v"}
MAX_MEDIA = 300 * 1024 * 1024  # 300 МБ на файл
DAYN = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
STATS = {"start": 0.0, "boards": {}, "panel_hits": 0, "last_panel": 0.0, "conns": {}}
SERVER = {"srv": None}

# ====================================================================
#                     ЯЗЫКИ: русский / English / Türkçe
# ====================================================================
LANGS = {"ru": "Русский", "en": "English", "tr": "Türkçe"}
if DEFAULT_LANG not in LANGS:
    DEFAULT_LANG = "ru"
LANG = contextvars.ContextVar("lang", default="ru")  # язык текущего запроса (из cookie lang)
DAYS = {"ru": DAYN,
        "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "tr": ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]}

# Сообщения API (ключ — русский текст, значение — (English, Türkçe)). Шаблоны с {x} — для .format()
TR = {
    "Доступ запрещён: преподаватель не отмечен как работающий": (
        "Access denied: teacher is not marked as working",
        "Erişim reddedildi: öğretmen çalışıyor olarak işaretlenmemiş"),
    "Срок временного доступа истёк": ("Temporary access has expired", "Geçici erişim süresi doldu"),
    "Неверный код устройства": ("Invalid device code", "Geçersiz cihaz kodu"),
    "Преподаватель не найден в базе данных": ("Teacher not found in the database",
                                              "Öğretmen veritabanında bulunamadı"),
    "Неверный пароль": ("Wrong password", "Yanlış parola"),
    "Неверный логин или пароль": ("Wrong login or password", "Yanlış kullanıcı adı veya parola"),
    "Данные профиля неполные: {f}. Обратитесь к администратору.": (
        "Profile data is incomplete: {f}. Contact the administrator.",
        "Profil bilgileri eksik: {f}. Yöneticiye başvurun."),
    "год рождения": ("birth year", "doğum yılı"),
    "год начала преподавания": ("year teaching started", "öğretmenliğe başlama yılı"),
    "предмет": ("subject", "ders"),
    "телефон": ("phone", "telefon"),
    "Нужна авторизация": ("Authorization required", "Yetkilendirme gerekli"),
    "Укажите пароль": ("Enter a password", "Parola girin"),
    "Такой логин уже существует": ("This login already exists", "Bu kullanıcı adı zaten var"),
    "Неверное время урока": ("Invalid lesson time", "Geçersiz ders saati"),
    "Укажите кабинет": ("Enter a room", "Derslik girin"),
    "Неверный день недели": ("Invalid weekday", "Geçersiz gün"),
    "Неверная дата или время экзамена": ("Invalid exam date or time", "Geçersiz sınav tarihi veya saati"),
    "Укажите название и кабинет": ("Enter a title and a room", "Başlık ve derslik girin"),
    "Допустимые форматы: jpg, png, gif, webp, mp4, webm, mov": (
        "Allowed formats: jpg, png, gif, webp, mp4, webm, mov",
        "İzin verilen biçimler: jpg, png, gif, webp, mp4, webm, mov"),
    "Файл больше 300 МБ": ("File is larger than 300 MB", "Dosya 300 MB sınırını aşıyor"),
    "Пароль администратора должен быть не короче 4 символов": (
        "Administrator password must be at least 4 characters",
        "Yönetici parolası en az 4 karakter olmalı"),
    "не работает": ("not working", "çalışmıyor"),
    "доступ истёк": ("access expired", "erişim süresi doldu"),
    "до ": ("until ", "şu tarihe kadar "),
    "работает": ("working", "çalışıyor"),
    "Школа": ("School", "Okul"),
    "{n}: не заполнено — {f}": ("{n}: missing — {f}", "{n}: eksik — {f}"),
    "Повторяется ФИО в базе: {n}": ("Duplicate full name in the database: {n}",
                                    "Veritabanında yinelenen ad soyad: {n}"),
    "{n}: есть уроки в расписании, но доступа нет ({w})": (
        "{n}: has lessons in the schedule but no access ({w})",
        "{n}: programda dersi var ama erişimi yok ({w})"),
    "Урок {d} {s} ({r}): преподаватель не найден в базе": (
        "Lesson {d} {s} ({r}): teacher not found in the database",
        "Ders {d} {s} ({r}): öğretmen veritabanında yok"),
    "{d} {a1}–{a2} и {b1}–{b2}": ("{d} {a1}–{a2} and {b1}–{b2}", "{d} {a1}–{a2} ve {b1}–{b2}"),
    "Кабинет {r}: пересечение уроков {w}": ("Room {r}: lessons overlap {w}",
                                            "Derslik {r}: ders çakışması {w}"),
    "{n}: два урока одновременно ({w})": ("{n}: two lessons at the same time ({w})",
                                          "{n}: aynı anda iki ders ({w})"),
    "Устройство {p}: не назначен кабинет — расписание не действует": (
        "Device {p}: no room assigned — the schedule does not apply",
        "Cihaz {p}: derslik atanmamış — program uygulanmaz"),
    "Устройство {p} ({r}) сейчас не в сети": ("Device {p} ({r}) is offline",
                                              "Cihaz {p} ({r}) şu anda çevrimdışı"),
    "Проверено: преподавателей {t}, уроков {l}, устройств {p}. Ошибок: {e}": (
        "Checked: teachers {t}, lessons {l}, devices {p}. Errors: {e}",
        "Kontrol edildi: öğretmen {t}, ders {l}, cihaz {p}. Hata: {e}"),
}

# Записи журнала и подписи, которые хранятся в базе по-русски и переводятся при показе
TR_EV = {
    "вход: нет в базе": ("login: not in database", "giriş: veritabanında yok"),
    "вход: неверный пароль": ("login: wrong password", "giriş: yanlış parola"),
    "вход: успешно": ("login: success", "giriş: başarılı"),
    "вход: ": ("login: ", "giriş: "),
    "блокировка преподавателем": ("locked by teacher", "öğretmen tarafından kilitlendi"),
    "расписание: блокировка": ("schedule: lock", "program: kilitleme"),
    "расписание: разблокировка": ("schedule: unlock", "program: kilit açma"),
    "расписание: урок не открыт — у преподавателя нет доступа": (
        "schedule: lesson not opened — teacher has no access",
        "program: ders açılmadı — öğretmenin erişimi yok"),
    "Доступ запрещён: преподаватель не отмечен как работающий": TR[
        "Доступ запрещён: преподаватель не отмечен как работающий"],
    "Срок временного доступа истёк": TR["Срок временного доступа истёк"],
    "Урок:": ("Lesson:", "Ders:"),
    "Экзамен:": ("Exam:", "Sınav:"),
    "администратор": ("administrator", "yönetici"),
}

# Тексты веб-страниц (панель, доска, вход, приложение). ВАЖНО: без апострофов в переводах (они внутри JS).
TR_UI = {
    "Доска": ("Board", "Tahta"),
    "Вход преподавателя": ("Teacher login", "Öğretmen girişi"),
    "EduLock — панель": ("EduLock — panel", "EduLock — panel"),
    "EduLock — преподаватель": ("EduLock — teacher", "EduLock — öğretmen"),
    "Устройство __PC__ заблокировано": ("Device __PC__ is locked", "__PC__ cihazı kilitli"),
    "Работает: ": ("In use: ", "Kullanımda: "),
    "Код: ": ("Code: ", "Kod: "),
    "Отсканируйте QR-код или введите код в приложении": (
        "Scan the QR code or enter the code in the app",
        "QR kodu tarayın veya kodu uygulamaya girin"),
    "Компьютер: ": ("Computer: ", "Bilgisayar: "),
    "Логин": ("Login", "Kullanıcı adı"),
    "Пароль администратора": ("Administrator password", "Yönetici parolası"),
    "Пароль": ("Password", "Parola"),
    "Войти и разблокировать": ("Sign in and unlock", "Giriş yap ve kilidi aç"),
    "Войти": ("Sign in", "Giriş"),
    "Заблокировать компьютер": ("Lock the computer", "Bilgisayarı kilitle"),
    "Ошибка: ": ("Error: ", "Hata: "),
    "Ошибка": ("Error", "Hata"),
    "Добро пожаловать, ": ("Welcome, ", "Hoş geldiniz, "),
    "🔒 Заблокировано": ("🔒 Locked", "🔒 Kilitlendi"),
    "Заблокировано": ("Locked", "Kilitlendi"),
    "Неверный пароль": ("Wrong password", "Yanlış parola"),
    # вкладки и заголовки
    "Устройства": ("Devices", "Cihazlar"),
    "Преподаватели": ("Teachers", "Öğretmenler"),
    "Расписание уроков": ("Lesson schedule", "Ders programı"),
    "Расписание": ("Schedule", "Program"),
    "Экзамены": ("Exams", "Sınavlar"),
    "Сводка": ("Summary", "Özet"),
    "Объявления": ("Announcements", "Duyurular"),
    "Настройки": ("Settings", "Ayarlar"),
    "Журнал": ("Log", "Günlük"),
    # устройства
    "ПК онлайн": ("PCs online", "Çevrimiçi PC"),
    "Умные доски": ("Smart boards", "Akıllı tahtalar"),
    "Всего часов работы": ("Total hours of operation", "Toplam çalışma saati"),
    "Текущая сессия": ("Current session", "Mevcut oturum"),
    "} ч</b>": ("} h</b>", "} sa</b>"),
    "🔒 Все": ("🔒 All", "🔒 Tümü"),
    "🔓 Все": ("🔓 All", "🔓 Tümü"),
    "🔒 Заблокирован'": ("🔒 Locked'", "🔒 Kilitli'"),
    "Открыт": ("Open", "Açık"),
    "🖥 доска": ("🖥 board", "🖥 tahta"),
    "💻 ПК": ("💻 PC", "💻 PC"),
    "Кабинет (для расписания)": ("Room (for the schedule)", "Derslik (program için)"),
    "Компьютер": ("Computer", "Bilgisayar"),
    "Умная доска": ("Smart board", "Akıllı tahta"),
    "Блок": ("Lock", "Kilitle"),
    "Откр.": ("Open", "Aç"),
    "Убрать устройство из списка?": ("Remove the device from the list?", "Cihaz listeden kaldırılsın mı?"),
    # преподаватели
    "Проверить данные": ("Check data", "Verileri kontrol et"),
    "Проверка данных": ("Data check", "Veri kontrolü"),
    "Добавить урок": ("Add lesson", "Ders ekle"),
    "Добавить": ("Add", "Ekle"),
    "Предмет: ": ("Subject: ", "Ders: "),
    "Год рожд.: ": ("Birth year: ", "Doğum yılı: "),
    "Преподаёт с: ": ("Teaching since: ", "Öğretmenlik başlangıcı: "),
    "Тел: ": ("Tel: ", "Tel: "),
    "Преподаёт с года": ("Teaching since (year)", "Öğretmenliğe başlama yılı"),
    "Год рождения": ("Birth year", "Doğum yılı"),
    "Телефон": ("Phone", "Telefon"),
    "Предмет": ("Subject", "Ders"),
    "ФИО": ("Full name", "Ad soyad"),
    "Новый пароль (пусто = не менять)": ("New password (empty = keep)", "Yeni parola (boş = değiştirme)"),
    "не работает": ("not working", "çalışmıyor"),
    "временный доступ истёк": ("temporary access expired", "geçici erişim süresi doldu"),
    "временный доступ до ": ("temporary access until ", "geçici erişim şu tarihe kadar: "),
    "Преподаватель работает (допуск к входу)": ("Teacher is working (allowed to sign in)",
                                                "Öğretmen çalışıyor (girişe izinli)"),
    "работает": ("working", "çalışıyor"),
    "Не хватает данных: ": ("Missing data: ", "Eksik bilgi: "),
    "Временный доступ: на сколько часов? (например 8 или 24). 0 = постоянный доступ без срока": (
        "Temporary access: for how many hours? (e.g. 8 or 24). 0 = permanent access without expiry",
        "Geçici erişim: kaç saat? (örn. 8 veya 24). 0 = süresiz kalıcı erişim"),
    "Временный доступ": ("Temporary access", "Geçici erişim"),
    "Редактирование": ("Editing", "Düzenleme"),
    "Новый преподаватель": ("New teacher", "Yeni öğretmen"),
    "Сохранить": ("Save", "Kaydet"),
    "Сохранено": ("Saved", "Kaydedildi"),
    "Отмена": ("Cancel", "İptal"),
    "Удалить преподавателя?": ("Delete the teacher?", "Öğretmen silinsin mi?"),
    # объявления
    "Текст объявления (необязательно, если добавляете фото или видео)": (
        "Announcement text (optional if you add a photo or video)",
        "Duyuru metni (fotoğraf veya video eklerseniz isteğe bağlı)"),
    "Опубликовать": ("Publish", "Yayınla"),
    "Загрузка ": ("Uploading ", "Yükleniyor "),
    # настройки
    "Название школы": ("School name", "Okul adı"),
    "Логотип:": ("Logo:", "Logo:"),
    "Выйти": ("Sign out", "Çıkış"),
    "Язык по умолчанию:": ("Default language:", "Varsayılan dil:"),
    "Публичный адрес сервера (для QR-кодов на досках)": (
        "Public server address (used for QR codes on boards)",
        "Genel sunucu adresi (tahtalardaki QR kodları için)"),
    "Ключ клиентов (для программ на ПК и досках)": (
        "Client key (used by the PC / board client program)",
        "İstemci anahtarı (PC / tahta programı için)"),
    "Новый пароль администратора (пусто = не менять)": (
        "New administrator password (empty = keep)", "Yeni yönetici parolası (boş = değiştirme)"),
    "Настройки сохраняются на сервере и остаются после перезапуска.": (
        "Settings are stored on the server and survive restarts.",
        "Ayarlar sunucuda saklanır ve yeniden başlatmadan sonra korunur."),
    "Ключ клиентов изменён — введите новый ключ в клиентских программах": (
        "Client key changed — enter the new key in the client programs",
        "İstemci anahtarı değişti — yeni anahtarı istemci programlarına girin"),
    # расписание и экзамены
    "Кабинет:": ("Room:", "Derslik:"),
    "Во время урока устройства кабинета разблокируются автоматически, на перемене и после уроков блокируются.": (
        "During a lesson the room devices unlock automatically; during breaks and after lessons they lock.",
        "Ders sırasında dersliğin cihazları otomatik açılır; teneffüste ve derslerden sonra kilitlenir."),
    "В этом кабинете пока нет уроков": ("There are no lessons in this room yet",
                                        "Bu dersliğde henüz ders yok"),
    "Кабинет (как в списке устройств)": ("Room (as in the device list)", "Derslik (cihaz listesindeki gibi)"),
    "Класс / группа (например 9А)": ("Class / group (e.g. 9A)", "Sınıf / grup (örn. 9A)"),
    "Все уроки": ("All lessons", "Tüm dersler"),
    "Выберите дни": ("Select days", "Gün seçin"),
    "— не найден —": ("— not found —", "— bulunamadı —"),
    "Кабинет": ("Room", "Derslik"),
    "Создать экзамен": ("Create exam", "Sınav oluştur"),
    "Название (например: Алгебра, 9А)": ("Title (e.g. Algebra, 9A)", "Başlık (örn. Cebir, 9A)"),
    "На время экзамена устройства кабинета разблокируются, а расписание уроков в это время не действует.": (
        "During an exam the room devices are unlocked and the lesson schedule does not apply.",
        "Sınav süresince dersliğin cihazlarının kilidi açılır ve ders programı geçerli olmaz."),
    # сводка
    "Все преподаватели и уроки": ("All teachers and lessons", "Tüm öğretmenler ve dersler"),
    "Преподаватель": ("Teacher", "Öğretmen"),
    "Статус": ("Status", "Durum"),
    "Уроков/нед": ("Lessons/wk", "Ders/hf"),
    "Часов/нед": ("Hours/wk", "Saat/hf"),
    "День": ("Day", "Gün"),
    "Начало": ("Start", "Başlangıç"),
    "Конец": ("End", "Bitiş"),
    "Класс": ("Class", "Sınıf"),
    "['Пн','Вт','Ср','Чт','Пт','Сб','Вс']": (
        "['Mon','Tue','Wed','Thu','Fri','Sat','Sun']", "['Pzt','Sal','Çar','Per','Cum','Cmt','Paz']"),
    # приложение преподавателя (телефон)
    "Разблокировка умной доски и компьютера": ("Unlock a smart board or computer",
                                               "Akıllı tahtanın veya bilgisayarın kilidini aç"),
    "Запомнить логин на этом телефоне": ("Remember login on this phone",
                                         "Kullanıcı adını bu telefonda hatırla"),
    "Код с экрана": ("Code from the screen", "Ekrandaki kod"),
    "Разблокировать по коду": ("Unlock by code", "Kodla kilidi aç"),
    "Сканировать QR-код": ("Scan QR code", "QR kodu tara"),
    "Заблокировать по коду": ("Lock by code", "Kodla kilitle"),
    "Введите 6-значный код с экрана": ("Enter the 6-digit code from the screen",
                                       "Ekrandaki 6 haneli kodu girin"),
    "Это не QR-код EduLock": ("This is not an EduLock QR code", "Bu bir EduLock QR kodu değil"),
    "Сканер недоступен в этом браузере или по http. Введите код с экрана либо наведите обычную камеру телефона на QR-код.": (
        "The scanner is not available in this browser or over http. Enter the code from the screen or point the normal phone camera at the QR code.",
        "Tarayıcı bu tarayıcıda veya http üzerinde kullanılamıyor. Ekrandaki kodu girin veya telefon kamerasını QR koduna tutun."),
    "Нет доступа к камере (для камеры нужен https). Введите код с экрана.": (
        "No camera access (the camera requires https). Enter the code from the screen.",
        "Kamera erişimi yok (kamera için https gerekir). Ekrandaki kodu girin."),
}

_UI_SORTED = sorted(TR_UI, key=len, reverse=True)
_EV_SORTED = sorted(TR_EV, key=len, reverse=True)


def _li():
    return {"en": 0, "tr": 1}.get(LANG.get())


def tr(s):
    """Перевод сообщения по языку текущего запроса."""
    i = _li()
    if i is None:
        return s
    v = TR.get(s)
    return v[i] if v else s


def loc(s):
    """Перевод записей журнала и подписей, хранящихся в базе по-русски."""
    i = _li()
    if i is None or not s:
        return s
    for k in _EV_SORTED:
        s = s.replace(k, TR_EV[k][i])
    return s


def dn(i):
    return DAYS[LANG.get()][i]


def school_name():
    return (get_setting("school", "") or "").strip() or tr("Школа")


def lang_switch(lg):
    opts = "".join(f'<option value={k}{" selected" if k == lg else ""}>{v}</option>' for k, v in LANGS.items())
    return ('<div style="position:fixed;right:12px;bottom:12px;z-index:99;opacity:.92">'
            '<select aria-label=Language onchange="document.cookie=\'lang=\'+this.value+\';path=/;max-age=31536000\';'
            'location.reload()" style="width:auto;margin:0;padding:6px 10px;font-size:13px;border-radius:12px">'
            + opts + '</select></div>')


def page(html):
    """HTML-страница на языке запроса + переключатель языка."""
    lg = LANG.get()
    html = html.replace("<html lang=ru>", f"<html lang={lg}>")
    i = _li()
    if i is not None:
        for k in _UI_SORTED:
            html = html.replace(k, TR_UI[k][i])
    return html.replace("</body>", lang_switch(lg) + "</body>", 1)


class LangMW:
    """ASGI-прослойка: язык запроса берётся из cookie lang (иначе — язык по умолчанию сервера)."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ck = ""
            for k, v in scope.get("headers", []):
                if k == b"cookie":
                    ck = v.decode("latin-1")
                    break
            m = re.search(r"(?:^|;\s*)lang=(ru|en|tr)\b", ck)
            tok = LANG.set(m.group(1) if m else DEFAULT_LANG)
            try:
                await self.app(scope, receive, send)
            finally:
                LANG.reset(tok)
        else:
            await self.app(scope, receive, send)


# ====================================================================
#                              СЕРВЕР
# ====================================================================
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    try:
        salt, _ = stored.split("$")
        return secrets.compare_digest(hash_pw(pw, salt), stored)
    except Exception:
        return False


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS teachers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT UNIQUE NOT NULL, pw TEXT NOT NULL,
        full_name TEXT NOT NULL, birth_year INTEGER, teach_since INTEGER,
        subject TEXT, phone TEXT, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS pcs(
        id TEXT PRIMARY KEY, locked INTEGER DEFAULT 1,
        last_seen REAL DEFAULT 0, teacher TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS announcements(
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, pc TEXT, teacher TEXT, event TEXT);
    CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY AUTOINCREMENT, weekday INTEGER, start_t TEXT, end_t TEXT,
        room TEXT, teacher_id INTEGER, subject TEXT, class_name TEXT);
    CREATE TABLE IF NOT EXISTS exams(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, day TEXT, start_t TEXT, end_t TEXT,
        room TEXT, teacher_id INTEGER);
    CREATE TABLE IF NOT EXISTS tokens(tk TEXT PRIMARY KEY, created REAL);
    """)
    for tbl, col, ddl in [("teachers", "access_until", "REAL"),
                          ("pcs", "room", "TEXT DEFAULT ''"),
                          ("pcs", "code", "TEXT DEFAULT ''"),
                          ("pcs", "kind", "TEXT DEFAULT 'pc'"),
                          ("announcements", "media", "TEXT DEFAULT ''"),
                          ("announcements", "kind", "TEXT DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    c.commit()
    c.close()


def get_setting(k, default=""):
    c = db()
    r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    c.close()
    return r["v"] if r else default


def set_setting(k, v):
    c = db()
    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
    c.commit()
    c.close()


def add_log(pc, teacher, event):
    c = db()
    c.execute("INSERT INTO log(ts,pc,teacher,event) VALUES(?,?,?,?)",
              (time.time(), pc, teacher, event))
    c.commit()
    c.close()


def missing_fields(t):
    """Какие данные профиля преподавателя не заполнены."""
    names = {"birth_year": "год рождения", "teach_since": "год начала преподавания",
             "subject": "предмет", "phone": "телефон"}
    return [tr(n) for k, n in names.items() if not t[k]]


LESSON_SEL = ("SELECT id, weekday, start_t AS start, end_t AS end, room, teacher_id, subject, "
              "class_name FROM lessons")
EXAM_SEL = "SELECT id, title, day AS date, start_t AS start, end_t AS end, room, teacher_id FROM exams"


def teacher_allowed(t):
    """Можно ли преподавателю входить: работает и срок временного доступа не истёк."""
    if not t["active"]:
        return False, "Доступ запрещён: преподаватель не отмечен как работающий"
    if t["access_until"] and time.time() > t["access_until"]:
        return False, "Срок временного доступа истёк"
    return True, ""


def new_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def to_min(s):
    return int(s[:2]) * 60 + int(s[3:5])


def room_state(room):
    """Что должно быть в кабинете сейчас по расписанию. None — кабинет не управляется расписанием."""
    now = time.localtime()
    hm, today = time.strftime("%H:%M", now), time.strftime("%Y-%m-%d", now)
    c = db()
    try:
        if not (c.execute("SELECT 1 FROM lessons WHERE room=?", (room,)).fetchone()
                or c.execute("SELECT 1 FROM exams WHERE room=?", (room,)).fetchone()):
            return None
        row = c.execute("""SELECT e.title AS name, t.full_name, t.active, t.access_until, e.teacher_id
            FROM exams e LEFT JOIN teachers t ON t.id=e.teacher_id
            WHERE e.room=? AND e.day=? AND e.start_t<=? AND ?<e.end_t""", (room, today, hm, hm)).fetchone()
        kind = "Экзамен"
        if not row:
            row = c.execute("""SELECT l.subject AS name, t.full_name, t.active, t.access_until, l.teacher_id
                FROM lessons l LEFT JOIN teachers t ON t.id=l.teacher_id
                WHERE l.room=? AND l.weekday=? AND l.start_t<=? AND ?<l.end_t""",
                            (room, now.tm_wday, hm, hm)).fetchone()
            kind = "Урок"
        if not row:
            return {"locked": True, "teacher": "", "label": ""}
        if row["teacher_id"] and not teacher_allowed(row)[0]:
            return {"locked": True, "teacher": "", "label": "", "denied": row["full_name"] or "?"}
        label = f"{kind}: {row['name'] or ''}".strip()
        who = row["full_name"] or ""
        return {"locked": False, "teacher": (who + " · " if who else "") + label, "label": label}
    finally:
        c.close()


def boards_online():
    """Умные доски онлайн: клиенты с типом 'доска' + открытые страницы /board."""
    now = time.time()
    ids = {p for p, t in STATS["boards"].items() if now - t < 20}
    c = db()
    for p in list(STATS["conns"]):
        r = c.execute("SELECT kind FROM pcs WHERE id=?", (p,)).fetchone()
        if r and r["kind"] == "board":
            ids.add(p)
    c.close()
    return len(ids)


def run_server(host, port):
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Request
        from fastapi.responses import HTMLResponse, Response, JSONResponse
        from fastapi.staticfiles import StaticFiles
        from urllib.parse import unquote
        import uvicorn
        import qrcode
        import qrcode.image.svg
    except ImportError:
        sys.exit('Установите: pip install fastapi uvicorn "qrcode[pil]" pillow websockets')

    init_db()
    app = FastAPI(title="EduLock")
    app.add_middleware(LangMW)
    conns = {}          # pc_id -> websocket
    STATS['conns'] = conns
    STATS['start'] = time.time()
    def need_admin(x_token):
        c = db()
        r = c.execute("SELECT created FROM tokens WHERE tk=?", (x_token or "",)).fetchone()
        c.close()
        if not r or time.time() - r["created"] > TOKEN_TTL:
            raise HTTPException(401, tr("Нужна авторизация"))

    async def push(pc_id):
        ws = conns.get(pc_id)
        if not ws:
            return
        c = db()
        r = c.execute("SELECT locked, teacher, code FROM pcs WHERE id=?", (pc_id,)).fetchone()
        ann = [{"text": a["text"] or "", "media": a["media"] or "", "kind": a["kind"] or ""}
               for a in c.execute("SELECT text, media, kind FROM announcements ORDER BY id DESC LIMIT 8")]
        c.close()
        try:
            await ws.send_text(json.dumps({
                "locked": bool(r["locked"]) if r else True,
                "teacher": r["teacher"] if r else "",
                "announcements": ann,
                "code": (r["code"] or "") if r else "",
                "school": school_name()}))
        except Exception:
            conns.pop(pc_id, None)

    async def set_lock(pc_id, locked, teacher=""):
        c = db()
        c.execute("INSERT OR IGNORE INTO pcs(id) VALUES(?)", (pc_id,))
        if locked:  # новый код при каждой блокировке (если уже была заблокирована — код сохраняем)
            c.execute("""UPDATE pcs SET locked=1, teacher='',
                         code=CASE WHEN locked=1 AND code!='' THEN code ELSE ? END WHERE id=?""",
                      (new_code(), pc_id))
        else:
            c.execute("UPDATE pcs SET locked=0, teacher=? WHERE id=?", (teacher, pc_id))
        c.commit()
        c.close()
        await push(pc_id)

    # ---------------- WebSocket клиентов ----------------
    @app.websocket("/ws/{pc_id}")
    async def ws_pc(ws: WebSocket, pc_id: str, key: str = "", kind: str = "pc", room: str = ""):
        if key != CLIENT_KEY:
            await ws.close(code=4001)
            return
        await ws.accept()
        conns[pc_id] = ws
        c = db()
        c.execute("INSERT OR IGNORE INTO pcs(id, kind) VALUES(?,?)",
                  (pc_id, "board" if kind == "board" else "pc"))
        if room:
            c.execute("UPDATE pcs SET room=? WHERE id=? AND (room IS NULL OR room='')", (room, pc_id))
        prev = c.execute("SELECT last_seen FROM pcs WHERE id=?", (pc_id,)).fetchone()
        was_away = (not prev) or (time.time() - (prev["last_seen"] or 0) > 90)
        c.execute("""UPDATE pcs SET last_seen=?, code=CASE WHEN code IS NULL OR code='' THEN ? ELSE code END
                     WHERE id=?""", (time.time(), new_code(), pc_id))
        r = c.execute("SELECT room FROM pcs WHERE id=?", (pc_id,)).fetchone()
        c.commit()
        c.close()
        st = room_state(r["room"]) if (r and r["room"] and was_away) else None
        if st:  # ПК включился — сразу применяем расписание
            await set_lock(pc_id, st["locked"], st["teacher"])
        else:
            await push(pc_id)
        try:
            while True:
                await ws.receive_text()  # heartbeat
                c = db()
                c.execute("UPDATE pcs SET last_seen=? WHERE id=?", (time.time(), pc_id))
                c.commit()
                c.close()
        except (WebSocketDisconnect, Exception):
            if conns.get(pc_id) is ws:
                conns.pop(pc_id, None)

    # ---------------- Вход преподавателя ----------------
    @app.post("/api/teacher_login")
    async def teacher_login(data: dict):
        pc, login, pw = data.get("pc", ""), data.get("login", "").strip(), data.get("password", "")
        code = str(data.get("code", "")).replace(" ", "")
        c = db()
        if not pc and code:
            r = c.execute("SELECT id FROM pcs WHERE code=?", (code,)).fetchone()
            pc = r["id"] if r else ""
        t = c.execute("SELECT * FROM teachers WHERE login=?", (login,)).fetchone()
        c.close()
        if not pc:
            return JSONResponse({"ok": False, "error": tr("Неверный код устройства")}, 404)
        if not t:
            add_log(pc, login, "вход: нет в базе")
            return JSONResponse({"ok": False, "error": tr("Преподаватель не найден в базе данных")}, 404)
        if not check_pw(pw, t["pw"]):
            add_log(pc, login, "вход: неверный пароль")
            return JSONResponse({"ok": False, "error": tr("Неверный пароль")}, 401)
        ok, why = teacher_allowed(t)
        if not ok:
            add_log(pc, login, "вход: " + why)
            return JSONResponse({"ok": False, "error": tr(why)}, 403)
        warn = missing_fields(t)
        await set_lock(pc, False, t["full_name"])
        add_log(pc, t["full_name"], "вход: успешно")
        return {"ok": True, "name": t["full_name"],
                "warning": tr("Данные профиля неполные: {f}. Обратитесь к администратору.").format(
                    f=", ".join(warn)) if warn else ""}

    @app.post("/api/teacher_lock")
    async def teacher_lock(data: dict):
        pc, code = data.get("pc", ""), str(data.get("code", "")).replace(" ", "")
        c = db()
        if not pc and code:
            r = c.execute("SELECT id FROM pcs WHERE code=?", (code,)).fetchone()
            pc = r["id"] if r else ""
        t = c.execute("SELECT * FROM teachers WHERE login=?", (data.get("login", "").strip(),)).fetchone()
        c.close()
        if not t or not check_pw(data.get("password", ""), t["pw"]):
            return JSONResponse({"ok": False, "error": tr("Неверный логин или пароль")}, 401)
        ok, why = teacher_allowed(t)
        if not ok:
            return JSONResponse({"ok": False, "error": tr(why)}, 403)
        if not pc:
            return JSONResponse({"ok": False, "error": tr("Неверный код устройства")}, 404)
        await set_lock(pc, True)
        add_log(pc, t["full_name"], "блокировка преподавателем")
        return {"ok": True}

    # ---------------- Админ API ----------------
    @app.post("/api/admin/login")
    async def admin_login(data: dict):
        if not secrets.compare_digest(str(data.get("password", "")).encode(), ADMIN_PASS.encode()):
            raise HTTPException(401, tr("Неверный пароль"))
        tk = secrets.token_hex(16)
        c = db()
        c.execute("DELETE FROM tokens WHERE created<?", (time.time() - TOKEN_TTL,))
        c.execute("INSERT INTO tokens VALUES(?,?)", (tk, time.time()))
        c.commit()
        c.close()
        return {"token": tk}

    @app.get("/api/pcs")
    async def api_pcs(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rows = [dict(r) for r in c.execute("SELECT * FROM pcs ORDER BY id")]
        c.close()
        for r in rows:
            r["online"] = r["id"] in conns
            r["teacher"] = loc(r["teacher"])
        return rows

    @app.post("/api/pcs/{pc_id}/{action}")
    async def api_pc_action(pc_id: str, action: str, x_token: str = Header("")):
        need_admin(x_token)
        if pc_id == "ALL":
            for p in list(conns):
                await set_lock(p, action == "lock", "администратор")
        else:
            await set_lock(pc_id, action == "lock", "администратор")
        add_log(pc_id, "admin", action)
        return {"ok": True}

    @app.delete("/api/pcs/{pc_id}")
    async def api_pc_del(pc_id: str, x_token: str = Header("")):
        need_admin(x_token)
        c = db(); c.execute("DELETE FROM pcs WHERE id=?", (pc_id,)); c.commit(); c.close()
        return {"ok": True}

    @app.get("/api/teachers")
    async def api_teachers(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rows = []
        for r in c.execute("SELECT * FROM teachers ORDER BY full_name"):
            d = dict(r); d.pop("pw")
            d["missing"] = missing_fields(r)
            rows.append(d)
        c.close()
        return rows

    @app.post("/api/teachers")
    async def api_teacher_save(data: dict, x_token: str = Header("")):
        need_admin(x_token)
        f = lambda k: (data.get(k) or None)
        num = lambda k: int(data[k]) if str(data.get(k) or "").isdigit() else None
        c = db()
        try:
            if data.get("id"):
                c.execute("""UPDATE teachers SET login=?, full_name=?, birth_year=?, teach_since=?,
                             subject=?, phone=?, active=? WHERE id=?""",
                          (data["login"], data["full_name"], num("birth_year"), num("teach_since"),
                           f("subject"), f("phone"), 1 if data.get("active") else 0, data["id"]))
                if data.get("password"):
                    c.execute("UPDATE teachers SET pw=? WHERE id=?",
                              (hash_pw(data["password"]), data["id"]))
            else:
                if not data.get("password"):
                    raise HTTPException(400, tr("Укажите пароль"))
                c.execute("""INSERT INTO teachers(login,pw,full_name,birth_year,teach_since,subject,phone,active)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (data["login"], hash_pw(data["password"]), data["full_name"],
                           num("birth_year"), num("teach_since"), f("subject"), f("phone"),
                           1 if data.get("active") else 0))
            c.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(400, tr("Такой логин уже существует"))
        finally:
            c.close()
        return {"ok": True}

    @app.delete("/api/teachers/{tid}")
    async def api_teacher_del(tid: int, x_token: str = Header("")):
        need_admin(x_token)
        c = db(); c.execute("DELETE FROM teachers WHERE id=?", (tid,)); c.commit(); c.close()
        return {"ok": True}

    @app.get("/api/announcements")
    async def api_ann(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rows = [dict(r) for r in c.execute("SELECT * FROM announcements ORDER BY id DESC")]
        c.close()
        return rows

    @app.post("/api/announcements")
    async def api_ann_add(data: dict, x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        c.execute("INSERT INTO announcements(text,created,media,kind) VALUES(?,?,?,?)",
                  (data.get("text", ""), time.time(), os.path.basename(data.get("media", "")),
                   data.get("kind", "")))
        c.commit(); c.close()
        for p in list(conns):
            await push(p)
        return {"ok": True}

    @app.delete("/api/announcements/{aid}")
    async def api_ann_del(aid: int, x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        r = c.execute("SELECT media FROM announcements WHERE id=?", (aid,)).fetchone()
        c.execute("DELETE FROM announcements WHERE id=?", (aid,)); c.commit(); c.close()
        if r and r["media"]:
            try:
                os.remove(os.path.join(MEDIA_DIR, os.path.basename(r["media"])))
            except OSError:
                pass
        for p in list(conns):
            await push(p)
        return {"ok": True}

    @app.get("/api/settings")
    async def api_settings_get(x_token: str = Header("")):
        need_admin(x_token)
        return {"school": school_name(), "logo": get_setting("logo"), "lang": DEFAULT_LANG,
                "client_key": CLIENT_KEY, "public_url": PUBLIC_URL}

    @app.post("/api/settings")
    async def api_settings_set(data: dict, x_token: str = Header("")):
        global ADMIN_PASS, CLIENT_KEY, DEFAULT_LANG, PUBLIC_URL
        need_admin(x_token)
        sch = str(data.get("school", "")).strip()
        set_setting("school", "" if sch in ("Школа", "School", "Okul") else sch)
        if "logo" in data:
            set_setting("logo", data["logo"])  # data:image/... base64
        changes = {}
        if data.get("lang") in LANGS:
            DEFAULT_LANG = data["lang"]
            changes["lang"] = DEFAULT_LANG
        if "public_url" in data:
            PUBLIC_URL = str(data.get("public_url") or "").strip().rstrip("/")
            changes["public_url"] = PUBLIC_URL
        ck = str(data.get("client_key") or "").strip()
        if ck and ck != CLIENT_KEY:
            CLIENT_KEY = ck
            changes["client_key"] = ck
        npw = str(data.get("new_password") or "")
        if npw:
            if len(npw) < 4:
                raise HTTPException(400, tr("Пароль администратора должен быть не короче 4 символов"))
            ADMIN_PASS = npw
            changes["admin_pass"] = npw
            c = db()
            c.execute("DELETE FROM tokens WHERE tk!=?", (x_token,))  # остальные сеансы админа закрываем
            c.commit()
            c.close()
        if changes:
            save_config(**changes)  # переживает перезапуск сервера
        return {"ok": True}

    @app.get("/api/log")
    async def api_log(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rows = [dict(r) for r in c.execute("SELECT * FROM log ORDER BY id DESC LIMIT 100")]
        c.close()
        for r in rows:
            r["event"] = loc(r["event"])
            r["teacher"] = loc(r["teacher"])
        return rows

    # ---------------- Публичное: QR, доска, вход ----------------
    @app.get("/api/stats")
    async def api_stats(x_token: str = Header("")):
        need_admin(x_token)
        now = time.time()
        total = float(get_setting("total_seconds", "0") or 0)
        up = int(now - STATS["start"])
        return {"uptime": f"{up//3600:02d}:{up%3600//60:02d}:{up%60:02d}",
                "total_hours": round(total / 3600, 1),
                "pcs_online": len(conns),
                "boards_online": boards_online(),
                "panel_hits": STATS["panel_hits"]}

    # ============ расписание, экзамены, доступ, проверка, медиа ============
    TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
    last = {}  # room -> (locked, teacher, denied): последнее применённое состояние
    os.makedirs(MEDIA_DIR, exist_ok=True)
    app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

    async def apply_room(room, st):
        c = db()
        ids = [r["id"] for r in c.execute("SELECT id FROM pcs WHERE room=?", (room,))]
        c.close()
        for pid in ids:
            await set_lock(pid, st["locked"], st["teacher"])
        add_log(room, st["teacher"], "расписание: " + ("блокировка" if st["locked"] else "разблокировка"))
        if st.get("denied"):
            add_log(room, st["denied"], "расписание: урок не открыт — у преподавателя нет доступа")

    async def scheduler():
        while True:
            try:
                c = db()
                rooms = [r["room"] for r in c.execute(
                    "SELECT DISTINCT room FROM pcs WHERE room IS NOT NULL AND room!=''")]
                c.close()
                for room in rooms:
                    st = room_state(room)
                    if st is None:
                        last.pop(room, None)
                        continue
                    sig = (st["locked"], st["teacher"], st.get("denied", ""))
                    if last.get(room) != sig:
                        last[room] = sig
                        await apply_room(room, st)
            except Exception as ex:
                print("scheduler:", ex)
            await asyncio.sleep(10)

    @app.on_event("startup")
    async def _startup():
        app.state.sched = asyncio.create_task(scheduler())

    @app.post("/api/pcconf")
    async def api_pcconf(data: dict, x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        if "room" in data:
            c.execute("UPDATE pcs SET room=? WHERE id=?", (str(data["room"]).strip(), data["id"]))
        if data.get("kind") in ("pc", "board"):
            c.execute("UPDATE pcs SET kind=? WHERE id=?", (data["kind"], data["id"]))
        c.commit(); c.close()
        last.clear()
        return {"ok": True}

    @app.post("/api/teachers/{tid}/temp")
    async def api_teacher_temp(tid: int, data: dict, x_token: str = Header("")):
        need_admin(x_token)
        h = float(data.get("hours") or 0)
        c = db()
        c.execute("UPDATE teachers SET access_until=?, active=1 WHERE id=?",
                  (time.time() + h * 3600 if h > 0 else None, tid))
        c.commit(); c.close()
        last.clear()
        return {"ok": True}

    @app.get("/api/rooms")
    async def api_rooms(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rooms = sorted({r[0] for r in c.execute(
            "SELECT room FROM pcs UNION SELECT room FROM lessons UNION SELECT room FROM exams") if r[0]})
        c.close()
        return rooms

    @app.get("/api/lessons")
    async def api_lessons(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rows = [dict(r) for r in c.execute(LESSON_SEL + " ORDER BY weekday, start_t")]
        c.close()
        return rows

    @app.post("/api/lessons")
    async def api_lesson_add(data: dict, x_token: str = Header("")):
        need_admin(x_token)
        a, b = data.get("start", ""), data.get("end", "")
        if not (TIME_RE.match(a) and TIME_RE.match(b) and a < b):
            raise HTTPException(400, tr("Неверное время урока"))
        room = str(data.get("room", "")).strip()
        if not room:
            raise HTTPException(400, tr("Укажите кабинет"))
        wd = int(data.get("weekday", -1))
        if wd not in range(7):
            raise HTTPException(400, tr("Неверный день недели"))
        c = db()
        c.execute("""INSERT INTO lessons(weekday,start_t,end_t,room,teacher_id,subject,class_name)
                     VALUES(?,?,?,?,?,?,?)""", (wd, a, b, room, data.get("teacher_id"),
                                                data.get("subject", ""), data.get("class_name", "")))
        c.commit(); c.close()
        last.clear()
        return {"ok": True}

    @app.delete("/api/lessons/{lid}")
    async def api_lesson_del(lid: int, x_token: str = Header("")):
        need_admin(x_token)
        c = db(); c.execute("DELETE FROM lessons WHERE id=?", (lid,)); c.commit(); c.close()
        last.clear()
        return {"ok": True}

    @app.get("/api/exams")
    async def api_exams(x_token: str = Header("")):
        need_admin(x_token)
        c = db()
        rows = [dict(r) for r in c.execute(EXAM_SEL + " ORDER BY day DESC, start_t")]
        c.close()
        return rows

    @app.post("/api/exams")
    async def api_exam_add(data: dict, x_token: str = Header("")):
        need_admin(x_token)
        a, b, d = data.get("start", ""), data.get("end", ""), data.get("date", "")
        if not (TIME_RE.match(a) and TIME_RE.match(b) and a < b and DATE_RE.match(d)):
            raise HTTPException(400, tr("Неверная дата или время экзамена"))
        room = str(data.get("room", "")).strip()
        if not room or not str(data.get("title", "")).strip():
            raise HTTPException(400, tr("Укажите название и кабинет"))
        c = db()
        c.execute("INSERT INTO exams(title,day,start_t,end_t,room,teacher_id) VALUES(?,?,?,?,?,?)",
                  (data["title"].strip(), d, a, b, room, data.get("teacher_id")))
        c.commit(); c.close()
        last.clear()
        return {"ok": True}

    @app.delete("/api/exams/{eid}")
    async def api_exam_del(eid: int, x_token: str = Header("")):
        need_admin(x_token)
        c = db(); c.execute("DELETE FROM exams WHERE id=?", (eid,)); c.commit(); c.close()
        last.clear()
        return {"ok": True}

    @app.get("/api/overview")
    async def api_overview(x_token: str = Header("")):
        """Таблица: все преподаватели и все их уроки."""
        need_admin(x_token)
        c = db()
        T = [dict(r) for r in c.execute("SELECT * FROM teachers ORDER BY full_name")]
        L = [dict(r) for r in c.execute(LESSON_SEL + " ORDER BY weekday, start_t")]
        c.close()
        out = []
        for t in T:
            ls = [l for l in L if l["teacher_id"] == t["id"]]
            if not t["active"]:
                status = tr("не работает")
            elif t["access_until"] and time.time() > t["access_until"]:
                status = tr("доступ истёк")
            elif t["access_until"]:
                status = tr("до ") + time.strftime("%d.%m %H:%M", time.localtime(t["access_until"]))
            else:
                status = tr("работает")
            out.append({"id": t["id"], "full_name": t["full_name"], "subject": t["subject"],
                        "status": status, "lessons": ls,
                        "week_minutes": sum(to_min(l["end"]) - to_min(l["start"]) for l in ls)})
        return out

    @app.get("/api/check")
    async def api_check(x_token: str = Header("")):
        """Кнопка «Проверить данные»: неполные профили, конфликты в расписании, устройства."""
        need_admin(x_token)
        c = db()
        T = [dict(r) for r in c.execute("SELECT * FROM teachers")]
        L = [dict(r) for r in c.execute(LESSON_SEL)]
        P = [dict(r) for r in c.execute("SELECT * FROM pcs")]
        c.close()
        tmap = {t["id"]: t for t in T}
        iss = []
        seen = {}
        for t in T:
            miss = missing_fields(t)
            if miss:
                iss.append(("warn", tr("{n}: не заполнено — {f}").format(n=t["full_name"], f=", ".join(miss))))
            seen.setdefault(t["full_name"].strip().lower(), []).append(t)
            ok, why = teacher_allowed(t)
            if not ok and any(l["teacher_id"] == t["id"] for l in L):
                iss.append(("error", tr("{n}: есть уроки в расписании, но доступа нет ({w})").format(
                    n=t["full_name"], w=tr(why))))
        for ts in seen.values():
            if len(ts) > 1:
                iss.append(("warn", tr("Повторяется ФИО в базе: {n}").format(n=ts[0]["full_name"])))
        for l in L:
            if l["teacher_id"] not in tmap:
                iss.append(("error", tr("Урок {d} {s} ({r}): преподаватель не найден в базе").format(
                    d=dn(l["weekday"]), s=l["start"], r=l["room"])))
        for i, a in enumerate(L):
            for b in L[i + 1:]:
                if a["weekday"] == b["weekday"] and a["start"] < b["end"] and b["start"] < a["end"]:
                    when = tr("{d} {a1}–{a2} и {b1}–{b2}").format(
                        d=dn(a["weekday"]), a1=a["start"], a2=a["end"], b1=b["start"], b2=b["end"])
                    if a["room"] == b["room"]:
                        iss.append(("error", tr("Кабинет {r}: пересечение уроков {w}").format(r=a["room"], w=when)))
                    if a["teacher_id"] and a["teacher_id"] == b["teacher_id"]:
                        name = tmap.get(a["teacher_id"], {}).get("full_name", "?")
                        iss.append(("error", tr("{n}: два урока одновременно ({w})").format(n=name, w=when)))
        for p in P:
            if not p["room"]:
                iss.append(("warn", tr("Устройство {p}: не назначен кабинет — расписание не действует").format(p=p["id"])))
            elif p["id"] not in conns:
                iss.append(("warn", tr("Устройство {p} ({r}) сейчас не в сети").format(p=p["id"], r=p["room"])))
        bad = sum(1 for lv, _x in iss if lv == "error")
        iss.append(("ok" if not bad else "warn",
                    tr("Проверено: преподавателей {t}, уроков {l}, устройств {p}. Ошибок: {e}").format(
                        t=len(T), l=len(L), p=len(P), e=bad)))
        return {"issues": [{"level": lv, "text": tx} for lv, tx in iss]}

    @app.post("/api/media")
    async def api_media(request: Request, x_token: str = Header(""), x_filename: str = Header("file")):
        """Загрузка фото/видео для объявлений (сырое тело запроса, без доп. библиотек)."""
        need_admin(x_token)
        ext = os.path.splitext(unquote(x_filename))[1].lower()
        if ext not in IMG_EXT | VID_EXT:
            raise HTTPException(400, tr("Допустимые форматы: jpg, png, gif, webp, mp4, webm, mov"))
        name = secrets.token_hex(8) + ext
        path = os.path.join(MEDIA_DIR, name)
        size = 0
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_MEDIA:
                    f.close(); os.remove(path)
                    raise HTTPException(413, tr("Файл больше 300 МБ"))
                f.write(chunk)
        return {"name": name, "kind": "video" if ext in VID_EXT else "image"}

    @app.get("/app", response_class=HTMLResponse)
    async def phone_app():
        return page(APP_HTML)

    @app.get("/manifest-app.json")
    async def manifest_app():
        return {"name": "EduLock — преподаватель", "short_name": "EduLock", "start_url": "/app",
                "display": "standalone", "background_color": "#f5f5f7", "theme_color": "#0071e3",
                "icons": [{"src": "/qr?data=edulock", "sizes": "512x512", "type": "image/svg+xml"}]}

    @app.get("/qr")
    async def qr(data: str):
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
        buf = io.BytesIO(); img.save(buf)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    @app.get("/api/public")
    async def api_public():
        c = db()
        ann = [{"text": a["text"] or "", "media": a["media"] or "", "kind": a["kind"] or ""}
               for a in c.execute("SELECT text, media, kind FROM announcements ORDER BY id DESC LIMIT 8")]
        c.close()
        return {"school": school_name(), "logo": get_setting("logo"),
                "announcements": ann}

    @app.get("/api/pcstate/{pc_id}")
    async def api_pcstate(pc_id: str):
        STATS['boards'][pc_id] = time.time()  # доска ещё жива
        c = db()
        r = c.execute("SELECT locked, teacher, code FROM pcs WHERE id=?", (pc_id,)).fetchone()
        c.close()
        return {"locked": bool(r["locked"]) if r else True, "teacher": loc(r["teacher"]) if r else "",
                "code": ((r["code"] or "")[:3] + " " + (r["code"] or "")[3:]) if r and r["locked"] and r["code"] else ""}

    @app.get("/board", response_class=HTMLResponse)
    async def board(request: Request, pc: str = "PC-01"):
        base = (PUBLIC_URL or str(request.base_url)).rstrip("/")
        return page(BOARD_HTML).replace("__PC__", pc).replace("__URL__", f"{base}/t/{pc}")

    @app.get("/t/{pc_id}", response_class=HTMLResponse)
    async def teacher_page(pc_id: str):
        return page(TEACHER_HTML).replace("__PC__", pc_id)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page():
        STATS['panel_hits'] += 1
        STATS['last_panel'] = time.time()
        return page(ADMIN_HTML)

    @app.get("/manifest.json")
    async def manifest():
        return {"name": "EduLock", "short_name": "EduLock", "start_url": "/admin",
                "display": "standalone", "background_color": "#0f172a", "theme_color": "#0f172a",
                "icons": [{"src": "/qr?data=edulock", "sizes": "512x512", "type": "image/svg+xml"}]}

    print(f"EduLock сервер: http://{host}:{port}/admin   (пароль админа: переменная ADMIN_PASS)")
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                        timeout_graceful_shutdown=3))
    SERVER["srv"] = srv

    def tick():  # копим общее время работы (часы) в базе
        while not srv.should_exit:
            time.sleep(30)
            try:
                set_setting("total_seconds", str(int(float(get_setting("total_seconds", "0") or 0)) + 30))
            except Exception:
                pass
    threading.Thread(target=tick, daemon=True).start()
    srv.run()


# ====================================================================
#                      HTML: панель, доска, вход
# ====================================================================
BOARD_HTML = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Доска</title>
<style>
:root{--bgbase:#f5f5f7;--c1:rgba(120,160,255,.55);--c2:rgba(214,150,255,.5);--c3:rgba(110,220,255,.5);--card:rgba(255,255,255,.62);--txt:#1d1d1f;--sub:#6e6e73;--line:rgba(0,0,0,.08);--blue:#0071e3;--red:#ff3b30;--green:#30b350;--orange:#ff9f0a;--inp:rgba(255,255,255,.75);--sel:rgba(255,255,255,.95);--shadow:0 10px 40px rgba(0,0,0,.08)}
@media(prefers-color-scheme:dark){:root{--bgbase:#000;--c1:rgba(60,90,230,.45);--c2:rgba(150,60,220,.4);--c3:rgba(20,150,190,.4);--card:rgba(44,44,52,.55);--txt:#f5f5f7;--sub:#a1a1a6;--line:rgba(255,255,255,.1);--inp:rgba(70,70,80,.6);--sel:rgba(99,99,102,.85);--shadow:0 10px 40px rgba(0,0,0,.4)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--txt);-webkit-font-smoothing:antialiased;background:radial-gradient(55% 45% at 12% 8%,var(--c1),transparent 70%),radial-gradient(50% 50% at 92% 18%,var(--c2),transparent 70%),radial-gradient(60% 55% at 50% 105%,var(--c3),transparent 70%),var(--bgbase);background-attachment:fixed}
.glass,.card,.box,header{background:var(--card);-webkit-backdrop-filter:blur(30px) saturate(180%);backdrop-filter:blur(30px) saturate(180%);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow)}
button{font:inherit;font-weight:600;font-size:15px;background:var(--blue);color:#fff;border:0;padding:11px 18px;border-radius:980px;cursor:pointer;transition:transform .15s,opacity .15s}
button:hover{opacity:.9}button:active{transform:scale(.96)}
button.g,button.lock{background:rgba(120,120,128,.2);color:var(--txt)}button.r{background:var(--red)}button.ok{background:var(--green)}
input,select,textarea{font:inherit;font-size:16px;width:100%;padding:13px 15px;margin:5px 0;border-radius:14px;border:1px solid var(--line);background:var(--inp);color:var(--txt);outline:0}
input:focus,textarea:focus{box-shadow:0 0 0 4px rgba(0,113,227,.3);border-color:var(--blue)}

:root{--bgbase:#000;--c1:rgba(60,90,230,.5);--c2:rgba(150,60,220,.45);--c3:rgba(20,150,190,.45);--card:rgba(44,44,52,.55);--txt:#f5f5f7;--sub:#a1a1a6;--line:rgba(255,255,255,.1);--shadow:0 10px 40px rgba(0,0,0,.4)}

body{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}
.clock{font-size:clamp(56px,11vw,150px);font-weight:700;letter-spacing:-.04em;line-height:1}
img.logo{max-height:110px;margin:18px 0 0}
h1{font-size:clamp(30px,4.5vw,60px);letter-spacing:-.03em;margin:12px 0 4px;font-weight:700}
.st{color:var(--sub);font-size:clamp(16px,2vw,24px)}
.qr{background:#fff;padding:22px;border-radius:36px;box-shadow:var(--shadow);margin:24px 0 12px;display:inline-block}
.qr img{width:min(38vh,320px);height:min(38vh,320px);display:block}
.code{font-size:clamp(34px,5vw,64px);font-weight:700;letter-spacing:.18em;margin:4px 0}
.ann{max-width:1000px;width:100%;margin:14px auto;font-size:clamp(18px,2.3vw,30px)}
.ann div{margin:10px 0;padding:14px 22px;border-radius:20px;background:var(--card);-webkit-backdrop-filter:blur(30px);backdrop-filter:blur(30px);border:1px solid var(--line)}
</style>
<body><div class=clock id=clk></div><img id=logo class=logo hidden><h1 id=school></h1>
<div class=st id=st></div>
<div class=qr><img src="/qr?data=__URL__"></div>
<p class=st>Отсканируйте QR-код или введите код в приложении</p>
<div class=code id=code></div>
<div class=ann id=ann></div>
<script>
async function upd(){try{
 const p=await (await fetch('/api/public')).json();
 school.textContent=p.school;
 if(p.logo){logo.src=p.logo;logo.hidden=false}
 ann.innerHTML=p.announcements.map(a=>'<div>'+(a.media?(a.kind=='video'?'<video src=/media/'+a.media+' autoplay muted loop style=max-width:100%;max-height:36vh;border-radius:16px></video>':'<img src=/media/'+a.media+' style=max-width:100%;max-height:36vh;border-radius:16px>')+'<br>':'')+(a.text?'📢 '+a.text.replace(/</g,'&lt;'):'')+'</div>').join('');
 const s=await (await fetch('/api/pcstate/__PC__')).json();
 code.textContent=s.locked&&s.code?'Код: '+s.code:'';st.textContent=s.locked?'Устройство __PC__ заблокировано':'Работает: '+s.teacher;
}catch(e){}}
function tk(){clk.textContent=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}tk();setInterval(tk,1000);upd();setInterval(upd,5000);
</script></body></html>"""

TEACHER_HTML = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Вход преподавателя</title>
<style>
:root{--bgbase:#f5f5f7;--c1:rgba(120,160,255,.55);--c2:rgba(214,150,255,.5);--c3:rgba(110,220,255,.5);--card:rgba(255,255,255,.62);--txt:#1d1d1f;--sub:#6e6e73;--line:rgba(0,0,0,.08);--blue:#0071e3;--red:#ff3b30;--green:#30b350;--orange:#ff9f0a;--inp:rgba(255,255,255,.75);--sel:rgba(255,255,255,.95);--shadow:0 10px 40px rgba(0,0,0,.08)}
@media(prefers-color-scheme:dark){:root{--bgbase:#000;--c1:rgba(60,90,230,.45);--c2:rgba(150,60,220,.4);--c3:rgba(20,150,190,.4);--card:rgba(44,44,52,.55);--txt:#f5f5f7;--sub:#a1a1a6;--line:rgba(255,255,255,.1);--inp:rgba(70,70,80,.6);--sel:rgba(99,99,102,.85);--shadow:0 10px 40px rgba(0,0,0,.4)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--txt);-webkit-font-smoothing:antialiased;background:radial-gradient(55% 45% at 12% 8%,var(--c1),transparent 70%),radial-gradient(50% 50% at 92% 18%,var(--c2),transparent 70%),radial-gradient(60% 55% at 50% 105%,var(--c3),transparent 70%),var(--bgbase);background-attachment:fixed}
.glass,.card,.box,header{background:var(--card);-webkit-backdrop-filter:blur(30px) saturate(180%);backdrop-filter:blur(30px) saturate(180%);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow)}
button{font:inherit;font-weight:600;font-size:15px;background:var(--blue);color:#fff;border:0;padding:11px 18px;border-radius:980px;cursor:pointer;transition:transform .15s,opacity .15s}
button:hover{opacity:.9}button:active{transform:scale(.96)}
button.g,button.lock{background:rgba(120,120,128,.2);color:var(--txt)}button.r{background:var(--red)}button.ok{background:var(--green)}
input,select,textarea{font:inherit;font-size:16px;width:100%;padding:13px 15px;margin:5px 0;border-radius:14px;border:1px solid var(--line);background:var(--inp);color:var(--txt);outline:0}
input:focus,textarea:focus{box-shadow:0 0 0 4px rgba(0,113,227,.3);border-color:var(--blue)}

body{display:flex;justify-content:center;align-items:center;padding:20px}
.box{padding:30px 26px;width:100%;max-width:400px}
.ico{font-size:46px;text-align:center}h2{text-align:center;font-size:26px;letter-spacing:-.02em;margin:6px 0 10px}
.box>div:nth-of-type(2){text-align:center;color:var(--sub);margin-bottom:10px}
button{width:100%;margin:8px 0;padding:14px}
.err,.ok,.warn{padding:13px 15px;border-radius:14px;margin-top:12px;font-size:15px}
.err{background:rgba(255,59,48,.16);color:var(--red)}.ok{background:rgba(48,179,80,.16);color:var(--green)}.warn{background:rgba(255,159,10,.18);color:var(--orange)}
</style>
<body><div class=box><div class=ico>🔐</div><h2>Вход преподавателя</h2><div>Компьютер: <b>__PC__</b></div>
<input id=l placeholder="Логин"><input id=p type=password placeholder="Пароль">
<button onclick=go()>Войти и разблокировать</button>
<button class=lock onclick=lk()>Заблокировать компьютер</button><div id=m></div></div>
<script>
async function go(){
 const r=await fetch('/api/teacher_login',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({pc:'__PC__',login:l.value,password:p.value})});
 const d=await r.json();
 if(!d.ok){m.innerHTML='<div class=err>⛔ '+(d.error||'Ошибка')+'</div>';return}
 m.innerHTML='<div class=ok>✅ Добро пожаловать, '+d.name+'!</div>'+(d.warning?'<div class=warn>⚠️ '+d.warning+'</div>':'');}
async function lk(){const r=await fetch('/api/teacher_lock',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({pc:'__PC__',login:l.value,password:p.value})});const d=await r.json();
 m.innerHTML=d.ok?'<div class=ok>🔒 Заблокировано</div>':'<div class=err>⛔ '+(d.error||'Ошибка')+'</div>'}
</script></body></html>"""

ADMIN_HTML = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=manifest href=/manifest.json><meta name=theme-color content="#0f172a"><title>EduLock — панель</title>
<style>
:root{--bgbase:#f5f5f7;--c1:rgba(120,160,255,.55);--c2:rgba(214,150,255,.5);--c3:rgba(110,220,255,.5);--card:rgba(255,255,255,.62);--txt:#1d1d1f;--sub:#6e6e73;--line:rgba(0,0,0,.08);--blue:#0071e3;--red:#ff3b30;--green:#30b350;--orange:#ff9f0a;--inp:rgba(255,255,255,.75);--sel:rgba(255,255,255,.95);--shadow:0 10px 40px rgba(0,0,0,.08)}
@media(prefers-color-scheme:dark){:root{--bgbase:#000;--c1:rgba(60,90,230,.45);--c2:rgba(150,60,220,.4);--c3:rgba(20,150,190,.4);--card:rgba(44,44,52,.55);--txt:#f5f5f7;--sub:#a1a1a6;--line:rgba(255,255,255,.1);--inp:rgba(70,70,80,.6);--sel:rgba(99,99,102,.85);--shadow:0 10px 40px rgba(0,0,0,.4)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--txt);-webkit-font-smoothing:antialiased;background:radial-gradient(55% 45% at 12% 8%,var(--c1),transparent 70%),radial-gradient(50% 50% at 92% 18%,var(--c2),transparent 70%),radial-gradient(60% 55% at 50% 105%,var(--c3),transparent 70%),var(--bgbase);background-attachment:fixed}
.glass,.card,.box,header{background:var(--card);-webkit-backdrop-filter:blur(30px) saturate(180%);backdrop-filter:blur(30px) saturate(180%);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow)}
button{font:inherit;font-weight:600;font-size:15px;background:var(--blue);color:#fff;border:0;padding:11px 18px;border-radius:980px;cursor:pointer;transition:transform .15s,opacity .15s}
button:hover{opacity:.9}button:active{transform:scale(.96)}
button.g,button.lock{background:rgba(120,120,128,.2);color:var(--txt)}button.r{background:var(--red)}button.ok{background:var(--green)}
input,select,textarea{font:inherit;font-size:16px;width:100%;padding:13px 15px;margin:5px 0;border-radius:14px;border:1px solid var(--line);background:var(--inp);color:var(--txt);outline:0}
input:focus,textarea:focus{box-shadow:0 0 0 4px rgba(0,113,227,.3);border-color:var(--blue)}

header{display:flex;flex-wrap:wrap;gap:4px;padding:6px;margin:14px auto 6px;max-width:760px;position:sticky;top:12px;z-index:10;border-radius:20px}
header button{flex:1;min-width:92px;background:transparent;color:var(--txt);font-weight:500;font-size:14px;padding:10px 6px;border-radius:14px}
header button.sel{background:var(--sel);box-shadow:0 2px 10px rgba(0,0,0,.12);font-weight:600}
main{padding:8px 14px 50px;max-width:900px;margin:auto}
h2{font-size:28px;letter-spacing:-.02em;margin:16px 4px}
.card{padding:16px;margin:10px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}.grid .card{margin:0}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px}.on{background:var(--green);box-shadow:0 0 10px var(--green)}.off{background:var(--red)}
.warn{color:var(--orange);font-size:13px}small{color:var(--sub)}
label.c{display:flex;gap:10px;align-items:center}label.c input{width:auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:10px 0 4px}
.stat{margin:0!important;padding:14px 16px}.stat b{display:block;font-size:28px;letter-spacing:-.02em}.stat span{color:var(--sub);font-size:13px}
#login{max-width:360px;margin:14vh auto;padding:30px;text-align:center}#login h2{margin-top:0}#login button{width:100%;margin-top:8px}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px;text-align:left;vertical-align:top}
th{color:var(--sub);font-weight:600}.cell{margin:2px 0}.cell small{display:block}
.bad{color:var(--red);font-size:14px}.okk{color:var(--green);font-size:14px}.wrn{color:var(--orange);font-size:14px}
</style><body>
<div id=login class=card><h2>EduLock</h2>
<input id=pw type=password placeholder="Пароль администратора"><button onclick=doLogin()>Войти</button>
<div id=lerr class=warn></div></div>
<div id=app hidden>
<header><button onclick="tab('pcs')">💻 Устройства</button><button onclick="tab('teachers')">👩‍🏫 Преподаватели</button>
<button onclick="tab('sched')">📅 Расписание</button><button onclick="tab('exams')">📝 Экзамены</button>
<button onclick="tab('overview')">📊 Сводка</button><button onclick="tab('ann')">📢 Объявления</button>
<button onclick="tab('set')">⚙️ Настройки</button><button onclick="tab('log')">📜 Журнал</button></header><main id=main></main></div>
<script>
let T=localStorage.getItem('tk')||'',cur='pcs',timer;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,m='GET',b){const r=await fetch('/api/'+p,{method:m,headers:{'Content-Type':'application/json','X-Token':T},
 body:b?JSON.stringify(b):undefined});if(r.status==401){logout();throw 0}
 const d=await r.json().catch(()=>({}));if(!r.ok){alert(d.detail||'Ошибка');throw 0}return d}
async function doLogin(){const r=await fetch('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({password:pw.value})});if(!r.ok){lerr.textContent='Неверный пароль';return}
 T=(await r.json()).token;localStorage.setItem('tk',T);start()}
function logout(){localStorage.removeItem('tk');location.reload()}
function start(){login.hidden=true;app.hidden=false;tab('pcs')}
function tab(t){cur=t;clearInterval(timer);document.querySelectorAll('header button').forEach(b=>b.classList.toggle('sel',(b.getAttribute('onclick')||'').indexOf("'"+t+"'")>0));({pcs,teachers,ann,set:settings,log,sched,exams,overview}[t])();
 if(t=='pcs')timer=setInterval(pcs,4000)}
const DAYN=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];let PCS=[],TEA=[],OV=[],curRoom='';
async function pcs(){if(document.activeElement&&main.contains(document.activeElement)&&document.activeElement.tagName!='BUTTON')return;
 const d=await api('pcs'),st=await api('stats');PCS=d.map(p=>p.id);
 main.innerHTML=`<div class=stats>
 <div class="card stat"><b>${st.pcs_online}</b><span>ПК онлайн</span></div>
 <div class="card stat"><b>${st.boards_online}</b><span>Умные доски</span></div>
 <div class="card stat"><b>${st.total_hours} ч</b><span>Всего часов работы</span></div>
 <div class="card stat"><b>${st.uptime}</b><span>Текущая сессия</span></div></div>
 <div class=row><h2>Устройства</h2><div><button class=r onclick="act('ALL','lock')">🔒 Все</button> <button class=ok onclick="act('ALL','unlock')">🔓 Все</button></div></div>
 <div class=grid>${d.map((p,i)=>`<div class=card><div><span class="dot ${p.online?'on':'off'}"></span><b>${esc(p.id)}</b> <small>${p.kind=='board'?'🖥 доска':'💻 ПК'}</small></div>
 <div>${p.locked?'🔒 Заблокирован':'🔓 '+esc(p.teacher||'Открыт')}</div>
 <input placeholder="Кабинет (для расписания)" value="${esc(p.room)}" onchange="pcConf(${i},this.value,null)">
 <select onchange="pcConf(${i},null,this.value)"><option value=pc ${p.kind!='board'?'selected':''}>Компьютер</option><option value=board ${p.kind=='board'?'selected':''}>Умная доска</option></select>
 <div class=row style="margin-top:6px"><button class=r onclick="act(PCS[${i}],'lock')">Блок</button><button class=ok onclick="act(PCS[${i}],'unlock')">Откр.</button><button class=g onclick="delPc(${i})">🗑</button></div></div>`).join('')}</div>`}
async function act(id,a){await api('pcs/'+encodeURIComponent(id)+'/'+a,'POST');pcs()}
async function pcConf(i,room,kind){const b={id:PCS[i]};if(room!==null)b.room=room;if(kind!==null)b.kind=kind;await api('pcconf','POST',b)}
async function delPc(i){if(confirm('Убрать устройство из списка?')){await api('pcs/'+encodeURIComponent(PCS[i]),'DELETE');pcs()}}
async function teachers(){const d=await api('teachers');TEA=d;
 main.innerHTML=`<div class=row><h2>Преподаватели</h2><div><button class=g onclick="checkData()">✔️ Проверить данные</button> <button onclick="tForm()">➕ Добавить</button></div></div><div id=chk></div>`+
 d.map((t,i)=>{const exp=t.access_until?new Date(t.access_until*1000):null,ex=exp&&exp<new Date();
 return `<div class=card><div class=row><div><b>${esc(t.full_name)}</b> <small>(${esc(t.login)})</small><br>
 Предмет: ${esc(t.subject||'—')} · Год рожд.: ${esc(t.birth_year||'—')} · Преподаёт с: ${esc(t.teach_since||'—')} · Тел: ${esc(t.phone||'—')}<br>
 ${!t.active?'⛔ не работает':ex?'⏱ временный доступ истёк':exp?'⏱ временный доступ до '+exp.toLocaleString():'✅ работает'}
 ${t.missing.length?`<div class=warn>⚠️ Не хватает данных: ${t.missing.join(', ')}</div>`:''}</div>
 <div class=row><button class=g onclick="tForm(TEA[${i}])">✏️</button><button class=g title="Временный доступ" onclick="tTemp(${t.id})">⏱</button><button class=r onclick="tDel(${t.id})">🗑</button></div></div></div>`}).join('')}
async function tTemp(id){const h=prompt('Временный доступ: на сколько часов? (например 8 или 24). 0 = постоянный доступ без срока','8');if(h===null)return;
 await api('teachers/'+id+'/temp','POST',{hours:parseFloat(h)||0});teachers()}
async function checkData(){const r=await api('check');
 chk.innerHTML='<div class=card><b>Проверка данных</b>'+r.issues.map(i=>'<div class='+(i.level=='error'?'bad':i.level=='warn'?'wrn':'okk')+'>'+(i.level=='error'?'⛔ ':i.level=='warn'?'⚠️ ':'✅ ')+esc(i.text)+'</div>').join('')+'</div>'}
function tForm(t){t=t||{active:1};main.innerHTML='<h2>'+(t.id?'Редактирование':'Новый преподаватель')+'</h2><div class=card>'+
 '<input id=f_name placeholder="ФИО" value="'+esc(t.full_name||'')+'"><input id=f_login placeholder="Логин" value="'+esc(t.login||'')+'">'+
 '<input id=f_pw type=password placeholder="'+(t.id?'Новый пароль (пусто = не менять)':'Пароль')+'">'+
 '<input id=f_by type=number placeholder="Год рождения" value="'+esc(t.birth_year||'')+'"><input id=f_ts type=number placeholder="Преподаёт с года" value="'+esc(t.teach_since||'')+'">'+
 '<input id=f_sub placeholder="Предмет" value="'+esc(t.subject||'')+'"><input id=f_ph placeholder="Телефон" value="'+esc(t.phone||'')+'">'+
 '<label class=c><input id=f_act type=checkbox '+(t.active?'checked':'')+'> Преподаватель работает (допуск к входу)</label><br>'+
 '<button onclick="tSave('+(t.id||0)+')">💾 Сохранить</button> <button class=g onclick="teachers()">Отмена</button></div>'}
async function tSave(id){await api('teachers','POST',{id:id||undefined,full_name:f_name.value,login:f_login.value,password:f_pw.value,
 birth_year:f_by.value,teach_since:f_ts.value,subject:f_sub.value,phone:f_ph.value,active:f_act.checked});teachers()}
async function tDel(id){if(confirm('Удалить преподавателя?')){await api('teachers/'+id,'DELETE');teachers()}}
async function ann(){const d=await api('announcements');
 main.innerHTML=`<h2>Объявления</h2><div class=card><textarea id=a_t rows=3 placeholder="Текст объявления (необязательно, если добавляете фото или видео)"></textarea>
 <input id=a_f type=file accept="image/*,video/*"><div id=a_s class=warn></div><button onclick=aAdd()>Опубликовать</button></div>`+
 d.map(a=>`<div class=card><div class=row><div>${a.media?(a.kind=='video'?`<video src="/media/${a.media}" style="max-height:90px;border-radius:10px" muted></video>`:`<img src="/media/${a.media}" style="max-height:90px;border-radius:10px">`):''}<div>${esc(a.text)}</div></div><button class=r onclick="aDel(${a.id})">🗑</button></div></div>`).join('')}
async function aAdd(){let media='',kind='';const f=a_f.files[0];
 if(f){a_s.textContent='Загрузка '+f.name+' …';const r=await fetch('/api/media',{method:'POST',headers:{'X-Token':T,'X-Filename':encodeURIComponent(f.name)},body:f});
  if(!r.ok){const e=await r.json().catch(()=>({}));a_s.textContent='Ошибка: '+(e.detail||r.status);return}const j=await r.json();media=j.name;kind=j.kind}
 if(!a_t.value.trim()&&!media)return;await api('announcements','POST',{text:a_t.value,media,kind});ann()}
async function aDel(id){await api('announcements/'+id,'DELETE');ann()}
async function settings(){const s=await api('settings');
 main.innerHTML='<h2>Настройки</h2><div class=card><input id=s_n placeholder="Название школы" value="'+esc(s.school)+'">'+
 '<p>Логотип:</p>'+(s.logo?'<img src="'+s.logo+'" style="max-height:80px;background:#fff;padding:6px;border-radius:8px"><br>':'')+
 '<input id=s_f type=file accept="image/*">'+
 '<p>Язык по умолчанию:</p><select id=s_l><option value=ru>Русский</option><option value=en>English</option><option value=tr>Türkçe</option></select>'+
 '<p>Публичный адрес сервера (для QR-кодов на досках)</p><input id=s_u placeholder="https://edulock.example.com" value="'+esc(s.public_url)+'">'+
 '<p>Ключ клиентов (для программ на ПК и досках)</p><input id=s_k value="'+esc(s.client_key)+'">'+
 '<input id=s_pw type=password placeholder="Новый пароль администратора (пусто = не менять)">'+
 '<small>Настройки сохраняются на сервере и остаются после перезапуска.</small><br>'+
 '<button onclick=sSave()>💾 Сохранить</button> <button class=g onclick=logout()>Выйти</button></div>';s_l.value=s.lang}
async function sSave(){const b={school:s_n.value,lang:s_l.value,public_url:s_u.value.trim(),client_key:s_k.value.trim(),new_password:s_pw.value};const f=s_f.files[0];
 if(f){b.logo=await new Promise(r=>{const x=new FileReader();x.onload=()=>r(x.result);x.readAsDataURL(f)})}
 await api('settings','POST',b);
 let note='';if(b.client_key!==s_k.defaultValue)note=String.fromCharCode(10)+'Ключ клиентов изменён — введите новый ключ в клиентских программах';
 alert('Сохранено'+note);
 if(document.documentElement.lang!==s_l.value){document.cookie='lang='+s_l.value+';path=/;max-age=31536000';location.reload()}else settings()}
async function log(){const d=await api('log');
 main.innerHTML='<h2>Журнал</h2>'+d.map(l=>'<div class=card>'+new Date(l.ts*1000).toLocaleString()+' · <b>'+esc(l.pc)+'</b> · '+esc(l.teacher)+' · '+esc(l.event)+'</div>').join('')}
const tname=id=>{const t=TEA.find(t=>t.id==id);return t?t.full_name:'— не найден —'};
const roomOpts=R=>R.map(r=>`<option value="${esc(r)}">`).join('');
const teaOpts=()=>TEA.map(t=>`<option value=${t.id}>${esc(t.full_name)}</option>`).join('');
async function sched(){const [L,T2,R]=await Promise.all([api('lessons'),api('teachers'),api('rooms')]);TEA=T2;
 if(!curRoom||!R.includes(curRoom))curRoom=R[0]||'';
 const rl=L.filter(x=>x.room==curRoom),slots=[...new Set(rl.map(x=>x.start+'–'+x.end))].sort();
 main.innerHTML=`<h2>Расписание уроков</h2>
 <div class=card><div class=row><b>Кабинет:</b><select onchange="curRoom=this.value;sched()" style="width:auto;min-width:170px">${R.map(r=>`<option ${r==curRoom?'selected':''}>${esc(r)}</option>`).join('')}</select></div>
 <small>Во время урока устройства кабинета разблокируются автоматически, на перемене и после уроков блокируются.</small>
 <div style="overflow-x:auto"><table><tr><th></th>${DAYN.slice(0,6).map(d=>`<th>${d}</th>`).join('')}</tr>
 ${slots.map(sl=>`<tr><th>${sl}</th>${[0,1,2,3,4,5].map(w=>`<td>${rl.filter(x=>x.weekday==w&&x.start+'–'+x.end==sl).map(x=>`<div class=cell>${esc(x.subject||'')}<small>${esc(tname(x.teacher_id))} ${esc(x.class_name||'')}</small></div>`).join('')}</td>`).join('')}</tr>`).join('')||'<tr><td>В этом кабинете пока нет уроков</td></tr>'}</table></div></div>
 <div class=card><b>Добавить урок</b>
 <div class=row style="justify-content:flex-start">${DAYN.map((d,i)=>`<label class=c><input type=checkbox class=dchk value=${i} ${i<5?'checked':''}> ${d}</label>`).join('')}</div>
 <div class=row><input id=l_s type=time value="08:30" style="flex:1"><input id=l_e type=time value="09:15" style="flex:1"></div>
 <input id=l_room list=rooms placeholder="Кабинет (как в списке устройств)" value="${esc(curRoom)}"><datalist id=rooms>${roomOpts(R)}</datalist>
 <select id=l_t>${teaOpts()}</select><input id=l_sub placeholder="Предмет"><input id=l_cls placeholder="Класс / группа (например 9А)">
 <button onclick=lAdd()>➕ Добавить</button></div>
 <div class=card><b>Все уроки</b>${L.map(x=>`<div class=row style="padding:4px 0"><span>${DAYN[x.weekday]} ${x.start}–${x.end} · ${esc(x.room)} · ${esc(x.subject||'')} · ${esc(tname(x.teacher_id))} ${esc(x.class_name||'')}</span><button class=g onclick="lDel(${x.id})">🗑</button></div>`).join('')}</div>`}
async function lAdd(){const ds=[...document.querySelectorAll('.dchk:checked')].map(x=>+x.value);if(!ds.length){alert('Выберите дни');return}
 const room=l_room.value.trim();for(const w of ds)await api('lessons','POST',{weekday:w,start:l_s.value,end:l_e.value,room,teacher_id:+l_t.value||null,subject:l_sub.value,class_name:l_cls.value});
 curRoom=room;sched()}
async function lDel(id){await api('lessons/'+id,'DELETE');sched()}
async function exams(){const [E,T2,R]=await Promise.all([api('exams'),api('teachers'),api('rooms')]);TEA=T2;
 main.innerHTML=`<h2>Экзамены</h2><div class=card><b>Создать экзамен</b>
 <input id=e_title placeholder="Название (например: Алгебра, 9А)">
 <div class=row><input id=e_d type=date value="${new Date().toISOString().slice(0,10)}" style="flex:1"><input id=e_s type=time value="09:00" style="flex:1"><input id=e_e type=time value="11:00" style="flex:1"></div>
 <input id=e_room list=rooms2 placeholder="Кабинет"><datalist id=rooms2>${roomOpts(R)}</datalist><select id=e_t>${teaOpts()}</select>
 <button onclick=eAdd()>➕ Создать экзамен</button>
 <small>На время экзамена устройства кабинета разблокируются, а расписание уроков в это время не действует.</small></div>`+
 E.map(x=>`<div class=card><div class=row><span><b>${esc(x.title)}</b><br>${esc(x.date)} · ${x.start}–${x.end} · ${esc(x.room)} · ${esc(tname(x.teacher_id))}</span><button class=r onclick="eDel(${x.id})">🗑</button></div></div>`).join('')}
async function eAdd(){await api('exams','POST',{title:e_title.value,date:e_d.value,start:e_s.value,end:e_e.value,room:e_room.value,teacher_id:+e_t.value||null});exams()}
async function eDel(id){await api('exams/'+id,'DELETE');exams()}
async function overview(){OV=await api('overview');
 main.innerHTML=`<div class=row><h2>Все преподаватели и уроки</h2><button class=g onclick="exportCsv()">⬇️ CSV</button></div>
 <div class=card style="overflow-x:auto"><table><tr><th>Преподаватель</th><th>Предмет</th><th>Статус</th><th>Уроков/нед</th><th>Часов/нед</th><th>Расписание</th></tr>
 ${OV.map(t=>`<tr><td><b>${esc(t.full_name)}</b></td><td>${esc(t.subject||'—')}</td><td>${esc(t.status)}</td><td>${t.lessons.length}</td><td>${(t.week_minutes/60).toFixed(1)}</td>
 <td>${t.lessons.map(l=>`${DAYN[l.weekday]} ${l.start}–${l.end} ${esc(l.room)} ${esc(l.class_name||'')}`).join('<br>')||'—'}</td></tr>`).join('')}</table></div>`}
function exportCsv(){const nl=String.fromCharCode(10),q=v=>'"'+String(v??'').split('"').join('""')+'"';
 const rows=[['Преподаватель','Предмет','Статус','День','Начало','Конец','Кабинет','Класс'].map(q).join(';')];
 OV.forEach(t=>{if(!t.lessons.length)rows.push([t.full_name,t.subject,t.status,'','','','',''].map(q).join(';'));
  t.lessons.forEach(l=>rows.push([t.full_name,t.subject,t.status,DAYN[l.weekday],l.start,l.end,l.room,l.class_name].map(q).join(';')))});
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([String.fromCharCode(65279)+rows.join(nl)],{type:'text/csv'}));a.download='teachers_lessons.csv';a.click()}
if(T)start();
</script></body></html>"""


# ====================================================================
APP_HTML = r"""<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel=manifest href=/manifest-app.json><meta name=theme-color content="#0071e3"><title>EduLock — преподаватель</title>
<style>__CSS__
video{width:100%;border-radius:16px;margin:8px 0}.codein{font-size:30px;text-align:center;letter-spacing:.3em}
.sub{text-align:center;color:var(--sub);margin-bottom:12px}label.rem{display:flex;gap:8px;align-items:center;margin:6px 2px}label.rem input{width:auto;margin:0}</style>
<body><div class=box><div class=ico>🔐</div><h2>EduLock</h2>
<div class=sub>Разблокировка умной доски и компьютера</div>
<input id=l placeholder="Логин" autocomplete=username><input id=p type=password placeholder="Пароль" autocomplete=current-password>
<label class=rem><input type=checkbox id=rem checked> Запомнить логин на этом телефоне</label>
<input id=code class=codein inputmode=numeric maxlength=7 placeholder="Код с экрана">
<button onclick=go()>🔓 Разблокировать по коду</button>
<button class=g onclick=scan()>📷 Сканировать QR-код</button>
<video id=v hidden playsinline muted></video>
<button class=lock onclick=lk()>🔒 Заблокировать по коду</button><div id=m></div></div>
<script>
l.value=localStorage.getItem('lg')||'';
function msg(t,c){m.innerHTML='<div class='+c+'>'+t+'</div>'}
async function call(url,body){
 if(rem.checked)localStorage.setItem('lg',l.value);
 const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({login:l.value,password:p.value},body))});
 return r.json()}
async function send(body){const d=await call('/api/teacher_login',body);
 if(!d.ok){msg('⛔ '+(d.error||'Ошибка'),'err');return}
 msg('✅ Добро пожаловать, '+d.name+'!','ok');if(d.warning)m.innerHTML+='<div class=warn>⚠️ '+d.warning+'</div>'}
function getCode(){const c=code.value.replace(/\D/g,'');if(c.length<6){msg('Введите 6-значный код с экрана','err');return null}return c}
function go(){const c=getCode();if(c)send({code:c})}
async function lk(){const c=getCode();if(!c)return;const d=await call('/api/teacher_lock',{code:c});
 msg(d.ok?'🔒 Заблокировано':'⛔ '+(d.error||'Ошибка'),d.ok?'ok':'err')}
function fromQr(txt){const mm=/\/t\/([^\/?#]+)/.exec(txt);if(!mm){msg('Это не QR-код EduLock','err');return}send({pc:decodeURIComponent(mm[1])})}
async function scan(){
 if(!('BarcodeDetector' in window)||!navigator.mediaDevices){msg('Сканер недоступен в этом браузере или по http. Введите код с экрана либо наведите обычную камеру телефона на QR-код.','warn');return}
 try{const st=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
  v.srcObject=st;v.hidden=false;await v.play();const det=new BarcodeDetector({formats:['qr_code']});
  const t=setInterval(async()=>{const r=await det.detect(v).catch(()=>[]);
   if(r.length){clearInterval(t);st.getTracks().forEach(x=>x.stop());v.hidden=true;fromQr(r[0].rawValue)}},400)
 }catch(e){msg('Нет доступа к камере (для камеры нужен https). Введите код с экрана.','warn')}}
</script></body></html>"""
APP_HTML = APP_HTML.replace("__CSS__", TEACHER_HTML.split("<style>")[1].split("</style>")[0])


def local_ip():
    try:
        k = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        k.connect(("8.8.8.8", 80))
        ip = k.getsockname()[0]
        k.close()
        return ip
    except Exception:
        return "127.0.0.1"


def fmt_hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


TR_GUI = {
    "EduLock — сервер": ("EduLock — server", "EduLock — sunucu"),
    "● Остановлен": ("● Stopped", "● Durduruldu"),
    "● Работает": ("● Running", "● Çalışıyor"),
    "Сервер блокировки экранов": ("Screen lock server", "Ekran kilitleme sunucusu"),
    "Порт": ("Port", "Port"),
    "Пароль админа": ("Admin password", "Yönetici parolası"),
    "Ключ клиентов": ("Client key", "İstemci anahtarı"),
    "▶ Запустить": ("▶ Start", "▶ Başlat"),
    "■ Остановить": ("■ Stop", "■ Durdur"),
    "Сессия (работает)": ("Session (uptime)", "Oturum (çalışma süresi)"),
    "Всего часов работы": ("Total hours of operation", "Toplam çalışma saati"),
    "Компьютеров онлайн": ("Computers online", "Çevrimiçi bilgisayar"),
    "Умных досок онлайн": ("Smart boards online", "Çevrimiçi akıllı tahta"),
    "Заходов в веб-панель": ("Web panel visits", "Web panel ziyaretleri"),
    "Последний заход": ("Last visit", "Son ziyaret"),
    "Доступ к веб-панели": ("Web panel access", "Web panel erişimi"),
    "Панель:": ("Panel:", "Panel:"),
    "Умная доска:": ("Smart board:", "Akıllı tahta:"),
    "Копировать": ("Copy", "Kopyala"),
    "Открыть": ("Open", "Aç"),
    "ч": ("h", "sa"),
    "Язык": ("Language", "Dil"),
    "Настройки сохраняются автоматически": ("Settings are saved automatically",
                                            "Ayarlar otomatik kaydedilir"),
}
GUI = {"thread": None, "port": 8000, "again": False, "draft": {}}


def gt(s):
    i = {"en": 0, "tr": 1}.get(DEFAULT_LANG)
    v = TR_GUI.get(s)
    return v[i] if (i is not None and v) else s


def run_gui(default_port):
    """Окно управления сервером. При смене языка окно пересоздаётся, сервер продолжает работать."""
    GUI["port"] = int(load_config().get("port") or default_port)
    while True:
        GUI["again"] = False
        _gui_window()
        if not GUI["again"]:
            break


def _gui_window():
    """Окно: старт/стоп, часы работы, ПК, доски, доступ в панель, язык. Настройки сохраняются."""
    import tkinter as tk
    import webbrowser
    BG, CARD, TXT, SUB, BLUE, GREEN, RED = "#f5f5f7", "#ffffff", "#1d1d1f", "#6e6e73", "#0071e3", "#30b350", "#ff3b30"
    F = "Segoe UI"
    init_db()
    root = tk.Tk()
    root.title(gt("EduLock — сервер"))
    root.configure(bg=BG, padx=22, pady=18)
    root.minsize(640, 660)
    draft = GUI["draft"]

    def btn(parent, text, cmd, bg=BLUE, fg="white"):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, relief="flat", bd=0, padx=18,
                         pady=8, font=(F, 10, "bold"), activebackground=bg, cursor="hand2")

    top = tk.Frame(root, bg=BG)
    top.pack(fill="x")
    tk.Label(top, text="EduLock", bg=BG, fg=TXT, font=(F, 26, "bold")).pack(side="left")
    status = tk.Label(top, text=gt("● Остановлен"), bg=BG, fg=RED, font=(F, 12, "bold"))
    status.pack(side="right")
    lv = tk.StringVar(value=LANGS[DEFAULT_LANG])
    lang_menu = tk.OptionMenu(top, lv, *LANGS.values(), command=lambda name: on_lang(name))
    lang_menu.config(bg=CARD, relief="flat", font=(F, 10), highlightthickness=0)
    lang_menu.pack(side="right", padx=12)
    tk.Label(top, text=gt("Язык") + ":", bg=BG, fg=SUB, font=(F, 10)).pack(side="right")
    tk.Label(root, text=gt("Сервер блокировки экранов"), bg=BG, fg=SUB, font=(F, 11)).pack(anchor="w")

    cfg = tk.Frame(root, bg=CARD, highlightbackground="#e5e5ea", highlightthickness=1, padx=14, pady=12)
    cfg.pack(fill="x", pady=(14, 6))

    def field(label, val, col, show=None, w=16):
        tk.Label(cfg, text=label, bg=CARD, fg=SUB, font=(F, 9)).grid(row=0, column=col, sticky="w", padx=6)
        e = tk.Entry(cfg, width=w, show=show, relief="flat", bg="#f2f2f7", font=(F, 11))
        e.grid(row=1, column=col, padx=6, ipady=5)
        e.insert(0, val)
        return e
    e_port = field(gt("Порт"), str(draft.get("port", GUI["port"])), 0, w=8)
    e_pass = field(gt("Пароль админа"), draft.get("pass", ADMIN_PASS), 1, show="•")
    e_key = field(gt("Ключ клиентов"), draft.get("key", CLIENT_KEY), 2, show="•")
    tk.Label(cfg, text=gt("Настройки сохраняются автоматически"), bg=CARD, fg=SUB,
             font=(F, 9)).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(8, 0))

    def start():
        global ADMIN_PASS, CLIENT_KEY
        if GUI["thread"] and GUI["thread"].is_alive():
            return
        ADMIN_PASS = e_pass.get() or "admin"
        CLIENT_KEY = e_key.get() or "edulock-key"
        try:
            GUI["port"] = int(e_port.get())
        except ValueError:
            return
        save_config(port=GUI["port"], admin_pass=ADMIN_PASS, client_key=CLIENT_KEY, lang=DEFAULT_LANG)
        SERVER["srv"] = None
        GUI["thread"] = threading.Thread(target=run_server, args=("0.0.0.0", GUI["port"]), daemon=True)
        GUI["thread"].start()

    def stop():
        if SERVER["srv"]:
            SERVER["srv"].should_exit = True

    br = tk.Frame(root, bg=BG)
    br.pack(fill="x", pady=6)
    btn(br, gt("▶ Запустить"), start, GREEN).pack(side="left")
    btn(br, gt("■ Остановить"), stop, RED).pack(side="left", padx=8)

    grid = tk.Frame(root, bg=BG)
    grid.pack(fill="x", pady=4)
    grid.columnconfigure((0, 1), weight=1)

    def card(title, r, c):
        f = tk.Frame(grid, bg=CARD, highlightbackground="#e5e5ea", highlightthickness=1, padx=14, pady=10)
        f.grid(row=r, column=c, padx=5, pady=5, sticky="nsew")
        tk.Label(f, text=title, bg=CARD, fg=SUB, font=(F, 10)).pack(anchor="w")
        v = tk.Label(f, text="—", bg=CARD, fg=TXT, font=(F, 22, "bold"))
        v.pack(anchor="w")
        return v
    v_up = card(gt("Сессия (работает)"), 0, 0)
    v_total = card(gt("Всего часов работы"), 0, 1)
    v_pcs = card(gt("Компьютеров онлайн"), 1, 0)
    v_boards = card(gt("Умных досок онлайн"), 1, 1)
    v_hits = card(gt("Заходов в веб-панель"), 2, 0)
    v_last = card(gt("Последний заход"), 2, 1)

    ul = tk.Frame(root, bg=CARD, highlightbackground="#e5e5ea", highlightthickness=1, padx=14, pady=10)
    ul.pack(fill="x", pady=8)
    tk.Label(ul, text=gt("Доступ к веб-панели"), bg=CARD, fg=SUB, font=(F, 10)).pack(anchor="w")
    urls = []

    def url_row(label, path):
        r = tk.Frame(ul, bg=CARD)
        r.pack(fill="x", pady=3)
        tk.Label(r, text=label, bg=CARD, fg=TXT, font=(F, 10, "bold"), width=14, anchor="w").pack(side="left")
        lab = tk.Label(r, text="", bg=CARD, fg=BLUE, font=(F, 10))
        lab.pack(side="left")

        def get():
            return (PUBLIC_URL or f"http://{local_ip()}:{GUI['port']}") + path
        btn(r, gt("Копировать"), lambda: (root.clipboard_clear(), root.clipboard_append(get())),
            "#e5e5ea", TXT).pack(side="right", padx=(6, 0))
        btn(r, gt("Открыть"), lambda: webbrowser.open(get())).pack(side="right")
        urls.append((lab, get))
    url_row(gt("Панель:"), "/admin")
    url_row(gt("Умная доска:"), "/board?pc=PC-01")

    def refresh():
        srv = SERVER["srv"]
        running = bool(GUI["thread"] and GUI["thread"].is_alive() and srv and srv.started)
        status.config(text=gt("● Работает") if running else gt("● Остановлен"), fg=GREEN if running else RED)
        now = time.time()
        v_up.config(text=fmt_hms(now - STATS["start"]) if running else "—")
        v_pcs.config(text=str(len(STATS["conns"])) if running else "—")
        v_boards.config(text=str(boards_online()) if running else "—")
        try:
            v_total.config(text=f"{float(get_setting('total_seconds', '0') or 0) / 3600:.1f} {gt('ч')}")
        except Exception:
            pass
        v_hits.config(text=str(STATS["panel_hits"]))
        v_last.config(text=time.strftime("%H:%M:%S", time.localtime(STATS["last_panel"]))
                      if STATS["last_panel"] else "—")
        for lab, get in urls:
            lab.config(text=get())
        root.after(1000, refresh)

    def on_lang(name):
        global DEFAULT_LANG
        DEFAULT_LANG = next(k for k, v in LANGS.items() if v == name)
        save_config(lang=DEFAULT_LANG)
        GUI["draft"] = {"port": e_port.get(), "pass": e_pass.get(), "key": e_key.get()}
        GUI["again"] = True  # окно пересоздаётся на новом языке, сервер не трогаем
        root.after(50, root.destroy)

    def on_close():
        GUI["again"] = False
        stop()
        root.after(300, root.destroy)
    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    if not (GUI["thread"] and GUI["thread"].is_alive()):
        start()  # сразу запускаем при открытии окна
    root.mainloop()


# ====================================================================
#                              РЕЖИМ VPS
# ====================================================================
def print_info(host, port):
    ip = local_ip()
    base = PUBLIC_URL or f"http://{ip}:{port}"
    line = "=" * 64
    print(line)
    print("  EduLock — режим VPS")
    print(line)
    print(f"  Панель администратора : {base}/admin")
    print(f"  Доска (пример)        : {base}/board?pc=PC-01")
    print(f"  Пароль администратора : {ADMIN_PASS}")
    print(f"  Ключ клиентов         : {CLIENT_KEY}   <- вводится в клиентских программах")
    print(f"  Адрес для клиентов    : {base}")
    print(f"  Язык по умолчанию     : {LANGS[DEFAULT_LANG]}")
    print(f"  Файл настроек         : {CONFIG_PATH}")
    print(f"  База данных           : {DB_PATH}")
    print(line)
    if ADMIN_PASS == "admin" or CLIENT_KEY == "edulock-key":
        print("  ВНИМАНИЕ: пароль/ключ по умолчанию! Смените их в панели (Настройки).")
    if not PUBLIC_URL:
        print("  Для HTTPS и QR на досках с вашим доменом: --public-url https://ваш.домен")
        print("  и поставьте перед сервером Caddy/nginx (см. --install-service).")
    print(line, flush=True)


def vps_prepare(host, port):
    """Первый запуск на VPS: если пароль/ключ не заданы — создаём случайные и сохраняем в файл настроек."""
    global ADMIN_PASS, CLIENT_KEY
    cfg = load_config()
    fresh = {}
    if not cfg.get("admin_pass") and not os.environ.get("ADMIN_PASS"):
        fresh["admin_pass"] = secrets.token_urlsafe(9)
    if not cfg.get("client_key") and not os.environ.get("CLIENT_KEY"):
        fresh["client_key"] = secrets.token_urlsafe(12)
    if fresh:
        save_config(**fresh)
        ADMIN_PASS = fresh.get("admin_pass", ADMIN_PASS)
        CLIENT_KEY = fresh.get("client_key", CLIENT_KEY)
        print("Созданы случайные пароль администратора и ключ клиентов (сохранены в файл настроек).")
    save_config(host=host, port=port, lang=DEFAULT_LANG, public_url=PUBLIC_URL)


def install_service(host, port):
    """Устанавливает EduLock как systemd-службу: автозапуск при загрузке VPS и перезапуск при сбое."""
    import shutil
    import subprocess
    from urllib.parse import urlparse
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        sys.exit("--install-service работает только на Linux с systemd.")
    if os.geteuid() != 0:
        sys.exit("Запустите от root:  sudo python3 " + os.path.abspath(__file__) + " --vps --install-service")
    unit = f"""[Unit]
Description=EduLock server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={BASE_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=EDULOCK_CONFIG={CONFIG_PATH}
Environment=EDULOCK_DB={DB_PATH}
Environment=EDULOCK_MEDIA={MEDIA_DIR}
ExecStart={sys.executable} {os.path.abspath(__file__)} --vps --host {host} --port {port}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    with open("/etc/systemd/system/edulock.service", "w", encoding="utf-8") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "edulock"], check=True)
    print("Служба edulock установлена и запущена.")
    print("  Статус : systemctl status edulock")
    print("  Логи   : journalctl -u edulock -f")
    print("  Рестарт: systemctl restart edulock")
    print(f"  Откройте порт: ufw allow {port}/tcp   (если используется ufw)")
    print_info(host, port)
    if PUBLIC_URL.startswith("https://"):
        dom = urlparse(PUBLIC_URL).hostname
        print("HTTPS через Caddy (apt install caddy), файл /etc/caddy/Caddyfile:")
        print(f"\n{dom} {{\n    reverse_proxy 127.0.0.1:{port}\n}}\n")
        print("Затем перезапустите службу с --host 127.0.0.1 (порт наружу не нужен) или закройте порт файрволом.")


def main():
    global DEFAULT_LANG, PUBLIC_URL
    ap = argparse.ArgumentParser(description="EduLock server")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--nogui", action="store_true", help="без окна (для Linux-сервера без экрана)")
    ap.add_argument("--vps", action="store_true",
                    help="режим VPS: без окна, пароли создаются автоматически, настройки сохраняются")
    ap.add_argument("--install-service", action="store_true",
                    help="установить systemd-службу (Linux, от root) — автозапуск на VPS")
    ap.add_argument("--public-url", default=None,
                    help="внешний адрес сервера, например https://edulock.example.com (для QR на досках)")
    ap.add_argument("--lang", choices=list(LANGS), default=None, help="язык по умолчанию: ru / en / tr")
    ap.add_argument("--show-config", action="store_true", help="показать адреса, пароль и ключ и выйти")
    a = ap.parse_args()
    cfg = load_config()
    host = a.host or cfg.get("host") or "0.0.0.0"
    port = a.port or int(cfg.get("port") or 8000)
    if a.lang:
        DEFAULT_LANG = a.lang
        save_config(lang=a.lang)
    if a.public_url is not None:
        PUBLIC_URL = a.public_url.rstrip("/")
        save_config(public_url=PUBLIC_URL)
    if a.show_config:
        return print_info(host, port)
    if a.vps or a.install_service:
        vps_prepare(host, port)
        if a.install_service:
            return install_service(host, port)
        print_info(host, port)
        return run_server(host, port)
    if a.nogui:
        return run_server(host, port)
    try:
        run_gui(port)
    except Exception as ex:  # нет tkinter / нет экрана -> обычный режим
        print("Окно недоступно (" + str(ex) + "), запускаю без GUI")
        run_server(host, port)


if __name__ == "__main__":
    main()
