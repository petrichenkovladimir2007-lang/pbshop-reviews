#!/usr/bin/env python3
"""
Парсер відгуків з Prom.ua → XML-фід відгуків для Google Merchant Center (v2.3)
Клієнт: pbshop.com.ua (P&BShop)

ОСОБЛИВІСТЬ ЦЬОГО КЛІЄНТА:
Каталог було переімпортовано — старі ID товарів з відгуків більше не існують
у поточному товарному фіді. Матчинг за ID неможливий.
Натомість матчимо за НАЗВОЮ товару:
  1. Назва товару з відгуку (data-reviews-products → name) — РОСІЙСЬКОЮ
  2. Шукаємо цю назву (нормалізовану) в RU-фіді (export_lang=ru) → отримуємо g:id
  3. За цим g:id беремо фінальні дані (title/link/brand/mpn) з UK-фіда (export_lang=uk)
Якщо точного збігу немає — пробуємо fuzzy-збіг (difflib, поріг 0.90) і логуємо
такі випадки окремо для ручної перевірки.

Використання:
    python3 pbshop_reviews_feed.py                       # Повний запуск
    python3 pbshop_reviews_feed.py --debug                # Дебаг-режим (1 сторінка)
    python3 pbshop_reviews_feed.py --pages 5               # Перші 5 сторінок
    python3 pbshop_reviews_feed.py --mode moderation       # Без заповнювачів і дублів
"""

import requests
import time
import re
import json
import hashlib
import logging
import argparse
import sys
import os
import difflib
from datetime import datetime
from bs4 import BeautifulSoup

# ============================================================
# КОНФІГУРАЦІЯ
# ============================================================

CONFIG = {
    "base_url": "https://pbshop.com.ua",
    "testimonials_url": "https://pbshop.com.ua/ua/testimonials",
    "max_pages": None,

    # UK-фід — джерело фінальних даних для XML (title/link/brand/mpn)
    "product_feed_url_uk": "https://pbshop.com.ua/google_merchant_center.xml?hash_tag=5927468c32f7dcc1cae69c05acb51e48&product_ids=&label_ids=&export_lang=uk&group_ids=",
    # RU-фід — джерело для матчингу за назвою (відгуки зберігають назву товару російською)
    "product_feed_url_ru": "https://pbshop.com.ua/google_merchant_center.xml?hash_tag=5927468c32f7dcc1cae69c05acb51e48&product_ids=&label_ids=&export_lang=ru&group_ids=",

    "publisher_name": "pbshop.com.ua",
    "favicon_url": "https://pbshop.com.ua/favicon.ico",
    "aggregator_name": "prom.ua",
    "output_file": "pbshop_reviews_feed.xml",
    "request_delay": 1.0,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "rating_map": {
        "Відмінно": 5,
        "Добре": 4,
        "Нормально": 3,
        "Погано": 2,
        "Дуже погано": 1,
        "Жахливо": 1,
    },

    # Поріг схожості для fuzzy-матчингу назв (0.0-1.0), коли точного збігу нема
    "fuzzy_threshold": 0.90,
}

# ============================================================
# ЛОГУВАННЯ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pbshop_parser")


# ============================================================
# HTTP-СЕСІЯ
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": CONFIG["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
    })
    return session


def fetch_page(session, url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text, resp.url
        except requests.RequestException as e:
            log.warning(f"Спроба {attempt}/{retries} для {url}: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)
    log.error(f"Не вдалося завантажити: {url}")
    return None, None


def fetch_bytes(session, url, retries=3):
    """Завантажити як сирі байти (для XML-фідів з проблемним кодуванням)."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=45)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            log.warning(f"Спроба {attempt}/{retries} для {url}: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)
    log.error(f"Не вдалося завантажити: {url}")
    return None


# ============================================================
# ПАРСИНГ ТОВАРНИХ ФІДІВ (UK + RU)
# ============================================================

def parse_product_feed(session, url, label=""):
    """Повертає словник { prom_id: {id, prom_id, title, link, brand, mpn} }."""
    log.info(f"Завантажую товарний фід ({label})...")
    xml_bytes = fetch_bytes(session, url)
    if not xml_bytes:
        log.error(f"Не вдалося завантажити товарний фід ({label})!")
        return {}

    soup = BeautifulSoup(xml_bytes, "xml")
    products = {}

    items = soup.find_all("item") or soup.find_all("entry") or soup.find_all("product")

    for item in items:
        id_tag    = item.find("g:id")    or item.find("id")
        title_tag = item.find("g:title") or item.find("title")
        link_tag  = item.find("g:link")  or item.find("link")
        brand_tag = item.find("g:brand") or item.find("brand")
        mpn_tag   = item.find("g:mpn")   or item.find("mpn")

        p_id   = id_tag.text.strip()   if id_tag   else ""
        title  = title_tag.text.strip() if title_tag else ""
        link   = link_tag.text.strip()  if link_tag  else ""
        brand  = brand_tag.text.strip() if brand_tag else ""
        mpn    = mpn_tag.text.strip()   if mpn_tag   else p_id

        if not p_id:
            continue

        products[p_id] = {
            "id": p_id,
            "prom_id": p_id,
            "title": title,
            "link": link,
            "brand": brand,
            "mpn": mpn or p_id,
        }

    log.info(f"Завантажено {len(products)} товарів з фіда ({label})")
    return products


def normalize_title(s):
    """Нормалізація назви товару для матчингу: нижній регістр, без пунктуації, без зайвих пробілів."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s, flags=re.UNICODE)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def build_title_index(products_ru):
    """{ normalized_title: [prom_id, ...] } — список, бо назви можуть повторюватись."""
    index = {}
    for prom_id, p in products_ru.items():
        n = normalize_title(p["title"])
        if not n:
            continue
        index.setdefault(n, []).append(prom_id)
    dupes = {t: ids for t, ids in index.items() if len(ids) > 1}
    if dupes:
        log.info(f"Неоднозначних назв у RU-фіді (кілька товарів з однаковою назвою): {len(dupes)}")
    return index


# ============================================================
# ПАРСИНГ ВІДГУКІВ (стара структура b-comments__*)
# ============================================================

def detect_max_pages(soup):
    paginator = soup.select_one('[data-bazooka="Paginator"]')
    if paginator:
        count = paginator.get("data-pagination-pages-count")
        if count:
            return int(count)
    return None


def parse_review_item(item, debug=False):
    review = {
        "author": "",
        "date": "",
        "datetime_iso": "",
        "rating": 5,
        "rating_text": "",
        "text": "",
        "products": [],
        "tags": [],
    }

    author_el = (item.select_one('[data-qaid="author_name"]')
                 or item.select_one("strong.b-comments__author-name"))
    if author_el:
        review["author"] = author_el.get_text(strip=True)

    date_el = (item.select_one('[data-qaid="review_date"]')
               or item.select_one("time.b-comments__date"))
    if date_el:
        review["datetime_iso"] = date_el.get("datetime", "")
        review["date"] = date_el.get_text(strip=True)

    rating_el = (item.select_one("span.cs-rating__state")
                 or item.select_one("span.b-rating__state"))
    if rating_el:
        review["rating_text"] = rating_el.get_text(strip=True)
        title = rating_el.get("title", "")
        m = re.search(r'(\d+)\s+з\s+(\d+)', title)
        if m:
            review["rating"] = int(m.group(1))
        elif review["rating_text"] in CONFIG["rating_map"]:
            review["rating"] = CONFIG["rating_map"][review["rating_text"]]

    text_el = (item.select_one('[data-qaid="review_text"]')
               or item.select_one("p.b-comments__text"))
    if text_el:
        review["text"] = text_el.get_text(strip=True)

    products_el = item.select_one('[data-reviews-products]')
    if products_el:
        try:
            products_json = products_el.get("data-reviews-products", "[]")
            for p in json.loads(products_json):
                review["products"].append({
                    "id": str(p.get("id", "")),
                    "name": p.get("name", ""),
                    "url": p.get("url", ""),
                })
        except (json.JSONDecodeError, TypeError) as e:
            if debug:
                log.warning(f"Помилка парсингу JSON товарів: {e}")

    for tag in item.select("li.b-comments-tags__item"):
        tag_title = tag.get("data-tag-title", "")
        if tag_title:
            review["tags"].append(tag_title)

    if debug:
        log.info(f"  Автор: {review['author']}")
        log.info(f"  Дата:  {review['date']} ({review['datetime_iso']})")
        log.info(f"  Рейт.: {review['rating']} ({review['rating_text']})")
        log.info(f"  Текст: {review['text'][:100] or '(пусто)'}")
        log.info(f"  Товари: {len(review['products'])} шт")
        for p in review["products"][:3]:
            log.info(f"    → [{p['id']}] {p['name']}")
        log.info(f"  Теги: {review['tags']}")
        log.info("")

    return review if review["author"] else None


def parse_reviews_page(html, page_num, debug=False):
    soup = BeautifulSoup(html, "html.parser")
    max_pages = detect_max_pages(soup) if page_num == 1 else None
    items = soup.select("li.b-comments__item") or soup.select("li.cs-comments__item")

    if debug:
        log.info(f"Знайдено {len(items)} блоків відгуків")

    reviews = []
    for i, item in enumerate(items):
        review = parse_review_item(item, debug=(debug and i < 3))
        if review:
            reviews.append(review)

    return reviews, max_pages


# ============================================================
# ЗБІР ВСІХ ВІДГУКІВ
# ============================================================

def collect_all_reviews(session, max_pages_override=None, debug=False):
    all_reviews = []
    max_pages = max_pages_override or CONFIG["max_pages"] or 9999

    for page_num in range(1, max_pages + 1):
        url = (CONFIG["testimonials_url"] if page_num == 1
               else f"{CONFIG['testimonials_url']}/page_{page_num}")

        log.info(f"Сторінка {page_num}: {url}")
        html, final_url = fetch_page(session, url)

        if not html:
            log.warning(f"Пропускаю сторінку {page_num}")
            continue

        if page_num > 1 and final_url and "/page_" not in final_url:
            log.info(f"Редірект на {final_url} — остання сторінка була {page_num - 1}")
            break

        reviews, detected_max = parse_reviews_page(html, page_num, debug=(debug and page_num == 1))

        if detected_max and not max_pages_override and not CONFIG["max_pages"]:
            max_pages = detected_max
            log.info(f"Автодетект: {max_pages} сторінок")

        if not reviews:
            log.info(f"Сторінка {page_num} пуста — зупиняюсь")
            break

        all_reviews.extend(reviews)
        log.info(f"  → {len(reviews)} відгуків (всього: {len(all_reviews)})")

        if debug:
            break

        time.sleep(CONFIG["request_delay"])

    return all_reviews


# ============================================================
# МАТЧИНГ ЗА НАЗВОЮ (RU-фід) → фінальні дані (UK-фід)
# ============================================================

def resolve_prom_id_by_name(name, title_index_ru, fuzzy_threshold):
    """Повертає (prom_id, method) або (None, None). method: 'exact' | 'fuzzy'.
    fuzzy_threshold=None вимикає fuzzy-фолбек повністю (тільки exact-матчинг)."""
    n = normalize_title(name)
    if not n:
        return None, None

    candidates = title_index_ru.get(n)
    if candidates:
        return candidates[0], "exact"

    if fuzzy_threshold is None:
        return None, None

    # Fuzzy fallback — РИЗИКОВАНО: схожі назви часто виявляються РІЗНИМИ товарами
    # (інший розмір/кількість/роз'єм тощо). НЕ використовувати для продакшн-фіда без ручної перевірки.
    close = difflib.get_close_matches(n, title_index_ru.keys(), n=1, cutoff=fuzzy_threshold)
    if close:
        return title_index_ru[close[0]][0], "fuzzy"

    return None, None


def match_and_expand_reviews(reviews, products_uk, title_index_ru, fuzzy_threshold):
    matched_pairs = []
    fuzzy_matches = []
    no_products = 0
    no_match = 0

    for review in reviews:
        if not review["products"]:
            no_products += 1
            continue

        found = False
        for p in review["products"]:
            prom_id, method = resolve_prom_id_by_name(p["name"], title_index_ru, fuzzy_threshold)
            if prom_id and prom_id in products_uk:
                matched_pairs.append((review, products_uk[prom_id]))
                found = True
                if method == "fuzzy":
                    fuzzy_matches.append((p["name"], products_uk[prom_id]["title"], prom_id))
            elif prom_id and prom_id not in products_uk:
                # Знайшли в RU, але цього ID нема в UK-фіді (розсинхрон фідів) — пропускаємо
                pass

        if not found:
            no_match += 1

    log.info(f"Матчинг: {len(matched_pairs)} пар | без товарів: {no_products} | не знайдено: {no_match}")
    if fuzzy_threshold is None and no_match:
        log.info(f"(fuzzy вимкнено — {no_match} відгуків без exact-збігу пропущено; --enable-fuzzy покаже кандидатів)")
    if fuzzy_matches:
        log.info(f"З них fuzzy-збігів (поріг {fuzzy_threshold}): {len(fuzzy_matches)} — ПЕРЕВІР ВРУЧНУ, "
                 f"це прив'язка за схожістю рядка, а не гарантований той самий товар:")
        for review_name, feed_title, pid in fuzzy_matches[:15]:
            log.info(f"    відгук: '{review_name}'")
            log.info(f"    фід:    '{feed_title}' [{pid}]")

    return matched_pairs


# ============================================================
# ФІЛЬТРАЦІЯ ДЛЯ МОДЕРАЦІЇ
# ============================================================

def is_placeholder(review):
    text = (review.get("text") or "").strip()
    if not text:
        return True
    if len(text) < 15:
        return True
    return False


def filter_for_moderation(matched_pairs):
    filtered = [(r, p) for r, p in matched_pairs if not is_placeholder(r)]
    removed_placeholders = len(matched_pairs) - len(filtered)
    log.info(f"Moderation: прибрано заповнювачів: {removed_placeholders}")

    seen_pairs = set()
    deduped = []
    removed_dupes = 0
    for r, p in filtered:
        key = (r["author"], p["prom_id"])
        if key in seen_pairs:
            removed_dupes += 1
            continue
        seen_pairs.add(key)
        deduped.append((r, p))

    log.info(f"Moderation: прибрано дублів автор+товар: {removed_dupes}")
    log.info(f"Moderation: залишилось пар: {len(deduped)}")
    return deduped


# ============================================================
# XML
# ============================================================

def escape_xml(text):
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def generate_review_id(review, product):
    raw = f"{review['author']}_{review['date']}_{product.get('prom_id', '')}_{review['text'][:30]}"
    return "RV" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6].upper()


def format_timestamp(review):
    iso = review.get("datetime_iso", "")
    if iso:
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
        except ValueError:
            pass
    date_str = review.get("date", "")
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%dT10:00:00+02:00")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%dT10:00:00+02:00")


def build_content(review):
    parts = []
    if review["text"]:
        parts.append(review["text"].rstrip(". "))
    if review["tags"]:
        parts.append(". ".join(review["tags"]))
    if not parts:
        parts.append(review.get("rating_text") or "Відмінно")
    return ". ".join(parts)


def generate_xml_feed(matched_pairs):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns:vc="http://www.w3.org/2007/XMLSchema-versioning" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation='
        '"http://www.google.com/shopping/reviews/schema/product/2.3/product_reviews.xsd">',
        "    <version>2.3</version>",
        "    <aggregator>",
        f"        <n>{escape_xml(CONFIG['aggregator_name'])}</n>",
        "    </aggregator>",
        "    <publisher>",
        f"        <n>{escape_xml(CONFIG['publisher_name'])}</n>",
        f"        <favicon>{escape_xml(CONFIG['favicon_url'])}</favicon>",
        "    </publisher>",
        "    <reviews>",
    ]

    seen_ids = set()

    for review, product in matched_pairs:
        review_id = generate_review_id(review, product)
        if review_id in seen_ids:
            continue
        seen_ids.add(review_id)

        timestamp = format_timestamp(review)
        content = build_content(review)
        rating = review["rating"]

        mpn = product.get("mpn") or product.get("prom_id") or product.get("id", "")
        brand = product.get("brand") or "Без бренду"
        product_name = product.get("title", "")
        product_url = product.get("link", "")

        if product_url and "source=merchant_center" not in product_url:
            sep = "&" if "?" in product_url else "?"
            product_url += f"{sep}source=merchant_center"

        line = (
            f"<review>"
            f"<review_id>{escape_xml(review_id)}</review_id>"
            f"<reviewer><n>{escape_xml(review['author'])}</n></reviewer>"
            f"<review_timestamp>{timestamp}</review_timestamp>"
            f"<content>{escape_xml(content)}</content>"
            f"<review_url type='group'>{escape_xml(CONFIG['testimonials_url'])}</review_url>"
            f"<ratings><overall min='1' max='5'>{rating}</overall></ratings>"
            f"<products><product><product_ids>"
            f"<mpns><mpn>{escape_xml(mpn)}</mpn></mpns>"
            f"<brands><brand>{escape_xml(brand)}</brand></brands>"
        )

        line += (
            f"</product_ids>"
            f"<product_name>{escape_xml(product_name)}</product_name>"
            f"<product_url>{escape_xml(product_url)}</product_url>"
            f"</product></products></review>"
        )

        lines.append(f"\t{line}")

    lines.append("    </reviews>")
    lines.append("</feed>")

    log.info(f"XML: {len(seen_ids)} унікальних відгуків у фіді")
    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="pbshop.com.ua Reviews → GMC XML Feed (матчинг за назвою)")
    parser.add_argument("--debug", action="store_true", help="Дебаг-режим (1 сторінка)")
    parser.add_argument("--pages", type=int, default=None, help="Кількість сторінок")
    parser.add_argument("--mode", choices=["full", "moderation"], default="full",
                        help="full = всі відгуки | moderation = без заповнювачів і дублів")
    parser.add_argument("--output", type=str, default=None, help="Вихідний файл")
    parser.add_argument("--enable-fuzzy", action="store_true",
                        help="Увімкнути fuzzy-матчинг назв (ВИМКНЕНО за замовч. — часто плутає товари з різними "
                             "характеристиками; використовуй тільки для ручної діагностики, не для продакшн-фіда)")
    parser.add_argument("--fuzzy-threshold", type=float, default=None,
                        help=f"Поріг fuzzy-матчингу назв (0.0-1.0) при --enable-fuzzy, за замовч. {CONFIG['fuzzy_threshold']}")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    output_file = args.output or CONFIG["output_file"]
    max_pages = 1 if args.debug else args.pages
    mode = args.mode
    if args.enable_fuzzy:
        fuzzy_threshold = args.fuzzy_threshold if args.fuzzy_threshold is not None else CONFIG["fuzzy_threshold"]
    else:
        fuzzy_threshold = None

    log.info("=" * 60)
    log.info("pbshop.com.ua Reviews → GMC XML Feed (матчинг за назвою)")
    log.info(f"Магазин: {CONFIG['publisher_name']}")
    log.info(f"Режим: {mode.upper()}")
    log.info("=" * 60)

    session = create_session()

    products_uk = parse_product_feed(session, CONFIG["product_feed_url_uk"], label="UK")
    if not products_uk:
        log.error("UK товарний фід пустий — перевір URL!")
        sys.exit(1)

    products_ru = parse_product_feed(session, CONFIG["product_feed_url_ru"], label="RU")
    if not products_ru:
        log.error("RU товарний фід пустий — перевір URL!")
        sys.exit(1)

    title_index_ru = build_title_index(products_ru)

    reviews = collect_all_reviews(session, max_pages_override=max_pages, debug=args.debug)
    log.info(f"Всього зібрано: {len(reviews)} відгуків")

    if not reviews:
        log.error("Жодного відгуку не знайдено!")
        sys.exit(1)

    matched_pairs = match_and_expand_reviews(reviews, products_uk, title_index_ru, fuzzy_threshold)

    if not matched_pairs:
        log.error("Жодного відгуку не зматчилось з товарами!")
        if reviews and reviews[0]["products"]:
            sample_names = [p["name"] for p in reviews[0]["products"][:3]]
            log.error(f"  Приклад назв з відгуків: {sample_names}")
        sys.exit(1)

    if mode == "moderation":
        matched_pairs = filter_for_moderation(matched_pairs)

    xml_content = generate_xml_feed(matched_pairs)

    os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(xml_content)

    file_size = os.path.getsize(output_file)
    log.info(f"Фід збережено: {output_file} ({file_size / 1024:.1f} KB)")
    log.info("Готово!")


if __name__ == "__main__":
    main()
