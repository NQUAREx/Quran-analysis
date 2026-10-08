# Контракт модулей v1

Вход — только Quran_text.txt. Корень проекта определяется Path, не cwd.
Основная БД data/processed/corpus.sqlite; таблицы verses и tokens.
Все номера 1-based; raw_start/raw_end — 0-based half-open смещения Unicode в полном исходном файле (декодированном UTF-8, без изменения переводов строк).

verses: verse_id TEXT ('2:255'), surah_id INTEGER, ayah_id INTEGER, global_ayah INTEGER, raw_text TEXT, nfc TEXT, plain TEXT, search TEXT, raw_start INTEGER, raw_end INTEGER, token_count INTEGER, letter_count INTEGER, mark_count INTEGER.

tokens: token_id TEXT ('2:255:1'), verse_id TEXT, surah_id INTEGER, ayah_id INTEGER, global_ayah INTEGER, global_token INTEGER, token_in_ayah INTEGER, token_in_surah INTEGER, raw TEXT, nfc TEXT, plain TEXT, search TEXT, raw_start INTEGER, raw_end INTEGER, letter_count INTEGER, mark_count INTEGER, is_opening_basmala INTEGER (1 только первые четыре токена префикса аятов x:1 при x != 1,9).

Профили: raw — точная поверхность токена; nfc — NFC; plain — NFC с удалением U+064B..U+0652, U+0670, U+0640 и редакторских U+06D6..U+06DC,U+06DE,U+06E9. Хамза/мадда U+0653..U+0655 сохраняются. search — plain с أإآٱ→ا, ى→ي; ة не меняется. Никакого NFKC. Это профили письменной поверхности, не леммы.

scope=file — все токены файла; scope=numbered — исключены только is_opening_basmala. Позиции глобальных токенов относятся к file; numbered при необходимости получает отдельный индекс и не переиспользует file как будто он неизменный.

Все модули экспортируют run(root: Path, config: dict) -> dict. Модули core.py, exploration.py, morphology.py, statistics.py, presentation.py. Корень вызывает последовательно. Каждый модуль владеет своими файлами; общие таблицы SQLite после core только для чтения. Выход дополнительных модулей — CSV (UTF-8), JSON; большие таблицы CSV.gz, sqlite отдельные. Числа не переписываются вручную в отчёт.

Каждый модуль пишет results/<module>_summary.json и results/tables/<prefix>*.csv[.gz]. Findings: results/hypotheses/<module>.json — список объектов с hypothesis_id, family, title_ru, status, profile_id, scope, observed, method_ru, limitation_ru, evidence_path; дополнительные поля допустимы. Coverage: results/<module>_coverage.json — список объектов с direction (A–J), analysis, status, universe, tested, limitations.

Не объявлять утверждения о значимости по отобранным красивым примерам. Описательные результаты имеют пустые p_value. Все интерпретации и подписи по-русски.
