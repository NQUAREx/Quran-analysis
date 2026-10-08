"""Finite permutation tests with auditable complete null draws (Russian reporting)."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sqlite3
import time
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix


def _json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _csv(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def holm(p):
    """Holm FWER adjustment; valid for arbitrarily dependent tests."""
    p = np.asarray(p, dtype=float)
    order = np.argsort(p, kind="stable")
    adjusted = np.maximum.accumulate((len(p) - np.arange(len(p))) * p[order])
    out = np.empty(len(p))
    out[order] = np.minimum(adjusted, 1)
    return out


def monte_carlo_p(observed, draws, tail="greater"):
    draws = np.asarray(draws)
    extreme = draws <= observed if tail == "less" else draws >= observed
    return (int(extreme.sum()) + 1) / (len(draws) + 1)


def _seed(base, key):
    return (base + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)) % (2**32)


def _letters(text):
    return "".join(c for c in text if unicodedata.category(c).startswith("L") and c != "ـ")


def _row(hid, title, profile, scope, model, observed, draws, seed, n, method, tail="greater"):
    varying = bool(np.ptp(draws) > 1e-12)
    return {
        "hypothesis_id": hid, "family": "primary_permutation", "title_ru": title,
        "profile_id": profile, "scope": scope, "null_model": model,
        "observed": float(observed), "null_mean": float(np.mean(draws)),
        "effect": float(observed - np.mean(draws)),
        "null_q025": float(np.quantile(draws, .025)),
        "null_q975": float(np.quantile(draws, .975)),
        "p_value": monte_carlo_p(observed, draws, tail) if varying else 1.0,
        "p_adjusted": None, "correction": "Holm-FWER (все первичные тесты)",
        "simulations": len(draws), "seed": seed, "n_units": int(n),
        "status": "ожидает поправки" if varying else "Нулевая модель не меняет статистику",
        "null_varies": varying, "tail": tail,
        "registration": "План до расчёта; независимой контрольной выборки нет",
        "method_ru": method,
        "limitation_ru": "Вывод относится к конкретной перестановочной модели; грамматика и риторика могут объяснять отличие. Не доказывает уникальность, намеренность или происхождение текста.",
        "evidence_path": "results/tables/statistical_null_draws.csv.gz",
    }


def run(root: Path, config: dict) -> dict:
    started = time.perf_counter()
    sc = config.get("statistics", {})
    B, base_seed = sc.get("simulations", 999), config.get("seed", 20261007)
    block_n = sc.get("block_verses", 5)
    suffix_ns = sc.get("suffix_lengths", [2, 3])
    conn = sqlite3.connect(root / "data/processed/corpus.sqlite")
    conn.row_factory = sqlite3.Row
    tokens = [dict(r) for r in conn.execute("SELECT * FROM tokens ORDER BY global_token")]
    verses = [dict(r) for r in conn.execute("SELECT * FROM verses ORDER BY global_ayah")]
    conn.close()
    tests, null_rows, lexical_rows = [], [], []
    selected_forms = []
    verse_map = {v["verse_id"]: i for i, v in enumerate(verses)}
    surah_ids = np.array([v["surah_id"] for v in verses])
    surah_groups = [np.flatnonzero(surah_ids == s) for s in np.unique(surah_ids)]
    left = np.flatnonzero(surah_ids[1:] == surah_ids[:-1])
    right = left + 1
    V = len(verses)
    alpha = sc.get("alpha", .05)
    for scope in ["file", "numbered"]:
        scope_tokens = [t for t in tokens if scope == "file" or not t["is_opening_basmala"]]
        vi = np.array([verse_map[t["verse_id"]] for t in scope_tokens], dtype=np.int32)
        lengths = np.bincount(vi, minlength=V).astype(float)
        starts = np.r_[0, np.cumsum(lengths.astype(int))[:-1]]
        # relative positions are recomputed after basmala removal.
        token_position = np.arange(len(vi)) - starts[vi]
        bins = np.minimum(2, np.floor(3 * (token_position + .5) / lengths[vi]).astype(int))
        endwords = [""] * V
        for t in scope_tokens:
            endwords[verse_map[t["verse_id"]]] = _letters(t["plain"])
        suffixes = [np.asarray([w[-n:] for w in endwords]) for n in suffix_ns]
        x = np.zeros(V)
        y = lengths.copy()
        for group in surah_groups:
            x[group] = (np.arange(len(group)) + .5) / len(group) - .5
            y[group] -= y[group].mean()
        denom = np.sqrt(np.sum(x*x) * np.sum(y*y))

        def metrics(order):
            l = lengths[order]
            res = [float(np.abs(l[left] - l[right]).mean()),
                   float(abs(np.dot(x, y[order]) / denom)) if denom else 0.0]
            res.extend(float((a[order[left]] == a[order[right]]).mean()) for a in suffixes)
            return np.array(res)

        observed = metrics(np.arange(V))
        for model in ["verse_shuffle_within_surah", f"block_{block_n}_shuffle_within_surah"]:
            sid = _seed(base_seed, scope + model)
            rng = np.random.default_rng(sid)
            draws = np.empty((B, 2 + len(suffix_ns)))
            blocks = [[g[i:i+block_n] for i in range(0, len(g), block_n)] for g in surah_groups]
            for b in range(B):
                order = np.concatenate([rng.permutation(g) for g in surah_groups]) if model.startswith("verse") else np.concatenate([np.concatenate([bg[i] for i in rng.permutation(len(bg))]) for bg in blocks])
                draws[b] = metrics(order)
            specs = [("length_adjacency", "Соседние аяты близки по числу токенов", "less"),
                     ("length_position", "Длина аята связана с положением внутри суры", "greater")]
            specs += [(f"ending_{n}", f"Соседние аяты имеют одинаковые последние {n} письменные буквы", "greater") for n in suffix_ns]
            for j, (key, title, tail) in enumerate(specs):
                hid = f"STAT-{key}-{scope}-{model}"
                row = _row(hid, title, "plain", scope, model, observed[j], draws[:,j], sid, len(left),
                           "Перестановка целых аятов внутри суры; сохраняет их тексты, длины, частоты и границы сур. Блоковый вариант сохраняет порядок внутри последовательных блоков из 5 аятов. См. docs/STATISTICAL_PLAN.md.", tail)
                if key == "length_position":
                    row["n_units"] = V
                tests.append(row)
                null_rows.extend({"hypothesis_id": hid, "simulation": b+1, "value": float(v)} for b,v in enumerate(draws[:,j]))

        for profile in sc.get("lexical_profiles", ["nfc", "plain"]):
            counts = Counter(t[profile] for t in scope_tokens)
            selected = sorted((w for w,n in counts.items() if n >= sc.get("lexical_min_count", 100)), key=lambda w:(-counts[w],w))[:sc.get("lexical_top_n",60)]
            K = len(selected)
            if not K:
                continue
            lookup = {w:i for i,w in enumerate(selected)}
            codes = np.array([lookup.get(t[profile],K) for t in scope_tokens])
            valid = codes < K
            verse_word = coo_matrix((np.ones(valid.sum()), (vi[valid], codes[valid])), shape=(V,K)).tocsr()
            capacity = np.bincount(vi*3+bins,minlength=V*3).reshape(V,3)
            fractions = np.divide(capacity, lengths[:,None], out=np.zeros((V,3)), where=lengths[:,None]>0)
            expected = np.asarray(verse_word.T @ fractions)

            def lexical_stat(c):
                observed_table = np.bincount(c*3+bins, minlength=(K+1)*3).reshape(K+1,3)[:K]
                chi = np.sum(np.divide((observed_table-expected)**2, expected, out=np.zeros((K,3)), where=expected>0),axis=1)
                return float(chi.max()), observed_table, chi

            obs, obs_table, chi = lexical_stat(codes)
            for k,w in enumerate(selected):
                selected_forms.append({"profile_id":profile,"scope":scope,"form":w,"frequency":counts[w],"max_statistic_component":float(chi[k])})
                for p in range(3):
                    lexical_rows.append({"profile_id":profile,"scope":scope,"form":w,"third":p+1,"observed":int(obs_table[k,p]),"expected":float(expected[k,p]),"capacity_tokens":int(capacity[:,p].sum()),"pearson_component_sum":float(chi[k])})
            for model in ["token_shuffle_within_verse", "cyclic_shift_within_verse"]:
                sid = _seed(base_seed, scope+profile+model)
                rng = np.random.default_rng(sid)
                draws = np.empty(B)
                for b in range(B):
                    if model.startswith("token"):
                        order = np.lexsort((rng.random(len(codes)), vi))
                    else:
                        shift = np.floor(rng.random(V)*lengths).astype(int)
                        order = starts[vi]+((token_position+shift[vi])%lengths[vi].astype(int))
                    draws[b] = lexical_stat(codes[order])[0]
                hid = f"STAT-lexical_position-{profile}-{scope}-{model}"
                row = _row(hid, "Частые словоформы неравномерно распределены внутри аята", profile, scope, model, obs, draws, sid, len(codes),
                           f"Максимум Pearson-подобного отклонения среди {K} форм. Ожидание учитывает число доступных мест каждой трети и состав каждого аята; весь поиск максимума повторён в каждом контроле. Выбор форм основан на инвариантных к перестановке общих частотах.")
                row["method_ru"] += f" Максимум наблюдается у формы {selected[int(np.argmax(chi))]}."
                tests.append(row)
                null_rows.extend({"hypothesis_id":hid,"simulation":b+1,"value":float(v)} for b,v in enumerate(draws))
        print(f"Статистика: scope={scope}, завершено {len(tests)} тестов", flush=True)

    adjusted = holm([t["p_value"] for t in tests])
    for t,p in zip(tests,adjusted):
        t["p_adjusted"] = float(p)
        t["family_test_count"] = len(tests)
        if t["null_varies"]:
            t["status"] = "Необычно относительно указанной модели после поправок" if p <= alpha else "Не выделяется на фоне контрольных данных"
    table_dir = root / "results/tables"
    _csv(table_dir / "statistical_tests.csv", tests)
    _csv(table_dir / "statistical_null_draws.csv.gz", null_rows)
    _csv(table_dir / "statistical_lexical_positions.csv", lexical_rows)
    _csv(table_dir / "statistical_selected_forms.csv", selected_forms)
    _json(root / "results/hypotheses/statistics.json", tests)
    summary = {"primary_test_count":len(tests),"simulations_per_test":B,"seed":base_seed,
               "correction":"Holm FWER over all primary tests", "alpha":alpha,
               "significant_under_model":sum(t["p_adjusted"]<=alpha and t["null_varies"] for t in tests),
               "strongest_test":min(tests,key=lambda t:t["p_adjusted"]) if tests else None,
               "tests":tests,"elapsed_seconds":round(time.perf_counter()-started,3),
               "limitations_ru":"Одна наблюдаемая книга; модели разрушают часть языковой структуры. Поправка относится к заявленным тестам, не ко всем мыслимым гипотезам."}
    _json(root / "results/statistics_summary.json", summary)
    _json(root / "results/statistics_coverage.json", [
        {"direction":"D","analysis":"Внутриаятная локализация частых форм; повтор поиска максимума в контролях","status":"complete_bounded","universe":"2 профиля × 2 scope × 2 нулевые модели; top-60 при count≥100","tested":8,"limitations":"Грамматика естественного языка не сохраняется полностью"},
        {"direction":"E","analysis":"Длины, порядок и последние 2/3 письменные буквы","status":"complete_bounded","universe":"4 метрики × 2 scope × 2 нулевые модели","tested":16,"limitations":"Не анализ произношения или смысловой кольцевой композиции"}
    ])
    return summary
