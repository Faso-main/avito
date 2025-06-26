import time
import re
import random
import os
import sys
import logging
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError, Page, BrowserContext

# --- Конфигурация логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Настройки имён файлов ---
MAIN_FILE = "ads_links.txt"  # Основной файл со ссылками на объявления
SELLER_IDS_FILE = "seller_ids.txt"  # Файл c ID продавцов
CURRENT_LINK_FILE = "current_link.txt"  # Файл с текущей (последней обработанной) ссылкой

# --- Селекторы Playwright ---
SELECTORS = {
    "messenger_header": '[data-marker="header/messenger"]',
    "ad_link_type1": '.styles-item-m0DD4 a[href]',
    "ad_link_type2_container": '.index-content-c0K1j',
    "ad_link_type2_item": '.iva-item-title-CdRXl a[href]',
    "pagination_next_button": '[data-marker="pagination-button/nextPage"]',
    "avito_logo": 'div.index-logo-K90gi',
    "seller_name_primary": '.style-nameWrapper-vmkRf span',
    "seller_name_alt": 'span.styles-module-size_ms-YUHT8',
    "seller_name_sticky": '.style-sticky-header-seller-text-mVIXS',
    "total_views": '[data-marker="item-view/total-views"]',
    "message_button": '[data-marker="messenger-button/button"]',
    "mini_messenger_link": '[data-marker="mini-messenger/messenger-page-link"]',
    "reply_input": 'textarea[data-marker="reply/input"]',
    "send_button": '[data-marker="reply/send"]',
}

# --- Вспомогательные функции ---
def random_sleep(min_sec: float = 1.0, max_sec: float = 2.0):
    """Случайная пауза между действиями."""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)

def parse_synonyms(synonyms_text: str) -> dict[str, list[str]]:
    """
    Разбор текста с синонимами формата:
    'слово: синоним1, синоним2; слово2: синоним3, ...'
    Возвращает словарь { 'слово': ['синоним1', 'синоним2'], ... }
    """
    result = {}
    synonyms_text = synonyms_text.strip()
    if not synonyms_text:
        return result

    lines = synonyms_text.split(";")
    for line in lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        word_part, syn_part = line.split(":", 1)
        base_word = word_part.strip().lower()
        synonyms = [s.strip() for s in syn_part.split(",") if s.strip()]
        if synonyms:
            result[base_word] = synonyms
    return result

def unique_message(original_text: str, synonyms_dict: dict[str, list[str]]) -> str:
    """Подстановка случайных синонимов вместо некоторых слов."""
    words = original_text.split()
    result_words = []
    for w in words:
        low = w.lower()
        if low in synonyms_dict:
            syn = random.choice(synonyms_dict[low])
            result_words.append(syn)
        else:
            result_words.append(w)
    return " ".join(result_words)

def collect_ads_links(page: Page) -> list[str]:
    """Собираем ссылки на объявления, учитывая разные варианты верстки."""
    links = []
    # Первый тип
    elems1 = page.query_selector_all(SELECTORS["ad_link_type1"])
    for el in elems1:
        href = el.get_attribute("href")
        if href:
            links.append(href)

    # Второй тип
    container = page.query_selector(SELECTORS["ad_link_type2_container"])
    if container:
        elems2 = container.query_selector_all(SELECTORS["ad_link_type2_item"])
        for el in elems2:
            href = el.get_attribute("href")
            if href:
                links.append(href)
    return links

def infinite_scroll_and_paginate(page: Page, existing_links: set[str], max_links: int = 200) -> list[str]:
    """
    «Бесконечная» прокрутка и переход на следующие страницы,
    чтобы собрать не более max_links *новых* ссылок.
    Возвращает список новых ссылок (без дублей).
    existing_links — множество уже имеющихся ссылок, чтобы исключать дубли.
    """
    global start_url # Это переменная должна быть изменяемой, если start_url меняется в зависимости от пагинации
    new_collected = []
    total_new = 0

    while True:
        old_count = total_new

        # 1) Бесконечная прокрутка
        while True:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            random_sleep(2, 4)

            found_now = 0
            current_links = collect_ads_links(page)
            for lk in current_links:
                if lk.startswith("/"):
                    lk = "https://www.avito.ru" + lk
                if lk not in existing_links:
                    existing_links.add(lk)
                    new_collected.append(lk)
                    found_now += 1
                    total_new += 1
                    if total_new >= max_links:
                        logger.info(f"Достигнут лимит {max_links} ссылок. Останавливаем сбор.")
                        start_url = page.url # Обновляем перед выходом
                        return new_collected

            if found_now == 0:
                # Если новые ссылки не найдены после прокрутки, прерываем внутренний цикл
                break

        # 2) Переход на следующую страницу
        if total_new == old_count: # Если новые ссылки не были найдены прокруткой, пытаемся перейти на следующую страницу
            next_link_el = page.query_selector(SELECTORS["pagination_next_button"])
            if next_link_el:
                next_href = next_link_el.get_attribute("href")
                if next_href:
                    if next_href.startswith("/"):
                        next_href = "https://www.avito.ru" + next_href
                    logger.info(f"Переходим на следующую страницу: {next_href}")
                    try:
                        page.goto(next_href, timeout=60000, wait_until="domcontentloaded")
                        random_sleep(3, 5)
                        start_url = page.url # Обновляем после успешного перехода
                    except TimeoutError:
                        logger.warning("Не удалось загрузить следующую страницу. Прерываем сбор.")
                        start_url = page.url # Обновляем URL даже при ошибке
                        return new_collected
                else:
                    logger.info("Кнопка 'Далее' найдена, но без атрибута href. Останавливаемся.")
                    start_url = page.url # Обновляем перед выходом
                    return new_collected
            else:
                logger.info("Нет кнопки «Следующая страница». Останавливаем сбор.")
                start_url = page.url # Обновляем перед выходом
                return new_collected
    
    start_url = page.url # Финальное обновление (на случай выхода из цикла)
    return new_collected

def refresh_page_via_logo(page: Page):
    """Клик по логотипу Авито, чтобы «обновить» или вернуться на главную."""
    logo_el = page.query_selector(SELECTORS["avito_logo"])
    if logo_el:
        try:
            logo_el.click()
            random_sleep(3, 6)
        except Exception as e:
            logger.warning(f"Не удалось кликнуть по логотипу Авито: {e}")
    else:
        logger.warning("Не удалось найти элемент с логотипом Авито для клика.")

def wait_for_messenger_element(page: Page, timeout_minutes: int = 10) -> bool:
    """
    Ждём появления элемента Личные сообщения (селектор data-marker='header/messenger')
    максимум timeout_minutes минут.
    Возвращает True, если элемент найден, иначе False.
    """
    total_seconds = timeout_minutes * 60
    interval = 2
    waited = 0
    while waited < total_seconds:
        el = page.query_selector(SELECTORS["messenger_header"])
        if el:
            return True
        time.sleep(interval)
        waited += interval
    return False

def get_seller_identifier(page: Page) -> str | None:
    """
    Пытаемся извлечь имя/ID продавца по нескольким вариантам селекторов,
    включая обработку случаев, когда имя изначально скрыто многоточием.
    """
    for attempt in range(3):
        seller_element = None
        try:
            seller_element = page.wait_for_selector(SELECTORS["seller_name_sticky"], timeout=5000)
        except TimeoutError:
            # Если не найден сразу, пытаемся прокрутить и снова найти
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            random_sleep(3, 5)
            try:
                seller_element = page.wait_for_selector(SELECTORS["seller_name_sticky"], timeout=5000)
            except TimeoutError:
                pass # Всё ещё не найдено, вернёт None

        if seller_element:
            seller_text = seller_element.text_content().strip()
            if seller_text == '...':
                if attempt == 2: # На последней попытке пробуем получить из атрибута title
                    title_attribute = seller_element.get_attribute('title')
                    if title_attribute:
                        return title_attribute.strip()
                # Если многоточие, прокручиваем и пробуем снова
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                random_sleep(3, 5)
                continue
            return seller_text
        else:
            # Если элемент не найден, прокручиваем вниз и пробуем снова
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            random_sleep(3, 5)
    return None

def get_ad_views(page: Page, min_views: int) -> int | None:
    """
    Пытаемся получить количество просмотров объявления.
    Возвращает количество просмотров или None, если не найдено после повторных попыток.
    """
    if min_views == 0:
        return 0 # Нет необходимости проверять, если min_views равен 0

    for attempt_view in range(3):
        try:
            views_el = page.wait_for_selector(SELECTORS["total_views"], timeout=5000)
            random_sleep(1, 2) # Меньшая задержка после нахождения элемента
            views_text = views_el.inner_text()
            match = re.search(r'(\d+)', views_text)
            views_number = int(match.group(1)) if match else 0
            return views_number
        except TimeoutError:
            logger.warning(f"Элемент просмотров не найден (попытка {attempt_view+1}/3). Прокручиваем и повторяем.")
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            random_sleep(2, 3)
        except Exception as e:
            logger.warning(f"Ошибка извлечения просмотров (попытка {attempt_view+1}/3): {e}. Прокручиваем и повторяем.")
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            random_sleep(2, 3)
    return None

def attempt_go_to_ad_url(page: Page, ad_url: str) -> bool:
    """Пытается перейти по URL объявления с повторными попытками и обновлениями."""
    for attempt_ad in range(3):
        try:
            page.goto(ad_url, timeout=60000, wait_until="domcontentloaded")
            random_sleep(3, 5)
            return True
        except TimeoutError:
            logger.warning(f"Не удалось загрузить объявление {ad_url} (попытка {attempt_ad+1}/3). Обновляем и повторяем.")
            refresh_page_via_logo(page)
            random_sleep(3, 5)
        except Exception as e:
            logger.error(f"Неожиданная ошибка при загрузке объявления {ad_url} (попытка {attempt_ad+1}/3): {e}")
            refresh_page_via_logo(page)
            random_sleep(3, 5)
    return False

def click_and_enter_messenger(page: Page, ad_url: str) -> bool:
    """Кликает по кнопке «Написать сообщение» и переходит в область ввода сообщений мессенджера."""
    for attempt_btn in range(3):
        try:
            # Прокручиваем вниз на случай, если кнопка не видна
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            random_sleep(1, 2)
            write_btn = page.wait_for_selector(SELECTORS["message_button"], timeout=10000)
            
            write_btn.click()
            random_sleep(3, 5)

            # Ждём и кликаем по ссылке мини-мессенджера, чтобы развернуть чат
            messenger_link_el = page.wait_for_selector(SELECTORS["mini_messenger_link"], timeout=9000)
            messenger_link_el.click()
            random_sleep(3, 5)
            return True
        except TimeoutError:
            logger.warning(f"Не удалось найти кнопку или ссылку мессенджера (попытка {attempt_btn+1}/3). Обновляем и повторяем.")
            refresh_page_via_logo(page)
            try:
                page.goto(ad_url, timeout=60000, wait_until="domcontentloaded")
                random_sleep(5, 7)
            except Exception as e:
                logger.error(f"Не удалось перезагрузить URL объявления после проблемы с мессенджером: {e}")
                return False
        except Exception as e:
            logger.error(f"Неожиданная ошибка при взаимодействии с кнопками мессенджера (попытка {attempt_btn+1}/3): {e}")
            return False
    return False

def find_and_fill_textarea(page: Page, message_text: str) -> bool:
    """Находит текстовое поле для сообщения и заполняет его заданным текстом."""
    for attempt_ta in range(3):
        try:
            text_area = page.wait_for_selector(SELECTORS["reply_input"], timeout=9000)
            text_area.fill(message_text)
            random_sleep(1, 2)
            return True
        except TimeoutError:
            logger.warning(f"Поле ввода сообщения не найдено (попытка {attempt_ta+1}/3). Повторяем.")
            # Здесь нет refresh_page_via_logo, так как мы уже находимся в контексте мессенджера
            random_sleep(2, 3) # Просто ждём немного и надеемся, что появится
        except Exception as e:
            logger.error(f"Неожиданная ошибка при поиске/заполнении текстового поля (попытка {attempt_ta+1}/3): {e}")
            random_sleep(2, 3)
    return False


# --- Основная функция ---
def main():
    global start_url # Объявляем как глобальную, чтобы она могла быть изменена вспомогательными функциями

    # --- Шаг 0. Спросим про очистку файлов ---
    choice = input("ОЧИСТИТЬ все 3 файла (основной, файл ID, текущая ссылка)?\n"
                   "(Нажмите Enter — очистить, введите любое другое значение — нет): ").strip()
    if choice == "":
        # Очищаем
        for fpath in [MAIN_FILE, SELLER_IDS_FILE, CURRENT_LINK_FILE]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    logger.info(f"Файл {fpath} очищен.")
                except OSError as e:
                    logger.error(f"Ошибка при очистке файла {fpath}: {e}")
        logger.info("Все три файла очищены.\n")
    else:
        logger.info("Продолжаем без очистки файлов.\n")

    # --- Подгрузим/создадим основной файл со ссылками ---
    if not os.path.exists(MAIN_FILE):
        open(MAIN_FILE, 'w', encoding='utf-8').close()
    with open(MAIN_FILE, 'r', encoding='utf-8') as f:
        main_links = [line.strip() for line in f if line.strip()]

    # --- Файл ID продавцов ---
    if not os.path.exists(SELLER_IDS_FILE):
        open(SELLER_IDS_FILE, 'w', encoding='utf-8').close()
    with open(SELLER_IDS_FILE, 'r', encoding='utf-8') as f:
        processed_sellers = set(line.strip() for line in f if line.strip())

    # --- Файл текущей ссылки ---
    current_link = None
    if os.path.exists(CURRENT_LINK_FILE):
        with open(CURRENT_LINK_FILE, 'r', encoding='utf-8') as f:
            c = f.read().strip()
            current_link = c if c else None

    # --- Запрос у пользователя: стартовая страница и макс. кол-во сбора ссылок ---
    start_url = input("Введите URL для сбора ссылок (стартовая страница).\n"
                      "Если оставить пустым, будет использоваться https://www.avito.ru/\n"
                      "> ").strip()
    if not start_url:
        start_url = "https://www.avito.ru/"

    max_links_str = input("Введите максимальное число ссылок для сбора за один раз [по умолчанию 200]: ").strip()
    try:
        max_links = int(max_links_str)
    except ValueError:
        logger.warning("Некорректный ввод для максимального количества ссылок. Используется значение по умолчанию: 200.")
        max_links = 200

    # --- Прочие параметры ---
    logger.info("\nПожалуйста, введите параметры для рассылки:\n")
    messages_count_str = input("Сколько всего сообщений нужно отправить? [по умолчанию 10]: ") or "10"
    min_views_str = input("Минимальное число просмотров? [0 = не проверять]: ") or "0"
    synonyms_text = input("Введите словарь синонимов (формат 'слово: синоним1, синоним2; ...'): ") or ""
    original_message = input("Введите исходный текст сообщения (можно использовать \\n для переноса строк): ")

    try:
        messages_count = int(messages_count_str)
    except ValueError:
        logger.warning("Некорректный ввод для количества сообщений. Используется значение по умолчанию: 10.")
        messages_count = 10

    try:
        min_views = int(min_views_str)
    except ValueError:
        logger.warning("Некорректный ввод для минимального количества просмотров. Используется значение по умолчанию: 0.")
        min_views = 0

    synonyms_dict = parse_synonyms(synonyms_text)

    # Счётчик отправленных сообщений
    sent_messages = 0

    # Папка профиля (для сохранения авторизации)
    user_data_dir = "avito_user_data"

    # Начинаем работу в браузере
    while True:
        context: BrowserContext = None
        try:
            with sync_playwright() as p:
                context = p.firefox.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False
                )
                page = context.pages[0] if context.pages else context.new_page()

                # Переходим на стартовую страницу (или восстанавливаемся)
                try:
                    page.goto(start_url, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    logger.error(f"Не удалось загрузить {start_url}: {e}")
                    logger.info("Попытка загрузить https://www.avito.ru/ вместо этого.")
                    page.goto("https://www.avito.ru/", wait_until="domcontentloaded")

                logger.info("\nОткрылся браузер. Авторизуйтесь (если нужно).")
                logger.info("Скрипт будет ждать появления элемента Личные сообщения (data-marker=\"header/messenger\") "
                            "до 10 минут.\n")

                # Ждём появления элемента сообщений
                if not wait_for_messenger_element(page, timeout_minutes=10):
                    logger.critical("Элемент 'Сообщения' не найден за 10 минут. Прекращаем работу.")
                    break # Выход из основного цикла

                random_sleep(5, 7) # Найдено, ждём случайные 5-7 секунд
                logger.info("Элемент сообщений найден. Начинаем основную логику.\n")

                # --- Главный цикл рассылки сообщений ---
                while sent_messages < messages_count:
                    # 1) Проверяем, надо ли собирать новые ссылки
                    # если основной файл пуст или current_link совпадает с последней ссылкой в main_links
                    try:
                        with open(CURRENT_LINK_FILE, 'r', encoding='utf-8') as cf:
                            current_link = cf.read().strip()
                    except FileNotFoundError:
                        current_link = None

                    need_collect = False
                    if not main_links:
                        need_collect = True
                    else:
                        last_in_main = main_links[-1]
                        if current_link and (current_link == last_in_main):
                            need_collect = True

                    if need_collect:
                        logger.info("\n--- Сбор новых ссылок, так как достигнут конец основного файла (или он пуст). ---")
                        try:
                            page.goto(start_url, timeout=60000, wait_until="domcontentloaded")
                            random_sleep(10, 20)
                        except Exception:
                            logger.error("Не удалось загрузить стартовую страницу для сбора ссылок. Пропускаем сбор.")
                            break

                        existing_links_set = set(main_links) # Конвертируем в set для эффективного поиска
                        new_links = infinite_scroll_and_paginate(page, existing_links_set, max_links=max_links)
                        if new_links:
                            random_sleep(10, 20)
                            with open(MAIN_FILE, 'a', encoding='utf-8') as f:
                                for link in new_links:
                                    f.write(link + "\n")
                            main_links.extend(new_links) # Добавляем в наш список в памяти
                            logger.info(f"Добавлено {len(new_links)} новых ссылок в основной файл.")
                        else:
                            logger.info("Новых ссылок не найдено. Возможно, ничего больше нет.")
                            if not main_links: # Если всё ещё пусто после попытки сбора
                                logger.info("Основной файл остался пустым. Останавливаем скрипт.")
                                break

                    # 2) Перебираем ссылки из main_links, начиная с той, что идёт после current_link
                    start_index = 0
                    if current_link in main_links:
                        idx = main_links.index(current_link)
                        start_index = idx + 1

                    for i in range(start_index, len(main_links)):
                        ad_url = main_links[i]
                        if sent_messages >= messages_count:
                            break

                        # Запишем в файл текущую ссылку
                        with open(CURRENT_LINK_FILE, 'w', encoding='utf-8') as cf:
                            cf.write(ad_url)

                        logger.info(f"\n>>> Переход к объявлению: {ad_url}")
                        if not attempt_go_to_ad_url(page, ad_url):
                            logger.warning(f"Пропускаем объявление {ad_url} из-за ошибок загрузки.")
                            continue

                        # Извлекаем ID продавца
                        seller_id = get_seller_identifier(page)
                        if not seller_id:
                            logger.warning("Продавец не найден для этого объявления. Пропускаем.")
                            continue

                        # Проверяем, не отправляли ли уже этому продавцу
                        if seller_id in processed_sellers:
                            logger.info(f"Продавцу «{seller_id}» уже отправляли сообщение. Пропускаем.")
                            continue

                        # Проверяем просмотры (если задан min_views)
                        if min_views > 0:
                            views_number = get_ad_views(page, min_views)
                            if views_number is None:
                                logger.warning("Не удалось получить число просмотров. Пропускаем объявление.")
                                continue
                            if views_number < min_views:
                                logger.info(f"Просмотров ({views_number}) меньше, чем {min_views}. Пропускаем объявление.")
                                continue
                            logger.info(f"Просмотры: {views_number}")

                        # Ищем кнопку «Написать» и переходим в мессенджер
                        if not click_and_enter_messenger(page, ad_url):
                            logger.warning("Не удалось перейти в мессенджер. Пропускаем объявление.")
                            continue

                        # Формируем итоговое сообщение (с заменой синонимов и переводом \n)
                        final_text = unique_message(original_message, synonyms_dict).replace('\\n', '\n')

                        if not find_and_fill_textarea(page, final_text):
                            logger.warning("Не удалось найти или заполнить поле ввода сообщения. Пропускаем объявление.")
                            continue

                        # Отправляем сообщение
                        try:
                            send_btn = page.wait_for_selector(SELECTORS["send_button"], timeout=5000)
                            random_sleep(1, 2)
                            send_btn.click()
                            random_sleep(2, 3) # Короткая задержка после отправки
                            sent_messages += 1
                            processed_sellers.add(seller_id)
                            with open(SELLER_IDS_FILE, 'a', encoding='utf-8') as sf:
                                sf.write(seller_id + "\n")
                            logger.info(f"Сообщение отправлено продавцу «{seller_id}». (Всего отправлено: {sent_messages})")
                        except TimeoutError:
                            logger.warning("Кнопка 'Отправить' не найдена. Пропускаем объявление.")
                        except Exception as e:
                            logger.error(f"Ошибка при отправке сообщения: {e}. Пропускаем объявление.")

                        # Если достигнут лимит сообщений, прерываем
                        if sent_messages >= messages_count:
                            break

                        logger.info("Задержка 5-15 минут перед следующим сообщением...")
                        random_sleep(300, 900)

                    # Проверяем лимит после завершения внутреннего цикла (либо по исчерпанию, либо по break)
                    if sent_messages >= messages_count:
                        logger.info(f"\nЛимит в {messages_count} сообщений достигнут.")
                        break # Выход из основного цикла while

                logger.info(f"\nГотово! Всего отправлено {sent_messages} (из {messages_count}).")
                
        except Exception as e:
            logger.exception("\nОШИБКА ВЕРХНЕГО УРОВНЯ: Произошла необработанная ошибка. Перезапускаем скрипт...")
            # Скрипт будет зациклен и перезапустит контекст браузера
            random_sleep(5, 10) # Небольшая пауза перед перезапуском
        finally:
            if context:
                try:
                    context.close()
                    logger.info("Контекст браузера закрыт.")
                except Exception as e:
                    logger.error(f"Ошибка при закрытии контекста браузера: {e}")
            if sent_messages >= messages_count:
                break # Выход из внешнего цикла while, если лимит был достигнут и контекст закрыт.

# --- Точка входа в скрипт ---
if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt as e: logger.exception(f'Остановлено пользователем......')
    except Exception as e: logger.error(f'Ошибка вида: {e}')