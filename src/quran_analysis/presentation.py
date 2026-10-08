"""Portable, source-backed Russian presentation of the calculated corpus.

The HTML deliberately uses neither a CDN nor fetch(): it also works at file://.
All displayed corpus values, figures and spreadsheet summaries originate here
from the same SQLite rows. Large analytical tables remain available in full.
"""
from __future__ import annotations

import csv
import base64
import gzip
import html
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from itertools import islice
from pathlib import Path


PROFILE_NAMES = {"raw": "Исходная поверхность", "nfc": "Unicode NFC", "plain": "Без выбранных огласовок", "search": "Поисковое объединение"}
SCOPE_NAMES = {"file": "Все токены файла", "numbered": "Без начальных басмал-префиксов"}


def _read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _rows(path, limit=None):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader if limit is None else islice(reader, limit))


def _write_csv(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]) if rows else fields or [])
        writer.writeheader()
        writer.writerows(rows)


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _flatten(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(obj, list):
        yield {"field": prefix, "value": json.dumps(obj, ensure_ascii=False)}
    else:
        yield {"field": prefix, "value": obj}


def _corpus_data(root):
    db = sqlite3.connect(root / "data/processed/corpus.sqlite")
    db.row_factory = sqlite3.Row
    verses = [dict(r) for r in db.execute("SELECT * FROM verses ORDER BY global_ayah")]
    tokens = [dict(r) for r in db.execute("SELECT * FROM tokens ORDER BY global_token")]
    db.close()
    forms = {profile: [] for profile in PROFILE_NAMES}
    form_ids = {profile: {} for profile in PROFILE_NAMES}
    verse_index = {v["verse_id"]: i for i, v in enumerate(verses)}
    token_rows, token_index = [], {}
    for i, token in enumerate(tokens):
        ids = []
        for profile in PROFILE_NAMES:
            surface = token[profile]
            if surface not in form_ids[profile]:
                form_ids[profile][surface] = len(forms[profile])
                forms[profile].append(surface)
            ids.append(form_ids[profile][surface])
        token_index[token["token_id"]] = i
        token_rows.append([verse_index[token["verse_id"]], token["token_in_ayah"], token["global_token"], token["token_in_surah"], token["raw_start"], token["raw_end"], *ids, token["is_opening_basmala"], token["letter_count"], token["mark_count"]])
    verse_rows = [[v[k] for k in ("verse_id", "surah_id", "ayah_id", "global_ayah", "raw_text", "raw_start", "raw_end", "token_count", "letter_count", "mark_count")] for v in verses]
    metrics, surahs, histograms, ranks = [], [], [], []
    for scope in SCOPE_NAMES:
        selected = [t for t in tokens if scope == "file" or not t["is_opening_basmala"]]
        scoped_verses = Counter(t["verse_id"] for t in selected)
        for profile in PROFILE_NAMES:
            freq = Counter(t[profile] for t in selected)
            profile_counts = {surface: (sum(unicodedata.category(c).startswith("L") and c != "\u0640" for c in surface), sum(unicodedata.category(c).startswith("M") for c in surface)) for surface in freq}
            metrics.append({"scope": scope, "profile_id": profile, "tokens": len(selected), "types": len(freq), "surahs": len({t["surah_id"] for t in selected}), "verses": len(verses), "codepoints": sum(len(surface)*count for surface, count in freq.items()), "letters": sum(profile_counts[surface][0]*count for surface, count in freq.items()), "marks": sum(profile_counts[surface][1]*count for surface, count in freq.items())})
            if profile == "plain":
                for rank, (form, count) in enumerate(freq.most_common(), 1):
                    ranks.append({"scope": scope, "profile_id": profile, "rank": rank, "form": form, "frequency": count, "tokens_denominator": len(selected)})
        buckets = defaultdict(list)
        for t in selected:
            buckets[t["surah_id"]].append(t)
        verse_counts = Counter(v["surah_id"] for v in verses)
        for surah_id, ts in sorted(buckets.items()):
            n = len(ts)
            surahs.append({"scope": scope, "profile_id": "plain", "surah_id": surah_id, "ayahs": verse_counts[surah_id], "tokens": n, "types_plain": len({t["plain"] for t in ts}), "letters": sum(t["letter_count"] for t in ts), "marks": sum(sum(unicodedata.category(c).startswith("M") for c in t["plain"]) for t in ts), "mean_tokens_per_ayah": n / verse_counts[surah_id]})
        lengths = Counter(scoped_verses.get(v["verse_id"], 0) for v in verses)
        for length, count in sorted(lengths.items()):
            histograms.append({"scope": scope, "tokens_per_ayah": length, "ayah_count": count, "ayahs_denominator": len(verses)})
    return {"forms": forms, "tokens": token_rows, "verses": verse_rows, "metrics": metrics, "surahs": surahs, "lengths": histograms}, token_index, ranks


def _morphology(root, token_index):
    path = root / "results/tables/morphology_segments.csv.gz"
    units = {"lemma": {}, "root": {}}
    pos_counts, samples = Counter(), []
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("alignment_status") != "verified":
                    continue
                indices = [token_index[x] for x in row.get("token_ids", "").split("|") if x in token_index]
                if not indices:
                    continue
                pos_counts[row.get("pos", "")] += 1
                for key in units:
                    label = row.get(key, "")
                    if label and label not in {"None", "null", "nan"}:
                        item = units[key].setdefault(label, {"unit": label, "tokens": set(), "segments": 0, "pos": set()})
                        item["tokens"].update(indices)
                        item["segments"] += 1
                        item["pos"].add(row.get("pos", ""))
                if len(samples) < 30:
                    samples.append(row)
    return {"units": {key: [{**v, "tokens": sorted(v["tokens"]), "pos": sorted(v["pos"])} for v in sorted(values.values(), key=lambda x: (-len(x["tokens"]), x["unit"]))] for key, values in units.items()}, "pos": [{"pos": pos, "segments": count} for pos, count in pos_counts.most_common()], "samples": samples}


def _figures(root, data, ranks):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    folder = root / "results/figures"
    folder.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#9aa6a6", "axes.labelcolor": "#243333", "text.color": "#243333", "savefig.facecolor": "#fbfaf7", "axes.facecolor": "#fbfaf7", "figure.facecolor": "#fbfaf7", "svg.fonttype": "none"})
    metadata = []
    def save(fig, name, title, caption, rows):
        fig.text(.01, .015, caption, fontsize=8, color="#4f6363", va="bottom")
        fig.tight_layout(rect=(0, .1, 1, .95))
        fig.savefig(folder / f"{name}.svg", bbox_inches="tight")
        fig.savefig(folder / f"{name}.png", dpi=170, bbox_inches="tight")
        plt.close(fig)
        _write_csv(folder / f"{name}.csv", rows)
        metadata.append({"id": name, "title_ru": title, "caption_ru": caption, "svg": f"results/figures/{name}.svg", "png": f"results/figures/{name}.png", "data": f"results/figures/{name}.csv", "image_uri": "data:image/svg+xml;base64," + base64.b64encode((folder / f"{name}.svg").read_bytes()).decode("ascii")})
    rows = [r for r in data["surahs"] if r["scope"] == "numbered"]
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar([r["surah_id"] for r in rows], [r["tokens"] for r in rows], color="#137c74", width=.85)
    ax.set(title="Как различается объём сур?", xlabel="Номер суры", ylabel="Орфографические токены, шт.")
    save(fig, "surah_tokens", "Объём сур", "Все суры; scope=numbered, профиль plain. Абсолютные количества; цвет обозначает объём.\nИз знаменателя исключены только начальные басмалы-префиксы.", rows)
    rows = [r for r in ranks if r["scope"] == "numbered"]
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.loglog([r["rank"] for r in rows], [r["frequency"] for r in rows], color="#137c74", linewidth=1.6)
    ax.set(title="Как частота меняется с рангом словоформы?", xlabel="Ранг словоформы (логарифмическая шкала)", ylabel="Частота, токенов (логарифмическая шкала)")
    save(fig, "rank_frequency", "Ранг и частота словоформ", "Все словоформы plain, scope=numbered; абсолютные частоты. Обе оси логарифмические.\nЛиния соединяет ранжированные наблюдения; модель степенного закона не подгонялась.", rows)
    rows = [r for r in data["lengths"] if r["scope"] == "numbered"]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar([r["tokens_per_ayah"] for r in rows], [r["ayah_count"] for r in rows], color="#9e6646", width=1)
    ax.set(title="Сколько токенов содержит аят?", xlabel="Орфографические токены в аяте, шт.", ylabel="Количество аятов, шт.")
    save(fig, "verse_lengths", "Распределение длины аятов", "Все нумерованные аяты; scope=numbered, профиль plain; шаг 1 токен. Абсолютные количества.\nДлины измерены после исключения начальных басмал-префиксов; внутренних басмал это не касается.", rows)
    rows = data["metrics"]
    fig, ax = plt.subplots(figsize=(9, 5))
    labels = list(PROFILE_NAMES)
    for offset, scope, color in [(-.18, "file", "#137c74"), (.18, "numbered", "#bd8054")]:
        vals = [next(r["types"] for r in rows if r["profile_id"] == p and r["scope"] == scope) for p in labels]
        ax.bar([i+offset for i in range(len(labels))], vals, width=.36, label=SCOPE_NAMES[scope], color=color)
    ax.set_xticks(range(len(labels)), labels)
    ax.set(title="Как нормализация меняет число словоформ?", xlabel="Профиль поверхности", ylabel="Различных словоформ, шт.")
    ax.legend(fontsize=9)
    save(fig, "profile_sensitivity", "Чувствительность к профилю текста", "Весь корпус; показаны оба scope. Абсолютное число различных поверхностей орфографических токенов.\nУменьшение словаря означает объединение написаний; это не лемматизация.", rows)
    growth = root / "results/tables/exploration_vocabulary_growth.csv"
    if growth.exists():
        rows = _rows(growth)
        if rows and "token_position" in rows[0] and "types" in rows[0]:
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot([int(r["token_position"]) for r in rows], [int(r["types"]) for r in rows], color="#137c74")
            ax.set(title="Как растёт словарь по мере чтения файла?", xlabel="Глобальная позиция токена в файле", ylabel="Накоплено словоформ, шт.")
            save(fig, "vocabulary_growth", "Накопление словаря", "Профиль plain; scope=file. Порядок исходного файла; абсолютные количества.\nЛиния соединяет рассчитанные контрольные позиции без сглаживания.", rows)
    tests_path = root / "results/tables/statistical_tests.csv"
    draws_path = root / "results/tables/statistical_null_draws.csv.gz"
    if tests_path.exists() and draws_path.exists():
        tests = _rows(tests_path)
        selected = [r for r in tests if r["scope"] == "numbered" and r["null_model"] == "verse_shuffle_within_surah" and ("length_adjacency" in r["hypothesis_id"] or "ending_2" in r["hypothesis_id"])]
        draw_rows = _rows(draws_path)
        for test in selected:
            rows = [r for r in draw_rows if r["hypothesis_id"] == test["hypothesis_id"]]
            if not rows:
                continue
            fig, ax = plt.subplots(figsize=(10, 5))
            values = [float(r["value"]) for r in rows]
            ax.hist(values, bins=30, color="#137c74", alpha=.85, label=f"Контроли, n={len(values)}")
            ax.axvline(float(test["observed"]), color="#ae5937", linewidth=2, label="Наблюдаемый результат")
            metric = "Средняя абсолютная разность длин соседних аятов, токенов" if "length_adjacency" in test["hypothesis_id"] else "Доля соседних аятов с одинаковыми последними 2 буквами"
            ax.set(title=test["title_ru"], xlabel=metric, ylabel="Контрольных повторов, шт.")
            ax.legend(fontsize=9)
            caption = f"Профиль {test['profile_id']}; scope={test['scope']}; {test['n_units']} соседних пар внутри сур; 30 интервалов.\nЗелёный: перестановки целых аятов внутри сур; оранжевый: исходный порядок. Holm p={float(test['p_adjusted']):.3g}."
            rows = [{"hypothesis_id": r["hypothesis_id"], "simulation": int(r["simulation"]), "value": float(r["value"]), "observed": float(test["observed"]), "profile_id": test["profile_id"], "scope": test["scope"]} for r in rows]
            save(fig, "statistic_" + ("length_adjacency" if "length_adjacency" in test["hypothesis_id"] else "ending_2"), test["title_ru"], caption, rows)
    return metadata


def _inventory(root):
    paths = sorted((root / "results/tables").glob("*.csv*"))
    paths += sorted((root / "results/hypotheses").glob("*.json"))
    paths += sorted((root / "results").glob("*_coverage.json"))
    paths += [p for p in sorted((root / "results").glob("*_summary.json")) if p.name != "presentation_summary.json"]
    paths += sorted((root / "data/processed").glob("*.sqlite.gz"))
    paths += sorted((root / "docs").glob("*.md"))
    paths += [root / "results/real_world.json", root / "report/REAL_WORLD.md"]
    paths += sorted((root / "results/figures").glob("*"))
    paths += [p for p in (root / "results/core_checks.json", root / "results/validation_checks.json", root / "docs/QUERIES.sql", root / "config/analysis.json") if p.exists()]
    result = []
    for path in paths:
        result.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "format": ".".join(path.suffixes).lstrip(".")})
    return result


def _excel_value(key, value):
    """Infer numbers without treating every column containing 'token' as text."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    field = key.lower()
    lexical = re.fullmatch(r"(?:raw|nfc|plain|search|form|word|token|lemma|root|unit|character|char|letter|sequence|ngram)(?:_[ab]|_bw)?", field)
    identifier = field.endswith("_id") and field not in {"surah_id", "ayah_id"}
    if lexical or identifier or field in {"code", "codepoint", "address", "suffix", "prefix"}:
        return value
    if re.fullmatch(r"[-+]?\d+", value):
        # Excel stores at most 15 significant decimal digits exactly.
        return int(value) if len(value.lstrip("-+0")) <= 15 else value
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", value):
        result = float(value)
        return result if math.isfinite(result) else None
    return value


def _spreadsheet(root, data, findings, coverage, summaries, inventory):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    wb = Workbook()
    wb.remove(wb.active)
    sheet_map = []
    names = set()
    def add_sheet(title, rows, source="", fields=None):
        title = title[:31]
        base = title
        i = 2
        while title in names:
            title = f"{base[:27]}_{i}"
            i += 1
        names.add(title)
        ws = wb.create_sheet(title)
        fields = fields or (list(rows[0]) if rows else ["Статус"])
        ws.append(fields)
        for row_number, row in enumerate(rows, 2):
            values = []
            for field in fields:
                value = row.get(field)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                if isinstance(value, str):
                    value = ILLEGAL_CHARACTERS_RE.sub("", value)
                    if len(value) > 32767:
                        value = f"Полное значение доступно в {source}; длина {len(value)} символов, превышает лимит ячейки XLSX."
                if isinstance(value, float) and not math.isfinite(value):
                    value = None
                values.append(value)
            ws.append(values)
            for column in range(1, len(fields) + 1):
                cell = ws.cell(row_number, column)
                # Explicitly text: addresses such as 2:255 cannot become time;
                # corpus text beginning with '=' cannot become a formula.
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                    cell.number_format = "@"
                    cell.alignment = Alignment(vertical="top", readingOrder=2 if any("\u0600" <= c <= "\u06ff" for c in cell.value) else 0)
                    value = cell.value
                    if value.startswith(("https://", "http://")):
                        cell.hyperlink = value
                        cell.font = Font(color="176C65", underline="single")
                    elif (value.startswith(("results/", "docs/", "data/", "config/", "src/")) and (root / value).is_file()):
                        cell.hyperlink = "../" + value
                        cell.font = Font(color="176C65", underline="single")
                elif isinstance(cell.value, float):
                    cell.number_format = "0.0000"
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.sheet_view.showGridLines = False
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="176C65")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[1].height = 32
        for j, field in enumerate(fields, 1):
            width = min(48, max(14, len(field) + 2, *(min(len(str(row.get(field, ""))), 40) + 2 for row in rows[:50])))
            ws.column_dimensions[get_column_letter(j)].width = width
        sheet_map.append({"Лист": title, "Строк данных": len(rows), "Источник": source or "data/processed/corpus.sqlite"})
        return ws
    notes = [
        {"Раздел": "Назначение", "Описание": "Обзорные и полные компактные таблицы. Полные вхождения и большие результаты доступны по путям на листе Файлы; XLSX не заменяет SQLite и CSV.gz."},
        {"Раздел": "Счёт", "Описание": "Токен — орфографическая единица по правилам core. raw/nfc/plain/search — поверхности, не леммы. Все числа рассчитаны по данному файлу."},
        {"Раздел": "Область", "Описание": "file включает все токены; numbered исключает только начальные басмалы-префиксы; басмалы 1:1 и 27:30 сохраняются."},
        {"Раздел": "Типы", "Описание": "Числовые поля — числа; арабские формы и адреса — текст. Пустая ячейка означает отсутствие значения, а не ноль."},
        {"Раздел": "Формат CSV", "Описание": "UTF-8; разделитель — запятая, quoting по стандарту CSV. CSV.gz распаковывается gzip/7-Zip. Идентификаторы импортировать как текст."},
        {"Раздел": "Пределы экспорта", "Описание": "В XLSX включены таблицы CSV не более 20 000 строк и 3 МБ. Большие CSV и все CSV.gz не сокращаются: вместо них даётся путь к полному файлу. Максимум Excel: 1 048 576 строк, 16 384 столбца, 32 767 символов на ячейку."},
    ]
    add_sheet("Начать здесь", notes)
    add_sheet("Корпус и профили", data["metrics"], "results/tables/presentation_corpus_metrics.csv")
    add_sheet("Суры", data["surahs"], "results/tables/presentation_surah_metrics.csv")
    add_sheet("Длины аятов", data["lengths"], "results/tables/presentation_verse_lengths.csv")
    add_sheet("Гипотезы", findings, "results/hypotheses/*.json", sorted({k for f in findings for k in f}) if findings else None)
    add_sheet("Карта покрытия", coverage, "results/*_coverage.json", sorted({k for f in coverage for k in f}) if coverage else None)
    flat = [{"module": module, **item} for module, obj in summaries.items() for item in _flatten(obj)]
    add_sheet("Паспорта модулей", flat, "results/*_summary.json")
    title_map = {
        "core_quality_issues": "Качество", "core_unicode_inventory": "Unicode", "core_normalization_collisions": "Коллизии профилей",
        "morphology_pos_frequencies": "Части речи", "morphology_root_frequencies": "Корни", "morphology_lemma_frequencies": "Леммы",
        "exploration_exact_verse_repeats": "Повторы аятов", "exploration_long_repeats": "Длинные повторы", "exploration_surah_similarity": "Сходство сур",
        "exploration_information": "Энтропия", "statistical_tests": "Статистические тесты",
    }
    skipped = []
    for path in sorted((root / "results/tables").glob("*.csv*")):
        if path.name.startswith("presentation_"):
            continue
        relative = str(path.relative_to(root))
        if path.suffix == ".gz" or path.stat().st_size > 3_000_000:
            skipped.append({"Файл": relative, "Причина": "Большая/сжатая таблица; доступна целиком в файле, без усечения"})
            continue
        rows = _rows(path)
        if len(rows) > 20_000:
            skipped.append({"Файл": relative, "Причина": f"{len(rows)} строк; доступна целиком в файле, без усечения"})
            continue
        for row in rows:
            for key, value in row.items():
                row[key] = _excel_value(key, value)
        add_sheet(title_map.get(path.stem, path.stem.replace("exploration_", "Иссл_").replace("statistics_", "Стат_").replace("morphology_", "Морф_").replace("core_", "База_")), rows, relative)
    add_sheet("Полные внешние таблицы", skipped)
    add_sheet("Файлы", [{"Путь": r["path"], "Байт": r["bytes"], "Формат": r["format"]} for r in inventory])
    fields = [
        {"Поле": "scope", "Значение": "file / numbered; правила басмал см. Начать здесь", "Единица": "категория"},
        {"Поле": "profile_id", "Значение": "raw / nfc / plain / search, не морфологический уровень", "Единица": "категория"},
        {"Поле": "tokens / token_count", "Значение": "Орфографические токены по правилам корпуса", "Единица": "шт."},
        {"Поле": "types / types_plain", "Значение": "Различные поверхности по указанному профилю", "Единица": "шт."},
        {"Поле": "letters / marks", "Значение": "Базовые письменные буквы / учитываемые диакритические знаки", "Единица": "кодовые точки"},
        {"Поле": "verse_id / token_id", "Значение": "Адрес суры:аята / суры:аята:токена", "Единица": "текстовый идентификатор"},
        {"Поле": "raw_start / raw_end", "Значение": "Начало включительно / конец исключительно в неизменном Unicode-тексте", "Единица": "кодовые точки, от 0"},
        {"Поле": "p_value / q_value", "Значение": "Хвостовая вероятность выбранной нулевой модели / скорректированная величина; не вероятность истинности утверждения", "Единица": "доля"},
        {"Поле": "segment_count / token_count", "Значение": "Число сегментов внешней разметки / число различных связанных орфографических токенов; знаменатели различаются", "Единица": "шт."},
    ]
    add_sheet("Словарь полей", fields, "docs/INTEGRATION.md")
    ws = add_sheet("Указатель листов", sheet_map.copy())
    for cell in list(ws.columns)[0][1:]:
        cell.hyperlink = "#'" + cell.value.replace("'", "''") + "'!A1"
        cell.font = Font(color="176C65", underline="single")
    path = root / "exports/quran_analysis.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return {"path": str(path.relative_to(root)), "sheets": len(wb.sheetnames), "external_tables": len(skipped)}


def _report(root, data, findings, coverage, summaries, figures, inventory):
    by = {(r["scope"], r["profile_id"]): r for r in data["metrics"]}
    f, n = by[("file", "plain")], by[("numbered", "plain")]
    core = summaries.get("core", {})
    text = ["# Исследование арабского текста Корана", "", "Откройте [интерактивный отчёт](../dashboard/index.html): он содержит весь индекс орфографических токенов и работает локально без сервера и интернета. [XLSX](../exports/quran_analysis.xlsx) предназначен для изучения компактных таблиц; полные таблицы и индексы перечислены ниже.", "", "## Паспорт корпуса", "", f"В данном файле рассчитано {f['surahs']:,} сур, {f['verses']:,} нумерованных аятов и {f['tokens']:,} орфографических токенов. Область `numbered` содержит {n['tokens']:,} токенов: исключены {f['tokens']-n['tokens']:,} токенов начальных басмал-префиксов. Басмалы 1:1 и внутри 27:30 сохранены. Эти количества описывают данный файл и выбранные правила, а не универсальные количества для всех редакций.", "", "| Профиль | Токены file | Словоформы file | Токены numbered | Словоформы numbered |", "|---|---:|---:|---:|---:|"]
    for profile in PROFILE_NAMES:
        a, b = by[("file", profile)], by[("numbered", profile)]
        text.append(f"| `{profile}` | {a['tokens']} | {a['types']} | {b['tokens']} | {b['types']} |")
    text += ["", "Источник таблицы: [presentation_corpus_metrics.csv](../results/tables/presentation_corpus_metrics.csv). Письменная словоформа не равна лемме или корню.", "", f"SHA-256 исходника: `{core.get('sha256', '')}`. {core.get('completeness_ru', '')} Происхождение: {core.get('provenance_ru', 'не установлено')}", "", "## Главное, что установлено", ""]
    morph, stats = summaries.get("morphology", {}), summaries.get("statistics", {})
    if morph:
        text += [f"- **Морфология имеет измеренное неполное покрытие:** надёжно связаны {morph.get('verified_tokens', 0):,} из {morph.get('source_tokens_numbered', 0):,} токенов numbered ({100*morph.get('token_coverage', 0):.2f}%). Частоты лемм и корней относятся к этой выровненной части; токены группового соответствия считаются связанными с группой, не обязательно с точным сегментом."]
    if stats:
        text += [f"- **Статистическая структура зависит от модели:** {stats.get('significant_under_model', 0)} из {stats.get('primary_test_count', 0)} первичных тестов отвергают выбранные перестановочные модели после общей поправки Holm при α={stats.get('alpha', .05)}; на тест выполнено {stats.get('simulations_per_test', 0)} контрольных повторов. Это совместимо с обычной языковой, грамматической и риторической структурой. Независимого подтверждающего корпуса нет."]
    for finding in findings:
        if finding.get("hypothesis_id") == "EXT-universality-basmala":
            text += ["- **Чувствительность к басмале:** формы الله и الرحيم профиля plain встречаются во всех сурах в области file; после исключения начальных басмал-префиксов в numbered ни одна форма не охватывает все суры."]
        elif finding.get("hypothesis_id") == "J-month-12":
            observed = finding.get("observed", {})
            text += [f"- **Опубликованный счёт месяца:** при указанном морфологическом правиле получено {observed.get('verified_included')} проверенно связанных вхождений, непроверенных кандидатов — {observed.get('unverified_included')}. Статус: {finding['status']}. Это результат заданного правила, не универсальный подсчёт значения «месяц»."]
        elif finding.get("hypothesis_id") == "J-day-365":
            observed = finding.get("observed", {})
            text += [f"- **Опубликованное число 365 не подтверждено полностью на исходном файле:** надёжно связаны {observed.get('verified_included')} включённых кандидата; ещё {observed.get('unverified_included')} требуют проверки выравнивания. Внешний итог не подставляется вместо результата пользовательского корпуса."]
    text += ["", "## Реестр результатов и статусов", "", "Полные карточки с наблюдаемыми значениями, методами, ограничениями, источниками публикаций, эффектами и поправками находятся в [интерактивном каталоге](../dashboard/index.html#findings) и [машиночитаемом реестре](../results/hypotheses/registry.json). Здесь приведён компактный указатель. Пустое p-value не означает нулевую вероятность.", "", "| Результат | Статус | Профиль / область | Полные исходные данные |", "|---|---|---|---|"]
    for finding in findings:
        title = finding.get("title_ru", finding.get("hypothesis_id", "Результат"))
        evidence = finding.get("evidence_path")
        paths = evidence if isinstance(evidence, list) else [evidence] if evidence else []
        sources = "; ".join(f"[{Path(p).name}](../{p})" for p in paths)
        status = {"descriptive": "Описательный результат", "candidate": "Кандидат", "descriptive_verified": "Проверенный описательный факт", "candidate_bounded_search": "Кандидат ограниченного поиска", "descriptive_no_significance": "Описательное совпадение; значимость не заявлена", "descriptive_control_comparison": "Описательное сравнение с контролями"}.get(finding.get("status"), finding.get("status", "не указан"))
        cells = [title, status, f"{finding.get('profile_id', '')} / {finding.get('scope', '')}", sources]
        text.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    text += ["", "## Карта покрытия", "", "Полнота утверждается только в границах указанных конечных семейств. Неограниченное пространство формул и все семантические отношения не перебирались.", "", "| Направление | Анализ | Статус | Проверено / пространство | Ограничения |", "|---|---|---|---|---|"]
    for row in coverage:
        vals = [row.get("direction", ""), row.get("analysis", ""), row.get("status", ""), f"{row.get('tested', '')} / {row.get('universe', '')}", row.get("limitations", "")]
        text.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in vals) + " |")
    text += ["", "## Методика и ограничения", "", "- Единственный первичный корпус — `Quran_text.txt`. Его неизменная копия, хеш и аудит хранятся отдельно. Внешняя морфология обогащает совпавшие позиции и не заменяет входной текст.", "- `raw` — поверхность токена; `nfc` — NFC; `plain` удаляет явно перечисленные огласовки и редакторские знаки; `search` дополнительно объединяет формы алифа и ى/ي. ة сохраняется. Комбинируемые хамза и мадда не удаляются общим правилом Mn.", "- Орфографический токен определяется границами записи. Клитики рассматриваются отдельно только при наличии надёжно выровненного морфологического сегмента. Буквы, диакритические знаки и морфологические сегменты не взаимозаменяемы.", "- `file` и `numbered` имеют разные знаменатели. Глобальные координаты токенов всегда относятся к исходному файлу. `numbered` не перенумеровывает эти исходные адреса.", "- Повторяющийся текст разных аятов сам по себе не является ошибкой корпуса. Частоты, позиционные совпадения и сходство зависят от профиля, длины и выбранной области.", "- Перестановочные модели проверяют ограниченные нулевые гипотезы. Отвержение модели не доказывает умысел, уникальность текста или сверхъестественную причину; подобные выводы данным анализом не устанавливаются.", "- Нормализация письменной поверхности не даёт достоверного значения слова. Семантические и тематические связи без внешней разметки не считаются исчерпывающе покрытыми.", "- Dashboard содержит полный поиск орфографических форм и полные связанные вхождения доступных лемм/корней с пагинацией. Предварительные таблицы прочих модулей помечены как первые строки; каждый полный источник доступен отдельно.", "- Все величины отчёта, XLSX и графиков берутся из рассчитанных таблиц или одного снимка SQLite. Числа не подбирались под внешние распространённые количества.", "", "Подробные спецификации, аудит и источники перечислены в [документации](../docs/INTEGRATION.md) и во вкладке «Методика и файлы» dashboard. Переносимый HTML использует встроенный JSON и системные арабские шрифты: внешний сервер и Google-аккаунт не требуются.", "", "## Графики и данные", ""]
    for figure in figures:
        text += [f"### {figure['title_ru']}", "", f"![{figure['title_ru']}](../{figure['png']})", "", figure["caption_ru"].replace("\n", " "), "", f"[SVG](../{figure['svg']}) · [PNG](../{figure['png']}) · [исходные числа](../{figure['data']})", ""]
    text += ["## Файлы результатов", "", "| Файл | Формат | Байт |", "|---|---|---:|"]
    for item in inventory:
        text.append(f"| [{item['path']}](../{item['path']}) | {item['format']} | {item['bytes']} |")
    text += ["", "Порядок запуска, версии и команда возобновления находятся в [README](../README.md) и [PROJECT_STATE](../PROJECT_STATE.md). Реестр исполнения — `run_manifest.json`. Проверки и их ограничения фиксируются в документах валидации, когда этап валидации завершён.", ""]
    path = root / "report/REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(text), encoding="utf-8")


def run(root: Path, config: dict) -> dict:
    root = Path(root).resolve()
    from .real_world import run as build_real_world
    build_real_world(root)
    (root / "results/tables").mkdir(parents=True, exist_ok=True)
    data, token_index, ranks = _corpus_data(root)
    _write_csv(root / "results/tables/presentation_corpus_metrics.csv", data["metrics"])
    _write_csv(root / "results/tables/presentation_surah_metrics.csv", data["surahs"])
    _write_csv(root / "results/tables/presentation_verse_lengths.csv", data["lengths"])
    _write_csv(root / "results/tables/presentation_rank_frequency.csv", ranks)
    findings, coverage, summaries = [], [], {}
    for path in sorted((root / "results/hypotheses").glob("*.json")):
        if path.name == "registry.json":
            continue
        values = _read_json(path, [])
        if isinstance(values, dict):
            values = values.get("hypotheses", values.get("findings", []))
        if isinstance(values, list):
            findings.extend(values)
    for path in sorted((root / "results").glob("*_coverage.json")):
        values = _read_json(path, [])
        if isinstance(values, list):
            coverage.extend(values)
    for path in sorted((root / "results").glob("*_summary.json")):
        if path.stem != "presentation_summary":
            summaries[path.stem.removesuffix("_summary")] = _read_json(path, {})
    data.update({"findings": findings, "coverage": coverage, "summaries": summaries, "morphology": _morphology(root, token_index),
                 "real_world": _read_json(root / "results/real_world.json", {})})
    previews = []
    for path in sorted((root / "results/tables").glob("*.csv*")):
        if path.name.startswith("presentation_"):
            continue
        rows = _rows(path, 30)
        previews.append({"name": path.name.removesuffix(".gz").removesuffix(".csv"), "path": str(path.relative_to(root)), "rows": rows, "limit": 30})
    data["previews"] = previews
    data["figures"] = _figures(root, data, ranks)
    data["files"] = _inventory(root)
    workbook = _spreadsheet(root, data, findings, coverage, summaries, data["files"])
    _report(root, data, findings, coverage, summaries, data["figures"], data["files"])
    template = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    dashboard = root / "dashboard/index.html"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(template.replace("__CORPUS_DATA__", payload), encoding="utf-8")
    result = {"dashboard": "dashboard/index.html", "dashboard_bytes": dashboard.stat().st_size, "report": "report/REPORT.md", "workbook": workbook, "figures": [{k: v for k, v in f.items() if k != "image_uri"} for f in data["figures"]], "tokens_embedded": len(data["tokens"]), "verses_embedded": len(data["verses"]), "findings": len(findings), "coverage_records": len(coverage), "morphology_units": {k: len(v) for k, v in data["morphology"]["units"].items()}, "limitations": ["XLSX не дублирует CSV.gz и таблицы более 20 000 строк / 3 МБ; полные файлы перечислены явно.", "HTML автономен; ссылки на отдельные файлы требуют сохранённой структуры проекта.", "Визуальная проверка выполняется отдельным этапом и не подразумевается самим экспортом."]}
    (root / "results/presentation_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
