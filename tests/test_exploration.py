"""Independent finite-universe and corpus-boundary checks for exploration."""
import csv
import gzip
import hashlib
import itertools
import json
from collections import Counter

import pytest

from quran_analysis import core, exploration


def read_table(root, table):
    path = root / table["path"]
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_public_configuration_bounds_are_resolved_and_conflicts_rejected():
    config = exploration.resolve_config({"exploration": {
        "word_ngram_min": 2, "word_ngram_max": 3, "cooccurrence_top_n": 7,
        "divisors": [19, 2], "window_tokens": 9, "long_repeat_seed": 3,
    }})
    assert config["word_ngram_lengths"] == [2, 3]
    assert config["numeric_divisors"] == [2, 19]
    assert config["cooccurrence_top_vocabulary"] == 7
    assert config["diversity_window_tokens"] == 9
    assert config["long_repeat_seed_length"] == 3
    for invalid in ({"unimplemented_option": 1}, {"max_formula_depth": 2},
                    {"cooccurrence_top_n": 3, "cooccurrence_top_vocabulary": 4},
                    {"word_ngram_min": 4, "word_ngram_max": 2},
                    {"long_repeat_seed": 9, "long_repeat_min_length": 8}):
        with pytest.raises(ValueError):
            exploration.resolve_config({"exploration": invalid})


def test_maximal_pair_extension_and_overlapping_occurrences():
    sequences = [list("xabcdefy"), list("zabcdefq"), list("aaaaa")]
    assert exploration.pair_extension(sequences, (0, 3), (1, 3), 2) == (0, 1, 1, 1, 6)
    assert exploration.pair_extension(sequences, (2, 1), (2, 2), 2) == (2, 0, 2, 1, 4)
    assert list(exploration.iter_ngrams("aaaa", 3)) == [(0, ("a", "a", "a")), (1, ("a", "a", "a"))]
    assert exploration.nonoverlapping_count([(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)], 2) == 3


def test_numeric_universe_and_exact_results():
    rows = list(exploration.integer_search("s", {"a": 2, "b": 3, "c": 5}, [2, 3]))
    # 3 atomic equalities + 6 unordered sums × 3 targets + 3 × (2 divisors + primality).
    assert len(rows) == 30
    assert sum(r["family"] == "equality" for r in rows) == 21
    equal = [r for r in rows if r["family"] == "equality" and r["result"]]
    assert [(r["expression"], r["comparison"]) for r in equal] == [("a+b", "c")]
    assert all(r["result"] == (r["lhs"] == r["rhs"]) for r in rows if r["family"] == "equality")
    assert all(exploration.is_prime(n) == (n in {2, 3, 5, 7, 11, 13, 17, 19}) for n in range(20))


def test_cooccurrence_is_binary_and_retains_absent_pairs():
    rows = list(exploration.cooccurrence_rows("verse", [("1", "aab"), ("2", "ac"), ("3", "c")], list("abc"), 2))
    ab, ac, bc = rows
    assert (ab["count_a"], ab["count_b"], ab["count_ab"], ab["lift"]) == (2, 1, 1, 1.5)
    assert ac["count_ab"] == 1
    assert bc["count_ab"] == 0 and bc["pmi_bits"] == ""
    assert all(r["support_pass"] == 0 for r in rows)


def test_windows_preserve_full_denominators_at_tail():
    assert exploration.window_starts(10, 4, 4) == [1, 5, 7]
    assert exploration.window_starts(4, 4, 3) == [1]
    assert exploration.window_starts(2, 4, 3) == [1]


def test_complete_tiny_corpus_occurrences_counts_scopes_and_readonly_database(tmp_path):
    source = ("1|1|بسم الله الرحمن الرحيم\n"
              "1|2|ألف باء جيم دال هاء واو زاي حاء\n"
              "1|3|ألف باء جيم دال هاء واو زاي حاء\n"
              "2|1|بسم الله الرحمن الرحيم ألف باء\n"
              "2|2|ألف باء جيم دال هاء واو زاي طاء\n"
              "3|1|ألف ألف باء\n").encode()
    _, verses, errors = core.parse_source(source)
    assert not errors
    tokens, spans, _ = core.tokenize(verses)
    database = tmp_path / "data/processed/corpus.sqlite"
    connection = core.create_database(database, verses, tokens, spans)
    connection.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    summary = exploration.run(tmp_path, {"seed": 123, "exploration": {
        "word_ngram_min": 2, "word_ngram_max": 3,
        "letter_ngram_min": 2, "letter_ngram_max": 2,
        "long_repeat_seed": 3, "long_repeat_min_length": 6,
        "cooccurrence_top_n": 5, "cooccurrence_window_tokens": 3,
        "window_tokens": 4, "position_window_tokens": 7, "position_window_step": 3,
        "compression_controls": 2,
    }})
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    tables = summary["tables"]
    for table in tables.values():
        assert len(read_table(tmp_path, table)) == table["rows"]
    assert summary["word_ngram_occurrences"] == sum(max(0, v["token_count"] - n + 1) for v in verses for n in (2, 3))
    by_id = {t["token_id"]: t for t in tokens}
    grams = {g["ngram_id"]: g for g in read_table(tmp_path, tables["word_ngrams"])}
    counts = Counter()
    for row in read_table(tmp_path, tables["word_ngram_occurrences"]):
        first, last = by_id[row["start_token_id"]], by_id[row["end_token_id"]]
        assert first["verse_id"] == last["verse_id"] == row["verse_id"]
        assert last["token_in_ayah"] - first["token_in_ayah"] + 1 == int(row["n"])
        observed = " ".join(t["plain"] for t in tokens[first["global_token"] - 1:last["global_token"]])
        assert observed == grams[row["ngram_id"]]["sequence"]
        counts[row["ngram_id"]] += 1
    assert all(counts[k] == int(g["frequency"]) for k, g in grams.items())
    assert summary["repeat_inventory"]["plain/file"] == {"repeated_groups": 1, "verses_in_groups": 2}
    assert summary["top_long_repeats"][0]["token_length"] == 8
    assert summary["top_long_repeats"][0]["frequency"] == 2
    assert summary["approximate_counts"]["high_similarity_nonidentical"] == 2
    assert summary["information"]["tokens"] == len(tokens)
    assert sum(summary["within_ayah_thirds_denominators"]) == len(tokens)
    assert sum(summary["within_surah_thirds_denominators"]) == len(tokens)
    assert sum(summary["corpus_thirds_denominators"]) == len(tokens)
    assert tables["cooccurrence"]["rows"] == 3 * len(list(itertools.combinations(range(5), 2)))
    assert summary["numeric_counts"]["equality_tested"] == 3 * (28 + 36 * 8)
    coverage = json.loads((tmp_path / "results/exploration_coverage.json").read_text())
    assert {row["direction"] for row in coverage} == set("CDEFGH")
