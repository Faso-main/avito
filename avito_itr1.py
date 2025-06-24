import time
import re
import random
import os
import sys
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError

# ---------------------------------------
# Настройки имён файлов (можете изменить)
# ---------------------------------------
MAIN_FILE = "ads_links.txt"       # Основной файл со ссылками на объявления
SELLER_IDS_FILE = "seller_ids.txt"  # Файл c ID продавцов
CURRENT_LINK_FILE = "current_link.txt"  # Файл с текущей (последней обработанной) ссылкой

# --------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ------------------------
def random_sleep(min_sec=1.0, max_sec=2.0):
    """Случайная пауза между действиями."""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)

def parse_synonyms(synonyms_text):
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
        if not line:
            continue
        if ":" not in line:
            continue
        word_part, syn_part = line.split(":", 1)
        base_word = word_part.strip().lower()
        synonyms = [s.strip() for s in syn_part.split(",") if s.strip()]
        if synonyms:
            result[base_word] = synonyms
    return result

def unique_message(original_text, synonyms_dict):
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

def get_seller_id_from_page(page):
    """Пытаемся извлечь имя/ID продавца по нескольким вариантам селекторов."""
    try:
        # 1) .style-nameWrapper-vmkRf span
        wrapper_el = page.query_selector('.style-nameWrapper-vmkRf span')
        if wrapper_el:
            text = wrapper_el.text_content().strip()
            if text:
                return text

        # 2) span.styles-module-size_ms-YUHT8
        alt_el = page.query_selector('span.styles-module-size_ms-YUHT8')
        if alt_el:
            alt_text = alt_el.text_content().strip()
            if alt_text:
                return alt_text

        return None
    except Exception as e:
        print(f"Ошибка при извлечении имени продавца: {e}")
        return None

def collect_ads_links(page):
    """Собираем ссылки на объявления, учитывая разные варианты верстки."""
    links = []
    # Первый тип
    elems1 = page.query_selector_all('.styles-item-m0DD4 a[href]')
    for el in elems1:
        href = el.get_attribute("href")
        if href:
            links.append(href)

    # Второй тип
    container = page.query_selector('.index-content-c0K1j')
    if container:
        elems2 = container.query_selector_all('.iva-item-title-CdRXl a[href]')
        for el in elems2:
            href = el.get_attribute("href")
            if href:
                links.append(href)

    return links

def infinite_scroll_with_next(page, existing_links, max_links=200):
    """
    «Бесконечная» прокрутка и переход на следующие страницы,
    чтобы собрать не более max_links *новых* ссылок.
    Возвращает список новых ссылок (без дублей).
    existing_links — множество уже имеющихся ссылок, чтобы исключать дубли.
    """
    global start_url  # Добавлено для обновления внешней переменной
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
                        print(f"Достигнут лимит {max_links} ссылок. Останавливаем сбор.")
                        start_url = page.url  # Обновляем перед выходом
                        return new_collected

            if found_now == 0:
                break

        # 2) Переход на следующую страницу
        if total_new == old_count:
            next_link_el = page.query_selector('[data-marker="pagination-button/nextPage"]')
            if next_link_el:
                next_href = next_link_el.get_attribute("href")
                if next_href:
                    if next_href.startswith("/"):
                        next_href = "https://www.avito.ru" + next_href
                    print(f"Переходим на следующую страницу: {next_href}")
                    try:
                        page.goto(next_href, timeout=60000, wait_until="domcontentloaded")
                        random_sleep(3, 5)
                        start_url = page.url  # Обновляем после перехода
                    except TimeoutError:
                        print("Не удалось загрузить следующую страницу. Прерываем сбор.")
                        start_url = page.url  # Обновляем URL даже при ошибке
                        return new_collected
                else:
                    print("Кнопка Next без href. Останавливаемся.")
                    start_url = page.url  # Обновляем перед выходом
                    return new_collected
            else:
                print("Нет кнопки «Следующая страница». Останавливаемся.")
                start_url = page.url  # Обновляем перед выходом
                return new_collected

    start_url = page.url  # Финальное обновление (на случай выхода из цикла)
    return new_collected

def click_avito_logo(page):
    """Клик по логотипу Авито, чтобы «обновить» или вернуться на главную."""
    logo_el = page.query_selector('div.index-logo-K90gi')
    if logo_el:
        logo_el.click()
        random_sleep(3, 6)
    else:
        print("Не удалось найти элемент с логотипом Авито для клика.")

def wait_for_messenger_element(page, timeout_minutes=10):
    """
    Ждём появления элемента Личные сообщения (селектор data-marker='header/messenger')
    максимум timeout_minutes минут.
    Возвращает True, если элемент найден, иначе False.
    """
    total_seconds = timeout_minutes * 60
    interval = 2
    waited = 0
    while waited < total_seconds:
        el = page.query_selector('[data-marker="header/messenger"]')
        if el:
            return True
        time.sleep(interval)
        waited += interval
    return False


# --------------------- ОСНОВНАЯ ФУНКЦИЯ ------------------------
def main():
    # === Шаг 0. Спросим про очистку файлов ===
    choice = input("ОЧИСТИТЬ все 3 файла (основной, файл ID, текущая ссылка)?\n"
                   "(Нажмите Enter — очистить, введите любое другое значение — нет): ").strip()
    if choice == "":
        # Очищаем
        for fpath in [MAIN_FILE, SELLER_IDS_FILE, CURRENT_LINK_FILE]:
            if os.path.exists(fpath):
                os.remove(fpath)
        print("Все три файла очищены.\n")
    else:
        print("Продолжаем без очистки файлов.\n")

    # === Подгрузим/создадим основной файл со ссылками ===
    if not os.path.exists(MAIN_FILE):
        open(MAIN_FILE, 'w', encoding='utf-8').close()
    with open(MAIN_FILE, 'r', encoding='utf-8') as f:
        main_links = [line.strip() for line in f if line.strip()]

    # === Файл ID продавцов ===
    if not os.path.exists(SELLER_IDS_FILE):
        open(SELLER_IDS_FILE, 'w', encoding='utf-8').close()
    with open(SELLER_IDS_FILE, 'r', encoding='utf-8') as f:
        processed_sellers = set(line.strip() for line in f if line.strip())

    # === Файл текущей ссылки ===
    current_link = None
    if os.path.exists(CURRENT_LINK_FILE):
        with open(CURRENT_LINK_FILE, 'r', encoding='utf-8') as f:
            c = f.read().strip()
            current_link = c if c else None

    # === Запрос у пользователя: стартовая страница и макс. кол-во сбора ссылок ===
    start_url = input("Введите URL для сбора ссылок (стартовая страница).\n"
                      "Если оставить пустым, будет использоваться https://www.avito.ru/\n"
                      "> ").strip()
    if not start_url:
        start_url = "https://www.avito.ru/"

    max_links_str = input("Введите максимальное число ссылок для сбора за один раз [по умолчанию 200]: ").strip()
    try:
        max_links = int(max_links_str)
    except:
        max_links = 200

    # === Прочие параметры ===
    print("\nПожалуйста, введите параметры для рассылки:\n")
    messages_count_str = input("Сколько всего сообщений нужно отправить? [по умолчанию 10]: ") or "10"
    min_views_str = input("Минимальное число просмотров? [0 = не проверять]: ") or "0"
    synonyms_text = input("Введите словарь синонимов (формат 'слово: синоним1, синоним2; ...'): ") or ""
    original_message = input("Введите исходный текст сообщения (можно использовать \\n для переноса строк): ")

    try:
        messages_count = int(messages_count_str)
    except ValueError:
        messages_count = 10

    try:
        min_views = int(min_views_str)
    except ValueError:
        min_views = 0

    synonyms_dict = parse_synonyms(synonyms_text)

    # Счётчик отправленных сообщений
    sent_messages = 0

    # Папка профиля (для сохранения авторизации)
    user_data_dir = "avito_user_data"

    # Начинаем работу в браузере
    while True:
        try:
            with sync_playwright() as p:
                context = p.firefox.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False
                )
                if context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()

                # Переходим на стартовую страницу (или восстанавливаемся)
                try:
                    page.goto(start_url, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"Не удалось загрузить {start_url}: {e}")
                    page.goto("https://www.avito.ru/", wait_until="domcontentloaded")

                print("\nОткрылся браузер. Авторизуйтесь (если нужно).")
                print("Скрипт будет ждать появления элемента Личные сообщения (data-marker=\"header/messenger\") "
                      "до 10 минут.\n")

                # Ждём появления элемента сообщений
                found = wait_for_messenger_element(page, timeout_minutes=10)
                if not found:
                    print("Элемент 'Сообщения' не найден за 10 минут. Прекращаем работу.")
                    context.close()
                    return

                # Если найден, ждём рандом 5–7 сек, затем начинаем
                random_sleep(5, 7)
                print("Элемент сообщений найден. Начинаем основную логику.\n")

                # -----------------------------------------
                # Главный цикл рассылки (пока не дошли до messages_count)
                # -----------------------------------------
                while sent_messages < messages_count:
                    # 1) Проверяем, надо ли собирать новые ссылки
                    # если основной файл пуст или
                    # если current_link совпадает с последней ссылкой в main_links
                    try: 
                        with open(CURRENT_LINK_FILE, 'r', encoding='utf-8') as cf:
                            current_link = cf.read().strip()  # Убираем лишние пробелы/переносы строк                    
                    except FileNotFoundError:
                            current_link = None  # Если файла нет, переменная остаётся пустой

                    need_collect = False
                    if len(main_links) == 0:
                        need_collect = True
                    else:
                        last_in_main = main_links[-1]
                        if current_link and (current_link == last_in_main):
                            need_collect = True

                    if need_collect:
                        print("\n--- Сбор новых ссылок, так как достигнут конец основного файла (или он пуст). ---")
                        # Переходим на start_url (на всякий случай)
                        try:
                            page.goto(start_url, timeout=60000, wait_until="domcontentloaded")
                            random_sleep(10, 20)
                        except:
                            print("Не удалось загрузить стартовую страницу для сбора ссылок. Пропускаем.")
                            break

                        # Собираем уже имеющиеся ссылки в множество (для уникальности)
                        existing_links = set(main_links)
                        # Запускаем «бесконечную» прокрутку
                        new_links = infinite_scroll_with_next(page, existing_links, max_links=max_links)
                        if new_links:
                            random_sleep(10, 20)
                            # Дописываем их в основной файл
                            with open(MAIN_FILE, 'a', encoding='utf-8') as f:
                                for link in new_links:
                                    f.write(link + "\n")
                            # И в наш список (чтобы в этой сессии скрипт знал об этих ссылках)
                            main_links.extend(new_links)
                            print(f"Добавлено {len(new_links)} новых ссылок в основной файл.")
                        else:
                            print("Новых ссылок не найдено. Возможно, ничего больше нет.")
                            # Если даже новых нет, прерываемся
                            if not main_links:
                                # Если в итоге всё равно пусто, выходим
                                print("Основной файл остался пустым. Останавливаем скрипт.")
                                break

                    # 2) Перебираем ссылки из main_links, начиная с той, что идёт после current_link
                    start_index = 0
                    if current_link in main_links:
                        idx = main_links.index(current_link)
                        start_index = idx + 1

                    # Если current_link нет или не нашли, то start_index = 0 (с начала)
                    for i in range(start_index, len(main_links)):
                        ad_url = main_links[i]
                        if sent_messages >= messages_count:
                            break

                        # Запишем в файл текущую ссылку
                        with open(CURRENT_LINK_FILE, 'w', encoding='utf-8') as cf:
                            cf.write(ad_url)

                        # Пытаемся открыть объявление
                        print(f"\n>>> Переход к объявлению: {ad_url}")
                        loaded_ok = False
                        for attempt_ad in range(3):
                            try:
                                page.goto(ad_url, timeout=60000, wait_until="domcontentloaded")
                                random_sleep(3, 5)
                                loaded_ok = True
                                break
                            except TimeoutError:
                                print(f"Не удалось загрузить объявление (попытка {attempt_ad+1}/3). Обновляем.")
                                click_avito_logo(page)
                                random_sleep(3, 5)
                        if not loaded_ok:
                            print("Пропускаем это объявление из-за ошибок загрузки.")
                            continue

                        # Извлекаем ID продавца
                        seller_id = None
                        for attempt_seller in range(3):
                            # Пытаемся найти элемент с именем продавца
                            seller_element = None
                            try:
                                seller_element = page.wait_for_selector('.style-sticky-header-seller-text-mVIXS', timeout=5000)
                            except:
                                pass
                            
                            if seller_element:
                                seller_text = seller_element.text_content().strip()
                                # Если имя скрыто троеточием
                                if seller_text == '...':
                                    # На последней попытке берём из title
                                    if attempt_seller == 2:
                                        seller_id = seller_element.get_attribute('title').strip()
                                        break
                                    # Прокручиваем и пробуем снова
                                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                                    random_sleep(3, 5)
                                    continue
                                
                                # Если имя нормальное
                                seller_id = seller_text
                                break
                            else:
                                # Если элемент не найден - прокручиваем
                                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                                random_sleep(3, 5)
                        
                        if not seller_id:
                            print("Продавец не найден. Пропускаем объявление.")
                            continue

                        # Проверяем, не отправляли ли уже этому продавцу
                        if seller_id in processed_sellers:
                            print(f"Продавцу «{seller_id}» уже отправляли сообщение. Пропускаем.")
                            continue

                        # Проверяем просмотры (если задан min_views)
                        if min_views > 0:
                            views_number = None
                            for attempt_view in range(3):
                                try:
                                    views_el = page.wait_for_selector('[data-marker="item-view/total-views"]', timeout=5000)
                                    random_sleep(5, 7)
                                    views_text = views_el.inner_text()
                                    match = re.search(r'(\d+)', views_text)
                                    views_number = int(match.group(1)) if match else 0
                                    break
                                except:
                                    print(f"Просмотры не найдены (попытка {attempt_view+1}). Обновляем.")
                                    click_avito_logo(page)
                                    try:
                                        page.goto(ad_url, timeout=60000, wait_until="domcontentloaded")
                                        random_sleep(2,4)
                                    except:
                                        pass
                            if views_number is None:
                                print("Не удалось получить число просмотров, пропускаем.")
                                continue
                            if views_number < min_views:
                                print(f"Просмотров ({views_number}) меньше, чем {min_views}. Пропускаем.")
                                continue
                            print(f"Просмотры: {views_number}")

                        # Ищем кнопку «Написать»
                        write_btn = None
                        for attempt_btn in range(3):
                            # Прокрутим вниз, вдруг кнопка не видна
                            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                            random_sleep(5, 7)
                            write_btn = page.query_selector('[data-marker="messenger-button/button"]')
                            if write_btn:
                                break
                            else:
                                print(f"Кнопка 'Написать' не найдена (попытка {attempt_btn+1}). Обновляем.")
                                click_avito_logo(page)
                                try:
                                    page.goto(ad_url, timeout=60000, wait_until="domcontentloaded")
                                    random_sleep(5, 7)
                                except:
                                    pass
                        if not write_btn:
                            print("Кнопка 'Написать' не найдена. Пропускаем объявление.")
                            continue

                        write_btn.click()
                        random_sleep(3, 5)


                        # Переход к форме отправки сообщений
                        # (Дождёмся элемента, который переключает в мини-мессенджер)
                        try:
                            page.wait_for_selector('[data-marker="mini-messenger/messenger-page-link"]', timeout=9000)
                            button = page.query_selector('[data-marker="mini-messenger/messenger-page-link"]')
                            if button:
                                button.click()
                                random_sleep(5, 7)
                        except:
                            print("Элемент перехода в мини-мессенджер не найден. Пробуем обновить страницу.")
                            click_avito_logo(page)
                            try:
                                page.goto(ad_url, timeout=60000, wait_until='domcontentloaded')
                                random_sleep(2, 4)
                                wb2 = page.query_selector('[data-marker="messenger-button/button"]')
                                if wb2:
                                    wb2.click()
                                    random_sleep(2, 3)
                                page.wait_for_selector('[data-marker="mini-messenger/messenger-page-link"]', timeout=5000)
                                button = page.query_selector('[data-marker="mini-messenger/messenger-page-link"]')
                                if button:
                                    button.click()
                                    random_sleep(2, 3)
                            except:
                                print("Так и не смогли найти переход в мессенджер. Пропускаем объявление.")
                                continue


                        # Теперь ищем textarea
                        text_area = None
                        for attempt_ta in range(3):
                            try:
                                text_area = page.wait_for_selector('textarea[data-marker="reply/input"]', timeout=9000)
                                if text_area:
                                    break
                            except:
                                pass
                            print(f"Поле ввода не найдено (попытка {attempt_ta+1}). Обновляем.")
                            click_avito_logo(page)
                            try:
                                page.goto(ad_url, timeout=60000, wait_until='domcontentloaded')
                                random_sleep(5, 7)
                                wbtn = page.query_selector('[data-marker="messenger-button/button"]')
                                if wbtn:
                                    wbtn.click()
                                    random_sleep(5, 7)
                            except:
                                pass
                        if not text_area:
                            print("Нет поля ввода сообщения. Пропускаем объявление.")
                            continue

                        # Формируем итоговое сообщение (с заменой синонимов и переводом \n)
                        final_text = unique_message(original_message, synonyms_dict).replace('\\n', '\n')

                        text_area.fill(final_text)
                        random_sleep(1, 2)

                        # Отправляем
                        try:
                            send_btn = page.wait_for_selector('[data-marker="reply/send"]', timeout=5000)
                            random_sleep(5, 7)
                            send_btn.click()
                            random_sleep(2, 3)
                            sent_messages += 1
                            processed_sellers.add(seller_id)
                            # Запишем seller_id в файл
                            with open(SELLER_IDS_FILE, 'a', encoding='utf-8') as sf:
                                sf.write(seller_id + "\n")
                            print(f"Сообщение отправлено продавцу «{seller_id}». (всего отправлено {sent_messages})")
                        except:
                            print("Кнопка 'Отправить' не найдена. Пропускаем объявление.")

                        # Если достигли лимит — выходим
                        if sent_messages >= messages_count:
                            break

                    # Проверяем лимит
                    if sent_messages >= messages_count:
                        print(f"\nЛимит в {messages_count} сообщений достигнут.")
                        break

                    print("Задержка 5-15 минут")
                    random_sleep(300, 900)
                    
                print(f"\nГотово! Всего отправлено {sent_messages} (из {messages_count}).")
                context.close()
                break  # Выходим из внешнего цикла

        except Exception as e:
            print(f"\nОШИБКА верхнего уровня: {e}")
            print("Перезапускаем скрипт... (данные processed_sellers и main_links в памяти сохраняются)\n")
            # Если хотите, можно сделать sys.exit(1) или return, 
            # но здесь оставлен перезапуск по требованию:
            continue

# --------------------- ЗАПУСК ------------------------
if __name__ == "__main__":
    main()
