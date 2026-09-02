# -*- coding: utf-8 -*-
"""
find_phone.py: телефон компании через веб-поиск (Exa), когда сайта нет.

Зачем этот скрипт. У части маленьких компаний (2-5 человек) вообще нет
своего сайта — ни угадать домен, ни найти его поиском нельзя, потому что
сайта не существует. Но данные о таких компаниях всё равно попадают на
агрегаторы (rusprofile.ru, companies.rbc.ru, companium.ru, checko.ru
и т.д.) — их индексируют поисковики, и обычный веб-поиск по названию
находит эти карточки, а на карточке иногда есть телефон.

Проверено вручную 29.08.2026 на выборке из 5 компаний без сайта:
2 из 5 дали точное совпадение по ИНН на агрегаторе (телефон нашёлся),
у 1 из 5 подтверждено, что контактов нет вообще нигде (Rusprofile прямо
пишет «отсутствуют в ЕГРЮЛ»), у 2 из 5 поиск зацепил ТЁЗОК из других
городов с другим ИНН. Отсюда и жёсткое правило ниже.

Правило верификации то же, что и у сайтов в enrich_site.py: телефон
принимается ТОЛЬКО если в тексте результата поиска нашёлся ТОТ ЖЕ ИНН,
что и у компании в реестре. Без этого — no_match, факт не сохраняется.
Тёзки — не редкость: на той же тестовой выборке «Алекон» из Магнитогорска
чуть не подменился «Алеконом» из Ростова-на-Дону, Москвы и Тулы.

Экономика. Каждый вызов --live тратит платный запрос к Exa Search API
(config.exa.api_key). Без --live скрипт только считает, скольким записям
в базе не хватает телефона, и в сеть не ходит.

Python 3.9, только стандартная библиотека (urllib вместо requests).
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

EXA_SEARCH_URL = "https://api.exa.ai/search"
UA = "LeadRadar/1.0 (find_phone; +https://topn8n.ru)"
TIMEOUT = 20
POLITE_DELAY = 0.6          # секунда между запросами к Exa, платный API, спешить некуда

# Разделители после кода города ОБЯЗАТЕЛЬНЫ ([\s\-()]+, не *). Живая проверка
# 29.08.2026: на rusprofile.ru телефон компании спрятан за платным доступом,
# но на странице остаётся 11-значный территориальный код (похоже на ОКТМО),
# который выглядит как один сплошной прогон цифр без единого пробела —
# +70401375000. Настоящий показанный номер на этих сайтах всегда содержит
# пробел/дефис/скобку сразу после кода города: «+7 343 286-20-24». Без
# этого требования скрипт один раз чуть не записал территориальный код
# как телефон компании, у которой реальных контактов нет нигде вообще.
RX_PHONE = re.compile(r'(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]+\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)')
RX_INN = re.compile(r'ИНН\D{0,10}(\d{12}|\d{10})(?!\d)')


class ExaError(RuntimeError):
    """Exa не ответила или ответила ошибкой. Не путать с «ничего не нашли»."""


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


def find_phones_in_text(text):
    found = []
    for m in RX_PHONE.finditer(text or ""):
        start, end = m.span()
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if before.isdigit() or after.isdigit():
            continue
        phone = normalize_phone(m.group(0))
        if phone and phone not in found:
            found.append(phone)
    return found


def find_inns_in_text(text):
    return list(dict.fromkeys(RX_INN.findall(text or "")))


def exa_search(query, api_key, num_results=5, timeout=TIMEOUT):
    """Один запрос к Exa Search API. Возвращает список результатов с текстом.

    contents.text просят сразу в запросе поиска — так не нужен отдельный
    вызов на выкачивание страницы, экономит и деньги, и время.
    """
    body = json.dumps({
        "query": query,
        "numResults": num_results,
        "type": "auto",
        "contents": {"text": {"maxCharacters": 3000}},
    }).encode("utf-8")
    req = urllib.request.Request(
        EXA_SEARCH_URL, data=body, method="POST",
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ExaError("Exa вернула HTTP %s: %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise ExaError("Exa недоступна: %s" % getattr(e, "reason", e))
    except (ValueError, TimeoutError) as e:
        raise ExaError("Exa: не разобрать ответ (%s)" % e)
    return payload.get("results") or []


def find_phone_for_company(name, inn, city=None, api_key=None, verbose=False):
    """Ищет телефон компании через Exa, принимает только с подтверждённым ИНН.

    Возвращает словарь:
      phone       найденный телефон или None
      source_url  страница, где нашли совпадение, или None
      matched_inn ИНН, который реально сверили (для отчёта)
      status      "found" | "no_match" | "error"
      error       текст ошибки Exa, если status == "error"
    """
    target_inn = re.sub(r"\D", "", inn or "")
    out = {"phone": None, "source_url": None, "matched_inn": None,
           "status": "no_match", "error": None}
    if not target_inn:
        out["status"] = "error"
        out["error"] = "у компании нет ИНН, сверять не с чем"
        return out

    query_parts = [p for p in (name, city, "ИНН " + target_inn) if p]
    query = " ".join(query_parts)

    try:
        results = exa_search(query, api_key)
    except ExaError as e:
        out["status"] = "error"
        out["error"] = str(e)
        if verbose:
            print("    Exa: %s" % e)
        return out

    for r in results:
        text = (r.get("text") or "") + " " + (r.get("title") or "")
        if target_inn not in find_inns_in_text(text):
            continue
        phones = find_phones_in_text(text)
        if phones:
            out["phone"] = phones[0]
            out["source_url"] = r.get("url")
            out["matched_inn"] = target_inn
            out["status"] = "found"
            return out
    return out


# ----------------------------------------------------------------------------
# Демо — без сети, показывает, что регулярки живы
# ----------------------------------------------------------------------------

SAMPLE_TEXT = (
    u"ООО «ЮРАКТИВ» – Екатеринбург – Гендиректор Волков А. В. – ИНН 6658494952 "
    u"Телефон +7 343 286-20-24 (+73432862024) 620000, Свердловская область"
)


def _demo():
    print("=" * 72)
    print("find_phone.py: демо")
    print("=" * 72)
    print("\n[1] Разбор образца текста результата поиска")
    inns = find_inns_in_text(SAMPLE_TEXT)
    phones = find_phones_in_text(SAMPLE_TEXT)
    print("    ИНН в тексте:    ", inns)
    print("    телефоны в тексте:", phones)
    ok = inns == ["6658494952"] and phones and phones[0] == "+73432862024"
    print("    итог самопроверки:", "регулярки живы" if ok else "ЕСТЬ РАСХОЖДЕНИЕ")

    if "--offline" in sys.argv:
        print("\n[2] Живой запрос к Exa пропущен, задан ключ --offline")
        return
    print("\n[2] Для живого запроса используйте --live через CLI-режим "
          "(--db/--inn/--inn-file), см. --help")


# ----------------------------------------------------------------------------
# Пакетный режим
# ----------------------------------------------------------------------------

def _cli(argv):
    import argparse
    import verify

    parser = argparse.ArgumentParser(
        description="Поиск телефона компании через Exa, когда своего сайта нет",
        epilog="Без --live в сеть не ходит и денег не тратит. Пример: "
               "find_phone.py --config config.json --db data/leads.db "
               "--only-missing --limit 30 --live")
    parser.add_argument("--config", help="конфиг ниши, по умолчанию config.json")
    parser.add_argument("--db", help="файл sqlite, по умолчанию data/leads.db")
    parser.add_argument("--inn", help="один ИНН")
    parser.add_argument("--inn-file", dest="inn_file",
                        help="файл со списком ИНН, по одному в строке")
    parser.add_argument("--limit", type=int, default=30,
                        help="сколько компаний обработать за прогон, по умолчанию 30")
    parser.add_argument("--only-missing", action="store_true",
                        help="только те, у кого и сайта, и телефона ещё нет")
    parser.add_argument("--live", action="store_true",
                        help="разрешить обращение к Exa Search API (платно)")
    parser.add_argument("--offline", action="store_true",
                        help="без сети: показать, сколько компаний ждёт телефон")
    args = parser.parse_args(argv)

    cfg = verify.load_config(args.config)
    profile = verify.config_profile(cfg)
    api_key = ((cfg.get("exa") or {}).get("api_key") or "").strip()

    db_path = verify.resolve_db_path(args.db, profile)
    if not os.path.exists(db_path):
        print("базы %s нет. Сначала соберите выборку через fns_rmsp.py" % db_path)
        return 1

    con = verify.open_db(db_path)
    verify.ensure_column(con, "enrichment", "phone")
    verify.ensure_column(con, "enrichment", "phone_source")
    verify.ensure_column(con, "enrichment", "phone_checked_at")

    inns = []
    if args.inn:
        inns.append(verify.norm_digits(args.inn))
    if args.inn_file:
        inns.extend(verify.read_inn_file(args.inn_file))
    companies = verify.db_companies(con, inns=inns or None, limit=None)

    if args.only_missing:
        existing_phones = {}
        for r in con.execute("SELECT inn, website, phone FROM enrichment"):
            existing_phones[r["inn"]] = (r["website"], r["phone"])
        companies = [
            c for c in companies
            if not (existing_phones.get(verify.norm_digits(c.get("inn")))
                    and (existing_phones[verify.norm_digits(c.get("inn"))][0]
                         or existing_phones[verify.norm_digits(c.get("inn"))][1]))
        ]
    companies = companies[:args.limit]

    if not companies:
        print("нечего искать: подходящих записей в базе не нашлось")
        con.close()
        return 0

    if not args.live:
        print("Поиск телефона. Параметры прогона:")
        print("      конфиг     : %s" % (cfg.get("_path") or "не найден"))
        print("      база       : %s" % db_path)
        print("      компаний   : %d" % len(companies))
        print()
        print("режим не выбран. Добавьте --live, чтобы сходить в Exa (платно), "
              "или --offline, чтобы просто посчитать сколько компаний ждёт телефон")
        return 0

    if not api_key:
        print("нет ключа Exa. Впишите его в config.json, раздел exa.api_key")
        con.close()
        return 1

    print("Поиск телефона. Параметры прогона:")
    print("      база     : %s" % db_path)
    print("      компаний : %d" % len(companies))
    print("      темп     : %.1f с на запрос, это примерно %d мин на прогон"
          % (POLITE_DELAY, round(len(companies) * POLITE_DELAY / 60) or 1))
    print()

    found, errors = 0, 0
    for row in companies:
        record = verify.canonical_record(row)
        name, inn = record.get("name"), record.get("inn")
        city = record.get("city")
        result = find_phone_for_company(name, inn, city, api_key)
        time.sleep(POLITE_DELAY)

        if result["status"] == "found":
            found += 1
            print("    %-13s %-16s %s" % (inn, result["phone"], result["source_url"]))
        elif result["status"] == "error":
            errors += 1
            print("    %-13s ошибка: %s" % (inn, result["error"]))
        else:
            print("    %-13s не найдено (совпадения по ИНН нет)" % inn)

        if result["status"] != "found":
            continue

        # Обновляем только телефонные поля, не трогая уже найденные сайт/почту/
        # директора — это не db_replace, полная замена строки стёрла бы всё,
        # что положил туда enrich_site.py на предыдущем шаге.
        existing = con.execute(
            "SELECT * FROM enrichment WHERE inn = ?", (inn,)
        ).fetchone()
        merged = dict(existing) if existing else {"inn": inn, "name": name}
        merged["phone"] = result["phone"]
        merged["phone_source"] = ("Exa-поиск, ИНН %s подтверждён в тексте %s"
                                  % (result["matched_inn"], result["source_url"]))
        merged["phone_checked_at"] = verify.now_stamp()
        if not merged.get("source"):
            merged["source"] = "телефон через Exa-поиск, сайта нет"
        if not merged.get("checked_at"):
            merged["checked_at"] = verify.now_stamp()
        verify.db_replace(con, "enrichment", merged)
        verify.db_upsert_company(con, {"inn": inn, "phone": result["phone"]})
        con.commit()

    con.close()
    print()
    print("телефон найден у %d из %d, ошибок Exa: %d"
          % (found, len(companies), errors))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    flags = ("--config", "--db", "--inn", "--inn-file", "--limit",
             "--only-missing", "--live", "--offline")
    # справка обязана печататься мгновенно, без единого запроса в сеть
    if "--help" in argv or "-h" in argv:
        return _cli(argv)
    if any(a.split("=")[0] in flags for a in argv):
        return _cli(argv)
    _demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
