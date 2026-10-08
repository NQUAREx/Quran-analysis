"""Bounded, source-backed comparisons between corpus results and calendar facts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


SOURCES = {
    "earth": {"name": "NASA, Earth Facts", "url": "https://science.nasa.gov/earth/facts/", "fact": "Длина года указана приблизительно как 365,25 суток."},
    "moon": {"name": "NASA Goddard, Moon Orbit", "url": "https://eclipse.gsfc.nasa.gov/SEhelp/moonorbit.html", "fact": "Средний синодический месяц: 29,53059 суток."},
    "calendar": {"name": "Encyclopaedia Britannica, Islamic calendar", "url": "https://www.britannica.com/topic/Islamic-calendar", "fact": "Лунный календарь состоит из 12 месяцев; год имеет 354 или 355 суток."},
    "ocean": {"name": "NASA, Earth Facts", "url": "https://science.nasa.gov/earth/facts/", "fact": "Океан покрывает около 71% поверхности Земли."},
    "moonlight": {"name": "NASA, Moon Phases", "url": "https://science.nasa.gov/moon/moon-phases/", "fact": "Луна не производит собственный свет; видимый лунный свет — отражённый солнечный."},
}


def _plain_finding(row: dict) -> dict:
    """Give each registered claim a human-readable unit and interpretation."""
    key = row["hypothesis_id"]
    value = row.get("observed")
    if row.get("family") == "primary_permutation":
        observed, shuffled = row["observed"], row["null_mean"]
        if "length_adjacency" in key:
            explanation = f"Соседние аяты отличаются по длине в среднем на {observed:.2f} токена; при выбранном перемешивании получилось {shuffled:.2f}. Меньшая разница означает более плавный переход длин."
        elif "length_position" in key:
            explanation = f"Сила связи длины с местом аята внутри суры — {observed:.3f} против {shuffled:.3f} у переставленных аятов. Это небольшая величина, хотя отличие от модели статистически замечено."
        elif "ending_" in key:
            letters = 2 if "ending_2" in key else 3
            explanation = f"У {100 * observed:.1f}% соседних пар совпадают последние {letters} письменные буквы; после перемешивания — у {100 * shuffled:.1f}%. Это наблюдение о звучании и записи концов аятов."
        else:
            explanation = f"Число {observed:.2f} — условный балл неравномерности мест частых слов внутри аята; после перестановки средний балл {shuffled:.2f}. Это не количество слов и не процент."
        explanation += " Вывод относится только к названной контрольной перестановке; связная речь обычно отличается от случайного порядка."
    elif key == "core_basmala_sensitivity":
        explanation = f"Во всём файле {value['file']} словоупотреблений; после исключения вступительных басмал — {value['numbered']}. Разницу в {value['difference_tokens']} создаёт оформление начала сур."
    elif key == "core_normalization_changes_vocabulary":
        explanation = f"Из-за выбранных знаков записи различают {value['raw']} форм; без них — {value['plain']}. Это объединение написаний, а не исчезновение фрагментов текста."
    elif key.startswith("I-morph-") and isinstance(value, dict):
        explanation = f"Во внешней разметке отмечено {value.get('segments', 0)} подходящих частей слов; они относятся к {value.get('tokens', 'неуказанному числу')} письменным токенам и {value.get('verses', 'неуказанному числу')} аятам. Одна часть слова и целое слово — разные единицы."
    elif key == "I-numeric-root-candidates":
        explanation = f"По 12 выбранным корням числовых слов отмечено {value['segments']} сегмента-кандидата. Корень сам по себе не сообщает, какое число и в каком смысле названо в аяте."
    elif key == "I-verb-person-transition":
        explanation = f"Из {value['verified_pairs']} сверенных пар соседних глаголов в {value['pairs_with_person_change']} меняется грамматическое лицо. Это может быть обычная смена говорящего или цитата; для риторического вывода нужен контекст."
    elif key == "J-month-12":
        explanation = "При отборе единственного числа слова «месяц» подтверждены 12 употреблений; формы двойственного и множественного числа сюда не включены. Сравнение с 12 месяцами года зависит от правила отбора."
    elif key == "J-day-365":
        explanation = "Для слова «день» надёжно сверены 362 употребления из 365 выбранных кандидатов, ещё 3 не выверены. Точного утверждения о 365 случаях в исходном файле пока нет."
    elif key == "J-basmala-114":
        explanation = "После нормализации полный файл содержит 114 басмал, как и сур; при счёте только нумерованных аятов остаются две. Это зависит от структуры вступлений."
    elif key == "J-basmala-19":
        explanation = "В выбранной записи формула содержит 19 букв. Длина меняется, если считать другие знаки или написания; внешняя физическая связь из этого числа не следует."
    elif key == "J-world-hereafter":
        explanation = "Пара 115:115 получена из внешней разметки при специальном отборе форм. Для второй единицы точное сопоставление с исходником недостаточно, поэтому равенство пока не подтверждено."
    elif key == "EX-C-001":
        one = value.get("plain/file", {})
        explanation = f"В записи без выбранных огласовок есть {one.get('repeated_groups', 0)} групп полностью одинаковых аятов, охватывающих {one.get('verses_in_groups', 0)} мест. При других правилах записи итог меняется."
    elif key == "EX-C-002":
        explanation = f"Найдено {value['groups']} групп длинных точных повторов. Это повторяющиеся последовательности слов, а не число разных смыслов."
    elif key == "EX-F-001":
        explanation = f"Из {value['equality_tested']} проверенных простых числовых равенств совпало {value['equality_matched']}. Их обнаружили поиском по многим формулам, поэтому совпадение требует осторожной интерпретации."
    elif key == "EX-G-001":
        first = value[0]
        explanation = f"Например, «{first['word_a']}» и «{first['word_b']}» встретились вместе в {first['count_ab']} аятах. Совместное появление часто связано с устойчивой формулой; высокий балл не доказывает необычное свойство мира."
    elif key == "EX-H-001":
        explanation = f"Исходный порядок сжался до {100 * value['observed_ratio']:.1f}% исходного размера, перемешанные варианты — в среднем до {100 * value['shuffle_mean_ratio']:.1f}%. Повторы и обычная языковая структура помогают сжатию."
    elif key == "EXT-universality-basmala":
        explanation = "Во всём файле слова вступительной формулы встречаются во всех сурах, а после исключения вступлений такого полного охвата нет. Это эффект способа хранения текста."
    elif key == "EXT-refrain-endings":
        first = value[0]
        explanation = f"Последние две буквы совпадают у {100 * first['all_rate']:.1f}% соседних пар; после исключения повторённых аятов — у {100 * first['retained_rate']:.1f}%. Значит, наблюдение нельзя объяснить только одним частым рефреном."
    elif key == "EXT-equality-search-volume":
        explanation = f"В полном файле есть {value['pairs_file']:,} пар форм с одинаковой частотой. Поиск среди десятков миллионов пар создаёт много совпадений; равная частота не означает связь значений.".replace(",", " ")
    else:
        raise ValueError(f"Нет простого объяснения для {key}")
    return {"id": key, "title": row["title_ru"], "explanation": explanation, "scope": row.get("scope", ""),
            "profile": row.get("profile_id", ""), "evidence_path": row.get("evidence_path", ""), "family": row.get("family", "")}


def run(root: Path) -> dict:
    root = Path(root)
    core = json.loads((root / "results/core_summary.json").read_text())
    morph = json.loads((root / "results/morphology_summary.json").read_text())
    stats = json.loads((root / "results/statistics_summary.json").read_text())
    explore = json.loads((root / "results/exploration_summary.json").read_text())
    extension = json.loads((root / "results/extension_summary.json").read_text())
    claims = {row["hypothesis_id"]: row for row in morph["claims"]}
    findings = json.loads((root / "results/hypotheses/registry.json").read_text())
    with sqlite3.connect(root / "data/processed/corpus.sqlite") as db:
        verses = {address: db.execute("SELECT raw_text FROM verses WHERE verse_id=?", (address,)).fetchone()[0]
                  for address in ("9:36", "18:25", "2:189", "10:5")}
        sea_count = db.execute("SELECT count(*) FROM tokens WHERE plain='البحر'").fetchone()[0]
        land_count = db.execute("SELECT count(*) FROM tokens WHERE plain='البر'").fetchone()[0]
    solar_days = 365.25
    synodic_days = 29.53059
    lunar_equivalent = 300 * solar_days / (12 * synodic_days)
    day = claims["J-day-365"]["observed"]
    month = claims["J-month-12"]["observed"]
    basmala = claims["J-basmala-114"]["observed"]
    afterlife = claims["J-world-hereafter"]["observed"]
    refrain = explore["top_repeated_verses"][0]
    vocabulary = {(row["profile_id"], row["scope"]): row for row in core["vocabulary"]}
    cases = [
        {"id": "verse_twelve_months", "kind": "direct", "title": "Двенадцать месяцев названы в самом тексте",
         "number": "12 месяцев", "plain": "В аяте 9:36 прямо сказано, что месяцев двенадцать. Это понятное календарное утверждение, а не результат подсчёта слов.",
         "interpretation": "Лунный календарь действительно устроен как последовательность 12 месяцев. Совпадение здесь смысловое и открытое: число уже записано в аяте.",
         "limit": "Аят не задаёт длину каждого месяца и не доказывает никакую частотную формулу.",
         "evidence": [{"label": "9:36", "text": verses["9:36"]}], "sources": ["calendar"]},
        {"id": "solar_lunar_years", "kind": "compatible", "title": "300 солнечных лет близки к 309 лунным",
         "number": f"{lunar_equivalent:.2f} лунного года", "plain": "В 18:25 говорится о трёхстах годах и ещё девяти. Если взять 300 лет по 365,25 суток и разделить на 12 средних лунных месяцев по 29,53059 суток, получится примерно 309,21 лунного года.",
         "interpretation": "Числа совместимы с пересчётом между солнечным и лунным годом: добавка около девяти лет имеет понятный календарный масштаб.",
         "limit": "Сам аят не называет первые годы солнечными, а добавленные — лунными. Это возможное объяснение чисел, не доказательство авторского замысла. Средние астрономические периоды не равны фактическим датам конкретного календаря.",
         "calculation": {"solar_year_days": solar_days, "synodic_month_days": synodic_days, "months_per_lunar_year": 12, "solar_years": 300, "lunar_years": lunar_equivalent, "verse_total": 309},
         "evidence": [{"label": "18:25", "text": verses["18:25"]}], "sources": ["earth", "moon"]},
        {"id": "moon_as_calendar", "kind": "direct", "title": "Фазы Луны связаны с отсчётом времени",
         "number": "около 29,53 суток", "plain": "В 2:189 лунные серпы названы отметками времени для людей и паломничества. В наблюдаемом мире цикл лунных фаз занимает в среднем 29,53059 суток.",
         "interpretation": "Это качественная связь текста с практикой календарного счёта. Число 29,53059 взято из астрономического источника; оно не записано в аяте.",
         "limit": "Аят не сообщает точную астрономическую длительность цикла.",
         "evidence": [{"label": "2:189", "text": verses["2:189"]}], "sources": ["moon", "calendar"]},
        {"id": "sun_moon_light", "kind": "compatible", "title": "Солнце и Луна описаны разными словами о свете",
         "number": "ضياء / نور", "plain": "В 10:5 для Солнца и Луны употреблены разные слова о свете. В наблюдаемом мире Луна действительно видна благодаря отражённому солнечному свету.",
         "interpretation": "Слова совместимы с различием светящего тела и освещённого тела, но сам аят не объясняет механизм отражения.",
         "limit": "Различие лексики само по себе не доказывает научного предсказания; перевод и значение слов требуют отдельного филологического анализа.",
         "evidence": [{"label": "10:5", "text": verses["10:5"]}], "sources": ["moonlight"]},
        {"id": "month_word_twelve", "kind": "conditional", "title": "Слово «месяц»: 12 выбранных вхождений",
         "number": f"{month['verified_included']} вхождений", "plain": f"При заранее заданном правиле подсчёта единственного числа شهر нашлось {month['verified_included']} проверенных вхождений. Ещё {month['external_categories']['excluded_dual_plural']} форм двойственного и множественного числа исключены этим правилом.",
         "interpretation": "Число совпадает с количеством месяцев в календарном году. Это частотное совпадение, которое зависит от грамматического отбора.",
         "limit": "Подсчёт относится к выровненной внешней разметке; другое правило включения форм даёт другой итог. Совпадение не показывает причинной связи.",
         "evidence": [{"label": "Таблица вхождений", "path": "results/tables/morphology_claim_occurrences.csv.gz", "filter": "claim_id=J-month-12"}], "sources": ["calendar"]},
        {"id": "day_word_year", "kind": "unconfirmed", "title": "«День = 365» не подтверждён полностью",
         "number": f"{day['verified_included']} проверенных + {day['unverified_included']} невыверенных", "plain": f"Внешний список даёт {day['external_categories']['included_singular']} подходящих кандидатов, но с исходным файлом надёжно сопоставлены только {day['verified_included']}. Ещё {day['unverified_included']} требуют проверки. При этом обычный солнечный год описывают примерно 365,25 суток, календарный бывает и високосным, а лунный — 354 или 355 суток.",
         "interpretation": "Заявлять точное равенство 365 для пользовательского файла пока нельзя.",
         "limit": "Другие формы слова, суффиксы и область счёта меняют число; даже точное совпадение с выбранным годом не было бы проверкой причинной связи.",
         "evidence": [{"label": "Таблица вхождений", "path": "results/tables/morphology_claim_occurrences.csv.gz", "filter": "claim_id=J-day-365"}], "sources": ["earth", "calendar"]},
        {"id": "basmala_surahs", "kind": "internal", "title": "114 басмал и 114 сур — связь структуры файла",
         "number": f"{basmala['observed']['plain/file']} и {core['surahs']}", "plain": f"После удаления огласовок в полном файле найдено {basmala['observed']['plain/file']} басмал и {core['surahs']} сур. В области только нумерованных аятов басмал уже {basmala['observed']['plain/numbered']}.",
         "interpretation": "Равенство описывает устройство именно этого файла и размещение вступительных формул, а не факт о природе или календаре.",
         "limit": "В точной исходной записи без нормализации найдено другое число: 112. Правило письма и включение вступлений существенны.",
         "evidence": [{"label": "Результат проверки", "path": "results/morphology_summary.json", "filter": "claims / J-basmala-114"}], "sources": []},
        {"id": "world_hereafter", "kind": "unconfirmed", "title": "Пара «земная жизнь — последняя» пока не проверена как 115:115",
         "number": "115:115 во внешней разметке", "plain": f"Во внешнем морфологическом словаре выбранные формы обеих лемм дают по 115. Для второй леммы в исходном файле точное сегментное выравнивание покрывает {afterlife['fixed_lemma_counts'][1]['verified_feminine_singular']} таких случаев.",
         "interpretation": "На текущих данных это нельзя предъявлять как проверенное равенство в исходном тексте.",
         "limit": "Потребуется ручная проверка значения и связи каждой формы; простое совпадение внешних итогов этого не заменяет.",
         "evidence": [{"label": "Результат проверки", "path": "results/morphology_summary.json", "filter": "claims / J-world-hereafter"}], "sources": []},
        {"id": "sea_land", "kind": "unconfirmed", "title": "Слова «море» и «суша» не дают долю океана при простом счёте",
         "number": f"{sea_count}:{land_count} словоформ", "plain": f"В профиле plain полные формы «البحر» и «البر» встречаются {sea_count} и {land_count} раз. Доля первой среди этих двух форм — {100 * sea_count / (sea_count + land_count):.1f}%, тогда как океан покрывает примерно 71% поверхности Земли.",
         "interpretation": "При этом чётко названном правиле подсчёта равенства с долей воды нет.",
         "limit": "Формы с приставками и другие слова могут дать иные числа; нельзя подбирать их после просмотра результата и затем объявлять совпадение заранее предсказанным.",
         "evidence": [{"label": "Полный словарь", "path": "results/tables/core_vocabulary_plain_file.csv.gz", "filter": "form=البحر; form=البر"}], "sources": ["ocean"]},
        {"id": "refrain", "kind": "internal", "title": "Повторяющийся аят — наблюдаемый приём текста",
         "number": f"{refrain['frequency']} раз", "plain": f"Фраза «{refrain['sequence']}» встречается как целый аят {refrain['frequency']} раз. Читатель может увидеть все адреса в таблице повторов.",
         "interpretation": "Это реальный повтор внутри композиции. Само число 31 не сопоставлено с независимым фактом внешнего мира.",
         "limit": "Частота зависит от сравнения без огласовок; не следует приписывать числу 31 особый физический смысл.",
         "evidence": [{"label": "Повторяющиеся аяты", "path": "results/tables/exploration_exact_verse_repeats.csv"}], "sources": []},
    ]
    plain_numbers = [
        {"number": f"{core['surahs']} сур · {core['ayahs']} аятов", "meaning": "Это число разделов и пронумерованных строк в данном файле, а не оценка возможных редакций."},
        {"number": f"{core['tokens_file']} словоупотреблений", "meaning": "Считались письменные токены, включая вступительные формулы; без 448 токенов этих вступлений остаётся 77 800."},
        {"number": f"{vocabulary[('raw', 'file')]['types']} → {vocabulary[('plain', 'file')]['types']} форм", "meaning": "Если убрать выбранные огласовки, разные написания объединяются. Это изменение правила записи, а не исчезновение слов из текста."},
        {"number": f"{vocabulary[('plain', 'file')]['hapax_types']} форм по одному разу", "meaning": "Столько разных написаний встретилось лишь однажды; это число словоформ, а не число уникальных смыслов или корней."},
        {"number": f"{morph['verified_tokens']} из {morph['source_tokens_numbered']}", "meaning": "Лемма и корень сверены примерно для 91% токенов области numbered. Отсутствующие 9% нельзя объявлять нулевыми частотами."},
        {"number": f"{stats['primary_test_count']} проверки порядка", "meaning": "Контрольные перестановки проверяют, отличается ли порядок текста от перемешанного. Отличие ожидаемо для обычной связной речи и не указывает само по себе на чудо или намеренный числовой код."},
        {"number": f"{stats['strongest_test']['observed']:.2f} против {stats['strongest_test']['null_mean']:.2f}", "meaning": "Соседние аяты в среднем отличаются по длине примерно на 5,98 слова; после перемешивания аятов внутри сур — на 6,59. Это наблюдение о композиции и порядке."},
        {"number": f"{refrain['frequency']} повторов рефрена", "meaning": "Один и тот же полный аят повторяется 31 раз. Это видимый при чтении приём композиции, а не число дней или месяцев."},
        {"number": f"{extension['equality_pairs']['pairs_file']:,} пар одинаковой частоты".replace(",", " "), "meaning": "При переборе десятков миллионов пар равные частоты неизбежны часто; отдельно взятое равенство без заранее выбранного правила мало что объясняет."},
    ]
    terms = [
        {"term": "Токен и словоформа", "meaning": "Токен — одно место слова в тексте; словоформа — вид написания. Одно написание может встречаться тысячи раз."},
        {"term": "Лемма и корень", "meaning": "Лемма объединяет грамматические формы словарной единицы; корень объединяет ещё более широкую семью. Эти уровни нельзя смешивать с точным написанием."},
        {"term": "raw / nfc / plain / search", "meaning": "Это четыре правила представления того же текста: от исходной записи до формы для поиска. При смене правила меняются некоторые частоты."},
        {"term": "file / numbered", "meaning": "file включает все слова файла; numbered исключает распознанные вступительные басмалы перед аятами. Разница здесь — 448 токенов."},
        {"term": "p-value и поправка Holm", "meaning": "p-value показывает, насколько необычен результат внутри выбранной модели случайной перестановки. Поправка Holm учитывает много одновременных проверок. Это не вероятность истинности и не доказательство сверхъестественной причины."},
        {"term": "PMI и совместная встречаемость", "meaning": "PMI отмечает пары слов, которые оказываются вместе чаще ожидания простой частотной модели. Редкая пара может получить высокий балл случайно."},
        {"term": "Энтропия и сжатие", "meaning": "Это меры предсказуемости и повторяемости записи. Текст с повторами обычно лучше сжимается; число не измеряет смысл или качество."},
        {"term": "SHA-256", "meaning": "Контрольный отпечаток файла. Он нужен, чтобы проверить, что расчёты сделаны на той же последовательности байтов."},
    ]
    result = {"schema_version": "1.0.0", "scope": "Десять явно перечисленных сопоставлений: календарные числа и циклы, свет Солнца и Луны, доля океана, известные частотные утверждения и внутренние структурные результаты. Это конечный разбор, а не все мыслимые параллели с миром.",
              "source_corpus_sha256": core["sha256"], "sources": SOURCES, "plain_numbers": plain_numbers, "terms": terms, "cases": cases,
              "plain_findings": [_plain_finding(row) for row in findings],
              "reading_rule": "Прямое содержание аята, арифметическая совместимость, условное совпадение частот и неподтверждённое утверждение имеют разную силу. Поиск совпадений задним числом не даёт вероятности случайности без полного набора проверенных вариантов и контрольной модели."}
    (root / "results/real_world.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Числа обычным языком и связи с реальным миром", "", result["scope"], "", "## Как читать главные цифры", ""]
    lines += [f"- **{row['number']}** — {row['meaning']}" for row in plain_numbers]
    lines += ["", "## Словарь показателей", ""]
    lines += [f"- **{row['term']}** — {row['meaning']}" for row in terms]
    lines += ["", "## Сопоставления", ""]
    for case in cases:
        lines += [f"### {case['title']}", "", f"**{case['number']}**. {case['plain']}", "", case["interpretation"], "", f"Граница вывода: {case['limit']}", ""]
        lines += [f"- Источник текста/подсчёта: {e['label']} — {e.get('text', e.get('path', ''))}" for e in case["evidence"]]
        lines += [f"- Внешний источник: [{SOURCES[key]['name']}]({SOURCES[key]['url']}) — {SOURCES[key]['fact']}" for key in case["sources"]]
        lines.append("")
    lines += ["## Правило интерпретации", "", result["reading_rule"], ""]
    lines += ["## Все зарегистрированные находки простым языком", "", "Одна строка ниже соответствует одной карточке каталога гипотез. Полные значения и методы сохраняются в `results/hypotheses/registry.json`.", ""]
    lines += [f"- **{row['title']}** (`{row['id']}`): {row['explanation']}" for row in result["plain_findings"]]
    (root / "report/REAL_WORLD.md").write_text("\n".join(lines), encoding="utf-8")
    return result


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[2])
