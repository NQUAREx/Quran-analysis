"""Конечные описательные поиски C–H; исходная SQLite открывается только для чтения."""
from __future__ import annotations

import bisect
import csv
import difflib
import gzip
import itertools
import json
import math
import random
import sqlite3
import time
import unicodedata
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


DEFAULTS = {
    "word_ngram_lengths": [2, 3, 4, 5],
    "letter_ngram_lengths": [2, 3, 4],
    "long_repeat_seed_length": 5,
    "long_repeat_seed_max_support": 40,
    "long_repeat_min_length": 8,
    "approximate_seed_length": 3,
    "approximate_seed_max_verse_support": 25,
    "approximate_min_verse_tokens": 5,
    "approximate_jaccard_alignment_threshold": 0.4,
    "cooccurrence_top_vocabulary": 120,
    "cooccurrence_min_support": 5,
    "cooccurrence_window_tokens": 50,
    "diversity_window_tokens": 500,
    "position_window_tokens": 1000,
    "position_window_step": 250,
    "compression_controls": 19,
    "numeric_divisors": [2, 3, 5, 7, 11, 19],
    "approximate_similarity_min": 0.8,
    "max_formula_depth": 1,
}


def resolve_config(config):
    """Translate the public configuration once; never silently ignore a bound."""
    supplied = config.get("exploration", {})
    aliases = {"cooccurrence_top_n": "cooccurrence_top_vocabulary",
               "long_repeat_seed": "long_repeat_seed_length",
               "divisors": "numeric_divisors", "window_tokens": "diversity_window_tokens"}
    range_keys = {f"{kind}_ngram_{end}" for kind in ("word", "letter") for end in ("min", "max")}
    unknown = set(supplied) - set(DEFAULTS) - set(aliases) - range_keys
    if unknown:
        raise ValueError(f"Unknown exploration configuration: {sorted(unknown)}")
    cfg = dict(DEFAULTS)
    cfg.update({aliases.get(k, k): v for k, v in supplied.items() if k not in range_keys})
    for alias, target in aliases.items():
        if alias in supplied and target in supplied and supplied[alias] != supplied[target]:
            raise ValueError(f"Conflicting values for {alias} and {target}")
    for kind in ("word", "letter"):
        key = f"{kind}_ngram_lengths"
        if any(f"{kind}_ngram_{end}" in supplied for end in ("min", "max")):
            lengths = list(range(supplied.get(f"{kind}_ngram_min", min(cfg[key])),
                                 supplied.get(f"{kind}_ngram_max", max(cfg[key])) + 1))
            if key in supplied and lengths != supplied[key]:
                raise ValueError(f"Conflicting bounds for {kind} n-grams")
            cfg[key] = lengths
        if not cfg[key] or any(not isinstance(n, int) or n < 1 for n in cfg[key]):
            raise ValueError(f"Invalid {key}")
        cfg[key] = sorted(set(cfg[key]))
    for key in ("long_repeat_seed_length", "long_repeat_seed_max_support", "long_repeat_min_length",
                "approximate_seed_length", "approximate_seed_max_verse_support", "approximate_min_verse_tokens",
                "cooccurrence_top_vocabulary", "cooccurrence_min_support", "cooccurrence_window_tokens",
                "diversity_window_tokens", "position_window_tokens", "position_window_step", "compression_controls"):
        if not isinstance(cfg[key], int) or cfg[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("approximate_jaccard_alignment_threshold", "approximate_similarity_min"):
        if not 0 <= cfg[key] <= 1:
            raise ValueError(f"{key} must be in [0, 1]")
    if cfg["max_formula_depth"] != 1:
        raise ValueError("Only the registered formula depth 1 is implemented")
    if cfg["long_repeat_min_length"] < cfg["long_repeat_seed_length"]:
        raise ValueError("long_repeat_min_length must be at least long_repeat_seed_length")
    if not cfg["numeric_divisors"] or any(not isinstance(n, int) or n < 2 for n in cfg["numeric_divisors"]):
        raise ValueError("numeric_divisors must be integers >= 2")
    cfg["numeric_divisors"] = sorted(set(cfg["numeric_divisors"]))
    return cfg


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path: Path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    count = 0
    with opener(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def iter_ngrams(sequence, n):
    """Все перекрывающиеся n-граммы одной заранее ограниченной единицы."""
    for start in range(max(0, len(sequence) - n + 1)):
        yield start, tuple(sequence[start:start + n])


def nonoverlapping_count(hits, length):
    """Greedy maximum number of equal-length intervals within each source unit."""
    count, previous_unit, next_position = 0, None, 0
    for unit, position in sorted(hits):
        if unit != previous_unit or position >= next_position:
            count += 1
            previous_unit, next_position = unit, position + length
    return count


def entropy(counter):
    n = sum(counter.values())
    return -sum((v / n) * math.log2(v / n) for v in counter.values()) if n else 0.0


def is_prime(n):
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    return all(n % divisor for divisor in range(3, math.isqrt(n) + 1, 2))


def base_letters(text):
    return [(index, char) for index, char in enumerate(text)
            if unicodedata.category(char).startswith("L") and char != "ـ"]


def pair_extension(sequences, occurrence_a, occurrence_b, seed_length):
    """Максимальное точное расширение пары внутри исходных аятов."""
    verse_a, start_a = occurrence_a
    verse_b, start_b = occurrence_b
    seq_a, seq_b = sequences[verse_a], sequences[verse_b]
    while start_a and start_b and seq_a[start_a - 1] == seq_b[start_b - 1]:
        start_a -= 1
        start_b -= 1
        seed_length += 1
    while (start_a + seed_length < len(seq_a)
           and start_b + seed_length < len(seq_b)
           and seq_a[start_a + seed_length] == seq_b[start_b + seed_length]):
        seed_length += 1
    return verse_a, start_a, verse_b, start_b, seed_length


def window_starts(total, width, step):
    """Full windows plus a final full window ending at the corpus boundary."""
    if total <= width:
        return [1]
    starts = list(range(1, total - width + 2, step))
    if starts[-1] != total - width + 1:
        starts.append(total - width + 1)
    return starts


def cooccurrence_rows(context_name, contexts, vocabulary, minimum_support):
    """Полное верхнее треугольное множество пар, включая нулевые связи."""
    index = {word: i for i, word in enumerate(vocabulary)}
    matrix = np.zeros((len(contexts), len(vocabulary)), dtype=np.int64)
    ids = []
    for row, (context_id, words) in enumerate(contexts):
        ids.append(context_id)
        for word in set(words):
            if word in index:
                matrix[row, index[word]] = 1
    counts = matrix.sum(axis=0)
    joint = matrix.T @ matrix
    total = len(contexts)
    for a, b in itertools.combinations(range(len(vocabulary)), 2):
        ca, cb, cab = int(counts[a]), int(counts[b]), int(joint[a, b])
        lift = (cab * total / (ca * cb)) if ca and cb else None
        examples = [ids[i] for i in np.flatnonzero(matrix[:, a] & matrix[:, b])[:5]]
        yield {"context": context_name, "word_a": vocabulary[a], "word_b": vocabulary[b],
               "n_contexts": total, "count_a": ca, "count_b": cb, "count_ab": cab,
               "pmi_bits": math.log2(lift) if lift and cab else "",
               "lift": lift if lift is not None else "", "support_pass": int(cab >= minimum_support),
               "example_context_ids": "|".join(map(str, examples)), "profile_id": "plain", "scope": "file"}


def integer_search(unit_id, values, divisors):
    """Глубина 1: x, x+y; равенство с атомом; остатки и простота атомов."""
    names = sorted(values)
    expressions = [(name, values[name], (name,)) for name in names]
    expressions += [(f"{a}+{b}", values[a] + values[b], (a, b))
                    for a, b in itertools.combinations_with_replacement(names, 2)]
    for expression, lhs, atoms in expressions:
        for target in names:
            if len(atoms) == 1 and atoms[0] >= target:
                continue  # саморавенства и обратные копии атомарных равенств исключены заранее
            yield {"unit_id": unit_id, "family": "equality", "expression": expression,
                   "comparison": target, "lhs": lhs, "rhs": values[target],
                   "result": int(lhs == values[target]), "residue": ""}
    for name in names:
        for divisor in divisors:
            yield {"unit_id": unit_id, "family": "divisibility", "expression": name,
                   "comparison": f"mod {divisor} = 0", "lhs": values[name], "rhs": divisor,
                   "result": int(values[name] % divisor == 0), "residue": values[name] % divisor}
        yield {"unit_id": unit_id, "family": "primality", "expression": name,
               "comparison": "prime", "lhs": values[name], "rhs": "",
               "result": int(is_prime(values[name])), "residue": ""}


def run(root: Path, config: dict) -> dict:
    root = Path(root)
    start_time = time.monotonic()
    cfg = resolve_config(config)
    seed = int(config.get("seed", config.get("random_seed", 1729)))
    tables = root / "results/tables"
    tables.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{root / 'data/processed/corpus.sqlite'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    verses = [dict(row) for row in connection.execute("SELECT * FROM verses ORDER BY global_ayah")]
    tokens = [dict(row) for row in connection.execute("SELECT * FROM tokens ORDER BY global_token")]
    connection.close()
    if not verses or not tokens:
        raise ValueError("The exploration module requires a nonempty corpus")
    by_verse = defaultdict(list)
    by_surah = defaultdict(list)
    for token in tokens:
        by_verse[token["verse_id"]].append(token)
        by_surah[token["surah_id"]].append(token)
    sequences = [[t["plain"] for t in by_verse[v["verse_id"]]] for v in verses]
    flat = [t["plain"] for t in tokens]
    frequencies = Counter(flat)
    paths, coverage, findings = {}, [], []

    def save(name, fields, rows, compressed=False):
        path = tables / f"exploration_{name}.csv{'.gz' if compressed else ''}"
        count = write_csv(path, fields, rows)
        paths[name] = {"path": str(path.relative_to(root)), "rows": count}
        return count

    # Полный словесный индекс и все координаты; ни одна граница аята не пересекается.
    word_groups = {}
    seeds_long, seeds_approx = defaultdict(list), defaultdict(set)
    occurrence_path = tables / "exploration_word_ngram_occurrences.csv.gz"
    fields = ["ngram_id", "n", "verse_id", "surah_id", "start_token_id", "end_token_id", "global_token", "profile_id", "scope"]
    with gzip.open(occurrence_path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        occurrence_count = 0
        for vi, verse in enumerate(verses):
            vt = by_verse[verse["verse_id"]]
            for pos, gram in iter_ngrams(sequences[vi], cfg["long_repeat_seed_length"]):
                seeds_long[gram].append((vi, pos))
            if len(vt) >= cfg["approximate_min_verse_tokens"]:
                for pos, gram in iter_ngrams(sequences[vi], cfg["approximate_seed_length"]):
                    seeds_approx[gram].add(vi)
            for n in cfg["word_ngram_lengths"]:
                for pos, gram in iter_ngrams(sequences[vi], n):
                    key = (n, gram)
                    if key not in word_groups:
                        word_groups[key] = {"ngram_id": f"w{len(word_groups) + 1}", "frequency": 0,
                                            "verses": set(), "surahs": set(),
                                            "nonoverlapping_frequency": 0, "accepted_unit": -1, "accepted_end": 0,
                                            "first_token_id": vt[pos]["token_id"]}
                    group = word_groups[key]
                    group["frequency"] += 1
                    if group["accepted_unit"] != vi or pos >= group["accepted_end"]:
                        group["nonoverlapping_frequency"] += 1
                        group["accepted_unit"], group["accepted_end"] = vi, pos + n
                    group["verses"].add(verse["verse_id"])
                    group["surahs"].add(verse["surah_id"])
                    group["last_token_id"] = vt[pos]["token_id"]
                    writer.writerow({"ngram_id": group["ngram_id"], "n": n, "verse_id": verse["verse_id"],
                                     "surah_id": verse["surah_id"], "start_token_id": vt[pos]["token_id"],
                                     "end_token_id": vt[pos + n - 1]["token_id"], "global_token": vt[pos]["global_token"],
                                     "profile_id": "plain", "scope": "file"})
                    occurrence_count += 1
    paths["word_ngram_occurrences"] = {"path": str(occurrence_path.relative_to(root)), "rows": occurrence_count}
    save("word_ngrams", ["ngram_id", "n", "sequence", "frequency", "nonoverlapping_frequency", "verse_count", "surah_count", "first_token_id", "last_token_id", "profile_id", "scope"],
         ({"ngram_id": g["ngram_id"], "n": n, "sequence": " ".join(gram), "frequency": g["frequency"],
           "nonoverlapping_frequency": g["nonoverlapping_frequency"],
           "verse_count": len(g["verses"]), "surah_count": len(g["surahs"]), "first_token_id": g["first_token_id"],
           "last_token_id": g["last_token_id"], "profile_id": "plain", "scope": "file"}
          for (n, gram), g in word_groups.items()), True)
    top_word_ngrams = sorted((dict(sequence=" ".join(gram), n=n, frequency=g["frequency"], first_token_id=g["first_token_id"])
                             for (n, gram), g in word_groups.items()), key=lambda r: (-r["frequency"], -r["n"], r["sequence"]))[:20]
    del word_groups
    coverage.append(dict(direction="C", analysis="Словесные n-граммы", status="complete_within_scope",
                         universe={"profile": "plain", "scope": "file", "n": cfg["word_ngram_lengths"], "cross_verse": False, "overlap": True, "nonoverlap": "greedy earliest-start per verse, separately per n-gram"},
                         tested=occurrence_count, limitations="Полный перебор в объявленном диапазоне; другие профили и сквозные n-граммы не вычислялись."))

    # Буквенные n-граммы строго внутри токена: знаки не являются письменными буквами.
    letter_groups = {}
    letter_path = tables / "exploration_letter_ngram_occurrences.csv.gz"
    letter_count = 0
    with gzip.open(letter_path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ngram_id", "n", "token_id", "letter_start_1based", "plain_start_0based", "plain_end_0based", "profile_id", "scope"])
        writer.writeheader()
        for token in tokens:
            letters = base_letters(token["plain"])
            for n in cfg["letter_ngram_lengths"]:
                for pos, gram in iter_ngrams([x[1] for x in letters], n):
                    key = (n, gram)
                    if key not in letter_groups:
                        letter_groups[key] = [f"l{len(letter_groups) + 1}", 0, token["token_id"], token["token_id"], 0, None, 0]
                    group = letter_groups[key]
                    group[1] += 1
                    group[3] = token["token_id"]
                    if group[5] != token["token_id"] or pos >= group[6]:
                        group[4] += 1
                        group[5], group[6] = token["token_id"], pos + n
                    writer.writerow(dict(ngram_id=group[0], n=n, token_id=token["token_id"], letter_start_1based=pos + 1,
                                         plain_start_0based=letters[pos][0], plain_end_0based=letters[pos + n - 1][0] + 1,
                                         profile_id="plain_base_letters", scope="file"))
                    letter_count += 1
    paths["letter_ngram_occurrences"] = {"path": str(letter_path.relative_to(root)), "rows": letter_count}
    save("letter_ngrams", ["ngram_id", "n", "sequence", "frequency", "nonoverlapping_frequency", "first_token_id", "last_token_id", "profile_id", "scope"],
         (dict(ngram_id=g[0], n=n, sequence="".join(gram), frequency=g[1], first_token_id=g[2], last_token_id=g[3],
               nonoverlapping_frequency=g[4],
               profile_id="plain_base_letters", scope="file") for (n, gram), g in letter_groups.items()), True)
    coverage.append(dict(direction="C", analysis="Буквенные n-граммы", status="complete_within_scope",
                         universe={"n": cfg["letter_ngram_lengths"], "boundary": "token", "unit": "Unicode Letter", "nonoverlap": "greedy earliest-start per token, separately per n-gram"}, tested=letter_count,
                         limitations="Только письменные буквы plain; комбинируемые знаки пропускаются. Межсловные последовательности не исследованы."))

    repeat_rows = []
    repeat_inventory = {}
    for profile in ["raw", "nfc", "plain", "search"]:
        for scope in ["file", "numbered"]:
            groups = defaultdict(list)
            for verse in verses:
                vt = [t for t in by_verse[verse["verse_id"]] if scope == "file" or not t["is_opening_basmala"]]
                # raw/file — исходная строка; остальные сравнивают профильную последовательность токенов.
                key = verse["raw_text"] if profile == "raw" and scope == "file" else " ".join(t[profile] for t in vt)
                groups[key].append((verse["verse_id"], len(vt)))
            repeated = {text: values for text, values in groups.items() if len(values) > 1}
            repeat_inventory[f"{profile}/{scope}"] = {"repeated_groups": len(repeated), "verses_in_groups": sum(map(len, repeated.values()))}
            for text, values in repeated.items():
                repeat_rows.append(dict(profile_id=profile, scope=scope, sequence=text, frequency=len(values),
                                        verse_ids="|".join(x[0] for x in values), token_count=values[0][1]))
    repeat_rows.sort(key=lambda r: (-r["frequency"], r["profile_id"], r["scope"], r["sequence"]))
    save("exact_verse_repeats", ["profile_id", "scope", "sequence", "frequency", "verse_ids", "token_count"], repeat_rows)
    coverage.append(dict(direction="C", analysis="Полностью повторяющиеся аяты", status="complete_within_scope",
                         universe={"profiles": ["raw", "nfc", "plain", "search"], "scopes": ["file", "numbered"]},
                         tested=len(verses) * 8,
                         limitations="raw/file сравнивает raw_text целиком, включая пробелы и редакторские знаки; остальные варианты сравнивают последовательности токенов профиля. Семантическое равенство не проверяется."))

    long_pairs, seed_pairs = set(), 0
    for gram, occurrences in seeds_long.items():
        if 2 <= len(occurrences) <= cfg["long_repeat_seed_max_support"]:
            for a, b in itertools.combinations(occurrences, 2):
                seed_pairs += 1
                expanded = pair_extension(sequences, a, b, len(gram))
                if expanded[-1] >= cfg["long_repeat_min_length"]:
                    long_pairs.add(expanded)
    long_groups = defaultdict(set)
    for va, sa, vb, sb, length in long_pairs:
        gram = tuple(sequences[va][sa:sa + length])
        long_groups[gram].update([(va, sa), (vb, sb)])
    long_rows = []
    for gram, discovered_hits in long_groups.items():
        # Candidate discovery is bounded, but every occurrence of each discovered phrase is recovered.
        hits = {(v, s) for v, s in seeds_long[gram[:cfg["long_repeat_seed_length"]]]
                if tuple(sequences[v][s:s + len(gram)]) == gram}
        assert discovered_hits <= hits
        ids = [by_verse[verses[v]["verse_id"]][s]["token_id"] for v, s in sorted(hits)]
        ends = [by_verse[verses[v]["verse_id"]][s + len(gram) - 1]["token_id"] for v, s in sorted(hits)]
        long_rows.append(dict(sequence=" ".join(gram), token_length=len(gram), frequency=len(ids), start_token_ids="|".join(ids),
                              nonoverlapping_frequency=nonoverlapping_count(hits, len(gram)),
                              end_token_ids="|".join(ends),
                              profile_id="plain", scope="file"))
    long_rows.sort(key=lambda r: (-r["token_length"], -r["frequency"], r["sequence"]))
    save("long_repeats", ["sequence", "token_length", "frequency", "nonoverlapping_frequency", "start_token_ids", "end_token_ids", "profile_id", "scope"], long_rows)
    # This ledger stores seed-expanded pairs; the group table stores all occurrences of found phrases.
    save("long_repeat_pairs", ["start_token_id_a", "start_token_id_b", "token_length", "global_token_gap"],
         (dict(start_token_id_a=by_verse[verses[va]["verse_id"]][sa]["token_id"],
               start_token_id_b=by_verse[verses[vb]["verse_id"]][sb]["token_id"], token_length=n,
               global_token_gap=by_verse[verses[vb]["verse_id"]][sb]["global_token"] - by_verse[verses[va]["verse_id"]][sa]["global_token"])
          for va, sa, vb, sb, n in sorted(long_pairs)), True)
    coverage.append(dict(direction="C", analysis="Длинные точные повторы", status="partial_bounded_search",
                         universe={"seed_length": cfg["long_repeat_seed_length"], "seed_max_support": cfg["long_repeat_seed_max_support"], "min_tokens": cfg["long_repeat_min_length"]},
                         tested=seed_pairs, limitations="Все пары разрешённых затравок расширены в обе стороны до границ аята. Повторы без затравки допустимой частоты могут быть пропущены. Для каждой найденной фразы frequency и адреса исчерпывающие; long_repeat_pairs содержит только пары, полученные расширением."))
    del seeds_long

    candidates = set()
    for hits in seeds_approx.values():
        if 2 <= len(hits) <= cfg["approximate_seed_max_verse_support"]:
            candidates.update(itertools.combinations(sorted(hits), 2))
    wordsets = [set(sequence) for sequence in sequences]
    approx_top = []
    approximate_counts = Counter()
    def approximate_rows():
        for a, b in sorted(candidates):
            union = len(wordsets[a] | wordsets[b])
            jaccard = len(wordsets[a] & wordsets[b]) / union if union else 0
            ratio, differences = "", ""
            if jaccard >= cfg["approximate_jaccard_alignment_threshold"]:
                approximate_counts["aligned"] += 1
                matcher = difflib.SequenceMatcher(None, sequences[a], sequences[b], autojunk=False)
                ratio = matcher.ratio()
                differences = json.dumps([dict(operation=tag, a=" ".join(sequences[a][i:j]), b=" ".join(sequences[b][k:l]),
                                               a_start_token_1based=i + 1, a_end_exclusive_1based=j + 1,
                                               b_start_token_1based=k + 1, b_end_exclusive_1based=l + 1)
                                          for tag, i, j, k, l in matcher.get_opcodes() if tag != "equal"], ensure_ascii=False)
            else:
                approximate_counts["skipped_below_jaccard"] += 1
            row = dict(verse_a=verses[a]["verse_id"], verse_b=verses[b]["verse_id"], tokens_a=len(sequences[a]), tokens_b=len(sequences[b]),
                       set_jaccard=jaccard, sequence_matcher_ratio=ratio, differences_json=differences, profile_id="plain", scope="file")
            if ratio != "" and cfg["approximate_similarity_min"] <= ratio < 1:
                approx_top.append(row)
                approximate_counts["high_similarity_nonidentical"] += 1
            yield row
    save("approximate_candidates", ["verse_a", "verse_b", "tokens_a", "tokens_b", "set_jaccard", "sequence_matcher_ratio", "differences_json", "profile_id", "scope"], approximate_rows(), True)
    approx_top.sort(key=lambda r: (-r["sequence_matcher_ratio"], -(r["tokens_a"] + r["tokens_b"])))
    save("approximate_high_similarity", ["verse_a", "verse_b", "tokens_a", "tokens_b", "set_jaccard", "sequence_matcher_ratio", "differences_json", "profile_id", "scope"], approx_top)
    coverage.append(dict(direction="C", analysis="Приблизительные совпадения аятов", status="partial_bounded_search",
                         universe={k: v for k, v in cfg.items() if k.startswith("approximate_")}, tested=len(candidates),
                         limitations="Кандидат должен иметь общую затравку допустимой поддержки; Jaccard множества форм для всех кандидатов, SequenceMatcher только при Jaccard≥порога. Это не полный перебор всех пар и не редакционное расстояние. Первый аргумент — более ранний аят; отношение SequenceMatcher может зависеть от порядка аргументов."))
    del candidates, seeds_approx

    # D: полные позиционные характеристики всех plain-форм.
    token_positions, ayah_positions, surah_positions = defaultdict(list), defaultdict(set), defaultdict(set)
    within_positions = defaultdict(lambda: [0, 0, 0])
    within_denominators = [0, 0, 0]
    other_thirds = {unit: defaultdict(lambda: [0, 0, 0]) for unit in ("surah", "corpus")}
    other_denominators = {unit: [0, 0, 0] for unit in other_thirds}
    normalized_sum = defaultdict(lambda: [0.0, 0.0, 0.0])
    surah_start = {s: ts[0]["global_token"] for s, ts in by_surah.items()}
    for token in tokens:
        word = token["plain"]
        token_positions[word].append(token["global_token"])
        ayah_positions[word].add(token["global_ayah"])
        surah_positions[word].add(token["surah_id"])
        length_v = len(by_verse[token["verse_id"]])
        bin_id = min(2, (token["token_in_ayah"] - 1) * 3 // max(1, length_v))
        within_positions[word][bin_id] += 1
        within_denominators[bin_id] += 1
        for unit, position, total in (("surah", token["token_in_surah"], len(by_surah[token["surah_id"]])),
                                      ("corpus", token["global_token"], len(tokens))):
            third = min(2, (position - 1) * 3 // total)
            other_thirds[unit][word][third] += 1
            other_denominators[unit][third] += 1
        normalized_sum[word][0] += (token["token_in_ayah"] - 1) / max(1, length_v - 1)
        normalized_sum[word][1] += (token["global_token"] - surah_start[token["surah_id"]]) / max(1, len(by_surah[token["surah_id"]]) - 1)
        normalized_sum[word][2] += (token["global_token"] - 1) / max(1, len(tokens) - 1)
    window = cfg["position_window_tokens"]
    starts = window_starts(len(tokens), window, cfg["position_window_step"])
    save("position_windows", ["window_id", "start_global_token", "end_global_token", "start_token_id", "end_token_id", "denominator", "profile_id", "scope"],
         (dict(window_id=i + 1, start_global_token=s, end_global_token=min(s + window - 1, len(tokens)),
               start_token_id=tokens[s - 1]["token_id"], end_token_id=tokens[min(s + window - 1, len(tokens)) - 1]["token_id"],
               denominator=min(window, len(tokens) - s + 1), profile_id="plain", scope="file") for i, s in enumerate(starts)))
    def occurrence_gap_rows():
        for word, positions in sorted(token_positions.items()):
            previous = None
            for rank, position in enumerate(positions, 1):
                token = tokens[position - 1]
                yield dict(word=word, occurrence=rank, token_id=token["token_id"], global_token=position,
                           global_ayah=token["global_ayah"], surah_id=token["surah_id"],
                           previous_token_id=previous["token_id"] if previous else "",
                           token_gap=position - previous["global_token"] if previous else "",
                           ayah_gap=token["global_ayah"] - previous["global_ayah"] if previous else "",
                           surah_gap=token["surah_id"] - previous["surah_id"] if previous else "",
                           profile_id="plain", scope="file")
                previous = token
    save("word_occurrence_gaps", ["word", "occurrence", "token_id", "global_token", "global_ayah", "surah_id", "previous_token_id", "token_gap", "ayah_gap", "surah_gap", "profile_id", "scope"], occurrence_gap_rows(), True)
    surah_counters = {s: Counter(t["plain"] for t in ts) for s, ts in by_surah.items()}
    def position_rows():
        for word, positions in sorted(token_positions.items()):
            n = len(positions)
            tgaps = np.diff(positions)
            agaps = np.diff(sorted(ayah_positions[word]))
            sgaps = np.diff(sorted(surah_positions[word]))
            missing = [positions[0] - 1, len(tokens) - positions[-1]] + [int(g) - 1 for g in tgaps]
            counts = [bisect.bisect_left(positions, s + window) - bisect.bisect_left(positions, s) for s in starts]
            peak = max(range(len(counts)), key=counts.__getitem__)
            hhi = sum((counter[word] / n) ** 2 for counter in surah_counters.values())
            kl = sum((counter[word] / n) * math.log2((counter[word] / n) / (len(by_surah[s]) / len(tokens)))
                     for s, counter in surah_counters.items() if counter[word])
            thirds = {f"{unit}_{label}_{measure}": (other_thirds[unit][word][i] if measure == "count"
                         else other_thirds[unit][word][i] / other_denominators[unit][i] * 10000 if other_denominators[unit][i] else "")
                      for unit in other_thirds for i, label in enumerate(("beginning", "middle", "end")) for measure in ("count", "per_10000")}
            yield dict(word=word, frequency=n, first_global_token=positions[0], last_global_token=positions[-1],
                       first_token_id=tokens[positions[0] - 1]["token_id"], last_token_id=tokens[positions[-1] - 1]["token_id"],
                       frequency_band="1" if n == 1 else "2-9" if n < 10 else "10-99" if n < 100 else "100+",
                       mean_token_gap=float(np.mean(tgaps)) if n > 1 else "", max_token_gap=int(max(tgaps)) if n > 1 else "",
                       max_ayah_gap=int(max(agaps)) if len(agaps) else "", max_surah_gap=int(max(sgaps)) if len(sgaps) else "",
                       max_absent_token_run=max(missing), surah_hhi=hhi, surah_kl_vs_length_bits=kl,
                       mean_normalized_ayah_position=normalized_sum[word][0] / n,
                       mean_normalized_surah_position=normalized_sum[word][1] / n,
                       mean_normalized_corpus_position=normalized_sum[word][2] / n,
                       beginning_count=within_positions[word][0], middle_count=within_positions[word][1], end_count=within_positions[word][2],
                       beginning_per_10000=within_positions[word][0] / within_denominators[0] * 10000 if within_denominators[0] else "",
                       middle_per_10000=within_positions[word][1] / within_denominators[1] * 10000 if within_denominators[1] else "",
                       end_per_10000=within_positions[word][2] / within_denominators[2] * 10000 if within_denominators[2] else "",
                       peak_window_start=starts[peak], peak_window_count=counts[peak],
                       peak_window_starts_json=json.dumps([s for s, count in zip(starts, counts) if count == counts[peak]]),
                       peak_window_denominator=min(window, len(tokens) - starts[peak] + 1), profile_id="plain", scope="file", **thirds)
    position_fields = ["word", "frequency", "first_global_token", "last_global_token", "mean_token_gap", "max_token_gap", "max_ayah_gap", "max_surah_gap", "max_absent_token_run", "surah_hhi", "surah_kl_vs_length_bits", "mean_normalized_ayah_position", "mean_normalized_surah_position", "mean_normalized_corpus_position", "beginning_count", "middle_count", "end_count", "beginning_per_10000", "middle_per_10000", "end_per_10000", "peak_window_start", "peak_window_count", "peak_window_denominator", "profile_id", "scope"]
    position_fields += ["first_token_id", "last_token_id", "frequency_band", "peak_window_starts_json"]
    position_fields += [f"{unit}_{label}_{measure}" for unit in other_thirds for label in ("beginning", "middle", "end") for measure in ("count", "per_10000")]
    save("positions", position_fields, position_rows(), True)
    coverage.append(dict(direction="D", analysis="Интервалы, трети и концентрация всех словоформ", status="complete_within_scope",
                         universe={"types": len(frequencies), "profile": "plain", "window": window, "step": cfg["position_window_step"], "windows": len(starts), "thirds_units": ["ayah", "surah", "corpus"], "frequency_bands": ["1", "2-9", "10-99", "100+"]}, tested=len(frequencies),
                         limitations="Описательные оценки без p-value; окна в глобальных токенах могут пересекать границы сур, последнее полное окно дополнительно привязано к концу корпуса. В сводке промежутки аятов/сур — между различными занятыми единицами; word_occurrence_gaps сохраняет интервалы последовательных вхождений, включая нулевые. Точки изменения и совпадения профилей всех пар не исследованы."))

    # E: границы, зеркальные пары и длины. Нечётный центр исключается из обеих половин.
    boundaries = defaultdict(list)
    verse_feature_rows, mirror_rows = [], []
    by_surah_verses = defaultdict(list)
    for vi, verse in enumerate(verses):
        seq = sequences[vi]
        by_surah_verses[verse["surah_id"]].append((verse, seq))
        letters = [char for _, char in base_letters("".join(seq))]
        for n in [1, 2, 3]:
            if len(seq) >= n:
                boundaries[("word_start", n, " ".join(seq[:n]))].append(verse["verse_id"])
                boundaries[("word_end", n, " ".join(seq[-n:]))].append(verse["verse_id"])
            if len(letters) >= n:
                boundaries[("letter_start", n, "".join(letters[:n]))].append(verse["verse_id"])
                boundaries[("letter_end", n, "".join(letters[-n:]))].append(verse["verse_id"])
        verse_feature_rows.append(dict(verse_id=verse["verse_id"], surah_id=verse["surah_id"], ayah_id=verse["ayah_id"],
                                      tokens=len(seq), types=len(set(seq)), letters=verse["letter_count"],
                                      entropy_bits=entropy(Counter(seq)), first_word=seq[0] if seq else "", last_word=seq[-1] if seq else "",
                                      first_last_word_equal=int(bool(seq) and seq[0] == seq[-1]),
                                      token_palindrome=int(bool(seq) and seq == seq[::-1]), profile_id="plain", scope="file"))
    save("verse_boundaries", ["kind", "n", "sequence", "frequency", "verse_ids", "profile_id", "scope"],
         (dict(kind=kind, n=n, sequence=text, frequency=len(ids), verse_ids="|".join(ids), profile_id="plain", scope="file")
          for (kind, n, text), ids in sorted(boundaries.items())))
    save("verse_features", ["verse_id", "surah_id", "ayah_id", "tokens", "types", "letters", "entropy_bits", "first_word", "last_word", "first_last_word_equal", "token_palindrome", "profile_id", "scope"], verse_feature_rows)
    surah_features = []
    cumulative_tokens = 0
    for surah, items in sorted(by_surah_verses.items()):
        allwords = [word for _, seq in items for word in seq]
        half = len(items) // 2
        left = [word for _, seq in items[:half] for word in seq]
        right = [word for _, seq in items[len(items) - half:] for word in seq] if half else []
        for i in range(half):
            va, sa = items[i]
            vb, sb = items[-i - 1]
            mirror_rows.append(dict(surah_id=surah, verse_a=va["verse_id"], verse_b=vb["verse_id"], tokens_a=len(sa), tokens_b=len(sb),
                                    tokens_equal=int(len(sa) == len(sb)), letters_a=va["letter_count"], letters_b=vb["letter_count"],
                                    letters_equal=int(va["letter_count"] == vb["letter_count"]), exact_sequence_equal=int(sa == sb)))
        cumulative_tokens += len(allwords)
        counter = Counter(allwords)
        h = entropy(counter)
        surah_features.append(dict(surah_id=surah, verses=len(items), tokens=len(allwords), letters=sum(v["letter_count"] for v, _ in items),
                                   types=len(counter), entropy_bits=h, perplexity=2 ** h, type_token_ratio=len(counter) / len(allwords),
                                   first_global_ayah=items[0][0]["global_ayah"], last_global_ayah=items[-1][0]["global_ayah"], cumulative_tokens=cumulative_tokens,
                                   first_half_tokens=len(left), second_half_tokens=len(right), omitted_central_ayah=items[half][0]["verse_id"] if len(items) % 2 else "",
                                   half_vocabulary_jaccard=len(set(left) & set(right)) / len(set(left) | set(right)) if left or right else "",
                                   profile_id="plain", scope="file"))
    save("surah_features", list(surah_features[0]), surah_features)
    save("mirror_verse_pairs", list(mirror_rows[0]) if mirror_rows else ["surah_id"], mirror_rows)
    coverage.append(dict(direction="E", analysis="Границы, половины и формальные зеркальные пары", status="complete_within_scope",
                         universe={"boundaries_n": [1, 2, 3], "mirror": "a ↔ A+1−a", "half_unit": "ayah"}, tested=len(verses) + len(mirror_rows),
                         limitations="Нечётный центральный аят исключён из обеих половин. Равенство длин не означает смысловую симметрию; письменное окончание не трактуется как рифма."))

    # Полная матрица сходства сур по TF–IDF, без стоп-слов.
    vocabulary_all = sorted(frequencies)
    columns = {w: i for i, w in enumerate(vocabulary_all)}
    surah_ids = sorted(by_surah)
    tf = np.zeros((len(surah_ids), len(columns)), dtype=np.float64)
    for i, surah in enumerate(surah_ids):
        for word, count in surah_counters[surah].items():
            tf[i, columns[word]] = count
    df = (tf > 0).sum(axis=0)
    idf = np.log((1 + len(surah_ids)) / (1 + df)) + 1
    tfidf = tf * idf
    tfidf /= np.maximum(np.linalg.norm(tfidf, axis=1, keepdims=True), 1e-30)
    cosine = tfidf @ tfidf.T
    similarity_rows = []
    for a, b in itertools.combinations(range(len(surah_ids)), 2):
        sa, sb = surah_ids[a], surah_ids[b]
        similarity_rows.append(dict(surah_a=sa, surah_b=sb, cosine=float(cosine[a, b]),
                                    shared_types=len(set(surah_counters[sa]) & set(surah_counters[sb])),
                                    tokens_a=len(by_surah[sa]), tokens_b=len(by_surah[sb]), adjacent=int(sb == sa + 1), profile_id="plain", scope="file"))
    similarity_rows.sort(key=lambda r: (-r["cosine"], r["surah_a"], r["surah_b"]))
    save("surah_similarity", list(similarity_rows[0]) if similarity_rows else ["surah_a"], similarity_rows)

    # F: полный журнал заранее ограниченной целочисленной грамматики.
    numeric_vars = ["surah_id", "verses", "tokens", "letters", "types", "first_global_ayah", "last_global_ayah", "cumulative_tokens"]
    numeric_counts = Counter()
    numeric_examples = []
    def numeric_rows():
        for row in surah_features:
            values = {name: int(row[name]) for name in numeric_vars}
            for test in integer_search(str(row["surah_id"]), values, cfg["numeric_divisors"]):
                numeric_counts[test["family"] + "_tested"] += 1
                numeric_counts[test["family"] + "_matched"] += test["result"]
                test.update(profile_id="plain", scope="file", status="descriptive_no_significance")
                if test["family"] == "equality" and test["result"] and len(numeric_examples) < 30:
                    numeric_examples.append(test.copy())
                yield test
    save("numeric_tests", ["unit_id", "family", "expression", "comparison", "lhs", "rhs", "result", "residue", "profile_id", "scope", "status"], numeric_rows(), True)
    equal_groups = defaultdict(list)
    for word, count in frequencies.items():
        equal_groups[count].append(word)
    save("frequency_equivalence", ["frequency", "type_count", "pair_count", "members_json", "profile_id", "scope"],
         (dict(frequency=freq, type_count=len(words), pair_count=len(words) * (len(words) - 1) // 2,
               members_json=json.dumps(sorted(words), ensure_ascii=False), profile_id="plain", scope="file")
          for freq, words in sorted(equal_groups.items())))
    coverage.append(dict(direction="F", analysis="Частотные классы и целочисленные равенства", status="complete_within_scope",
                         universe={"variables": numeric_vars, "expressions": "x; x+y (x≤y по имени)", "comparison": "expression == atomic_variable", "divisors": cfg["numeric_divisors"], "rounding": "нет", "depth": 1, "constants_in_equalities": []},
                         tested=sum(v for k, v in numeric_counts.items() if k.endswith("_tested")),
                         limitations="Сохранены совпадения и несовпадения; саморавенства и симметричные дубли атомарных равенств исключены заранее. Значимость не заявляется. Абджад, произвольные цифросклейки и числовые выражения языка в этот поиск не входят."))

    # G: бинарное присутствие, полное множество top-V пар в трёх контекстах.
    top_vocabulary = [w for w, _ in sorted(frequencies.items(), key=lambda pair: (-pair[1], pair[0]))[:cfg["cooccurrence_top_vocabulary"]]]
    window_size = cfg["cooccurrence_window_tokens"]
    contexts = {
        "verse": [(v["verse_id"], sequences[i]) for i, v in enumerate(verses)],
        "surah": [(str(s), [t["plain"] for t in ts]) for s, ts in sorted(by_surah.items())],
        "nonoverlap_window": [(f"{s}:tokens:{p+1}-{p+window_size}", [t["plain"] for t in ts[p:p+window_size]])
                              for s, ts in sorted(by_surah.items()) for p in range(0, len(ts) - window_size + 1, window_size)],
    }
    cooccur = [row for kind, units in contexts.items() for row in cooccurrence_rows(kind, units, top_vocabulary, cfg["cooccurrence_min_support"])]
    save("cooccurrence", ["context", "word_a", "word_b", "n_contexts", "count_a", "count_b", "count_ab", "pmi_bits", "lift", "support_pass", "example_context_ids", "profile_id", "scope"], cooccur, True)
    cooccur_top = sorted([r for r in cooccur if r["support_pass"] and r["context"] == "verse"], key=lambda r: (-r["lift"], -r["count_ab"]))[:20]
    save("cooccurrence_vocabulary", ["rank", "word", "token_frequency"], (dict(rank=i + 1, word=w, token_frequency=frequencies[w]) for i, w in enumerate(top_vocabulary)))
    coverage.append(dict(direction="G", analysis="Совместное присутствие и сходство сур", status="complete_within_scope",
                         universe={"top_vocabulary": len(top_vocabulary), "pairs_per_context": len(top_vocabulary) * (len(top_vocabulary) - 1) // 2, "contexts": {k: len(v) for k, v in contexts.items()}, "min_support": cfg["cooccurrence_min_support"]},
                         tested=len(cooccur) + len(similarity_rows), limitations=f"PMI/lift описательные, зависят от длины контекста; нулевые пары сохранены, для поддержки <{cfg['cooccurrence_min_support']} support_pass=0. Окна по {window_size} токенов внутри сур; хвосты короче окна исключены. Леммы и корни сюда не входят."))

    # H: информационные показатели и сравнимые окна; кодек фиксирован.
    global_entropy = entropy(frequencies)
    info_rows = [dict(level="corpus", unit_id="file", tokens=len(tokens), types=len(frequencies), entropy_bits=global_entropy,
                      perplexity=2 ** global_entropy, type_token_ratio=len(frequencies) / len(tokens), profile_id="plain", scope="file")]
    info_rows += [{k: row[k] for k in ["tokens", "types", "entropy_bits", "perplexity", "type_token_ratio", "profile_id", "scope"]}
                 | dict(level="surah", unit_id=str(row["surah_id"])) for row in surah_features]
    save("information", list(info_rows[0]), info_rows)
    growth = []
    seen = set()
    for i, word in enumerate(flat, 1):
        seen.add(word)
        if i % 100 == 0 or i == len(flat):
            growth.append(dict(token_position=i, types=len(seen), type_token_ratio=len(seen) / i, profile_id="plain", scope="file"))
    save("vocabulary_growth", list(growth[0]), growth)
    dwindow = cfg["diversity_window_tokens"]
    diversity = []
    for surah, ts in sorted(by_surah.items()):
        for pos in range(0, len(ts) - dwindow + 1, dwindow):
            counter = Counter(t["plain"] for t in ts[pos:pos + dwindow])
            diversity.append(dict(surah_id=surah, start_token_id=ts[pos]["token_id"], end_token_id=ts[pos + dwindow - 1]["token_id"],
                                  tokens=dwindow, types=len(counter), type_token_ratio=len(counter) / dwindow,
                                  entropy_bits=entropy(counter), profile_id="plain", scope="file"))
    save("window_diversity", ["surah_id", "start_token_id", "end_token_id", "tokens", "types", "type_token_ratio", "entropy_bits", "profile_id", "scope"], diversity)
    payload = " ".join(flat).encode("utf-8")
    observed_size = len(zlib.compress(payload, 9))
    compression_rows = [dict(control_id=0, ordering="observed", utf8_bytes=len(payload), compressed_bytes=observed_size, compression_ratio=observed_size / len(payload), seed=seed)]
    rng = random.Random(seed)
    for i in range(1, cfg["compression_controls"] + 1):
        shuffled = flat.copy()
        rng.shuffle(shuffled)
        data = " ".join(shuffled).encode("utf-8")
        compressed = len(zlib.compress(data, 9))
        compression_rows.append(dict(control_id=i, ordering="global_token_shuffle", utf8_bytes=len(data), compressed_bytes=compressed, compression_ratio=compressed / len(data), seed=seed))
    save("compression_controls", list(compression_rows[0]), compression_rows)
    coverage.append(dict(direction="H", analysis="Энтропия, рост словаря, равные окна и сжатие", status="complete_within_scope",
                         universe={"representation": "plain tokens joined by one ASCII space, UTF-8", "codec": "zlib level 9", "window": dwindow, "controls": cfg["compression_controls"], "vocabulary_growth_stride": 100, "include_final_growth_position": True}, tested=len(info_rows) + len(diversity) + len(compression_rows) + len(growth),
                         limitations="Энтропия нулевого порядка без модели синтаксиса; сырое TTR не используется для ранжирования сур. Короткие суры и хвосты не входят в сравнение фиксированных окон. Контроль сжатия разрушает весь порядок и не доказывает специфичность корпуса. Степенной закон и периодичность не заявляются."))

    top_repeated = [r for r in repeat_rows if r["profile_id"] == "plain" and r["scope"] == "file"][:10]
    findings.append(dict(hypothesis_id="EX-C-001", family="C", title_ru="Точные повторяющиеся аяты зависят от профиля записи", status="descriptive_verified",
                         profile_id="plain", scope="file", observed=repeat_inventory, method_ru="Полная группировка аятов по raw/nfc/plain/search и двум правилам вступительной басмалы.",
                         limitation_ru="Равенство написания не является самостоятельным свидетельством необычности; для numbered сравнивается последовательность оставшихся токенов.",
                         evidence_path=paths["exact_verse_repeats"]["path"], p_value=None))
    findings.append(dict(hypothesis_id="EX-C-002", family="C", title_ru="Длинные точные повторяющиеся последовательности", status="candidate_bounded_search",
                         profile_id="plain", scope="file", observed={"groups": len(long_rows), "longest": long_rows[0] if long_rows else None},
                         method_ru=f"Расширение всех пар {cfg['long_repeat_seed_length']}-токенных затравок с поддержкой 2–{cfg['long_repeat_seed_max_support']} до несовпадения/границы аята; минимум {cfg['long_repeat_min_length']} токенов.",
                         limitation_ru="Это ограниченный поиск кандидатов; повторы, состоящие только из более частых затравок, могут быть пропущены. Найденные адреса доступны полностью.",
                         evidence_path=paths["long_repeat_pairs"]["path"], p_value=None))
    findings.append(dict(hypothesis_id="EX-F-001", family="F", title_ru="Совпадения ограниченной целочисленной грамматики", status="descriptive_no_significance",
                         profile_id="plain", scope="file", observed=dict(numeric_counts), method_ru=f"Все равенства x=y и x+y=z восьми характеристик каждой суры; делимость на {','.join(map(str, cfg['numeric_divisors']))} и простота атомов.",
                         limitation_ru="Формулы имеют разные естественные зависимости. Совпадение без модели контроля и учёта выбора не является статистическим доказательством.",
                         evidence_path=paths["numeric_tests"]["path"], p_value=None))
    findings.append(dict(hypothesis_id="EX-G-001", family="G", title_ru="Ассоциации частых слов в аятах", status="descriptive_verified",
                         profile_id="plain", scope="file", observed=cooccur_top[:5], method_ru=f"Бинарное присутствие top-{len(top_vocabulary)} словоформ; PMI/lift, поддержка не менее {cfg['cooccurrence_min_support']} совместных аятов; все пары сохранены.",
                         limitation_ru="Не учитывает грамматическую роль и все различия длины аятов; статистическая значимость и семантическая связь не заявляются.",
                         evidence_path=paths["cooccurrence"]["path"], p_value=None))
    findings.append(dict(hypothesis_id="EX-H-001", family="H", title_ru="Порядок токенов и степень сжатия", status="descriptive_control_comparison",
                         profile_id="plain", scope="file", observed={"observed_ratio": compression_rows[0]["compression_ratio"], "shuffle_mean_ratio": float(np.mean([r["compression_ratio"] for r in compression_rows[1:]]))},
                         method_ru=f"UTF-8 из plain-токенов, пробел-разделитель, zlib level 9; {cfg['compression_controls']} глобальных перестановок при seed={seed}.",
                         limitation_ru="Контроль сохраняет частоты и байтовую длину, но разрушает грамматику и границы. Это свойство порядка данного текста, не тест уникальности Корана.",
                         evidence_path=paths["compression_controls"]["path"], p_value=None))
    summary = dict(profile_id="plain", scope="file", config=cfg, seed=seed, tables=paths,
                   word_ngram_occurrences=occurrence_count, letter_ngram_occurrences=letter_count,
                   repeat_inventory=repeat_inventory, long_repeat_groups=len(long_rows), long_repeat_seed_pairs=seed_pairs,
                   approximate_counts=dict(approximate_counts), approximate_candidate_pairs=paths["approximate_candidates"]["rows"],
                   top_word_ngrams=top_word_ngrams, top_repeated_verses=top_repeated, top_long_repeats=long_rows[:10],
                   top_approximate=approx_top[:10], top_surah_similarity=similarity_rows[:10], top_cooccurrence=cooccur_top,
                   numeric_counts=dict(numeric_counts), numeric_examples=numeric_examples,
                   information=info_rows[0], compression=compression_rows,
                   within_ayah_thirds_denominators=within_denominators,
                   within_surah_thirds_denominators=other_denominators["surah"],
                   corpus_thirds_denominators=other_denominators["corpus"],
                   duration_seconds=time.monotonic() - start_time)
    dump(root / "results/exploration_summary.json", summary)
    dump(root / "results/exploration_coverage.json", coverage)
    dump(root / "results/hypotheses/exploration.json", findings)
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Конечные поиски повторов, структуры и совместной встречаемости")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    configuration = {}
    json_path = args.root / "config/analysis.json"
    yaml_path = args.root / "config/analysis.yaml"
    if json_path.exists():
        configuration = json.loads(json_path.read_text(encoding="utf-8"))
    elif yaml_path.exists():
        import yaml
        configuration = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    print(json.dumps(run(args.root, configuration), ensure_ascii=False, indent=2))
