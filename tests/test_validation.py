"""Validation must detect broken data, not simply repeat production outputs."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from quran_analysis.validation import independent_source, independent_profiles, validate_database, _validate_basmala, _validate_catalogues


SAMPLE = (
    "1|1|بِسْمِ اللَّهِ الرَّحْمَـٰنِ الرَّحِيمِ\n"
    "2|1|بِسْمِ اللَّهِ الرَّحْمَـٰنِ الرَّحِيمِ الم\n"
    "9|1|بَرَاءَةٌ ۚ مِنَ اللَّهِ\n"
    "27|30|إِنَّهُ مِن سُلَيْمَانَ وَإِنَّهُ بِسْمِ اللَّهِ الرَّحْمَـٰنِ الرَّحِيمِ\n"
    "95|1|بِّسْمِ اللَّهِ الرَّحْمَـٰنِ الرَّحِيمِ وَالتِّينِ\n"
)


def write_fixture(path: Path, reference: dict):
    con = sqlite3.connect(path)
    for table in ("verses", "tokens"):
        rows = reference[table]
        values = []
        for source in rows:
            row = dict(source)
            if table == "verses":
                row["mark_count"] = row.pop("mark_count_all")
                row.pop("mark_count_tokens")
            values.append(row)
        columns = list(values[0])
        con.execute(f"CREATE TABLE {table} (" + ",".join(f'{c} {"INTEGER" if isinstance(values[0][c], int) else "TEXT"}' for c in columns) + ")")
        con.executemany(f'INSERT INTO {table} VALUES (' + ','.join('?' for _ in columns) + ')', [[r[c] for c in columns] for r in values])
    con.commit()
    con.close()


class IndependentValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "corpus.sqlite"
        self.reference = independent_source(SAMPLE)
        write_fixture(self.path, self.reference)

    def tearDown(self):
        self.temp.cleanup()

    def checks(self):
        checks = []
        validate_database(self.path, SAMPLE, self.reference, checks)
        return {c["check_id"]: c for c in checks}

    def mutate(self, sql):
        with sqlite3.connect(self.path) as con:
            con.execute(sql)

    def test_valid_fixture_and_editorial_mark_conservation(self):
        checks = self.checks()
        self.assertTrue(all(c["status"] != "fail" for c in checks.values()))
        self.assertEqual(checks["database.complete_schema"]["status"], "not_run")
        result = checks["aggregates.mark_conservation"]
        self.assertEqual(result["source_all_marks"] - result["source_token_marks"], 1)
        self.assertEqual(len(self.reference["tokens"]), 25)

    def test_raw_span_off_by_one_is_detected(self):
        self.mutate("UPDATE tokens SET raw_start = raw_start + 1 WHERE token_id='2:1:5'")
        self.assertEqual(self.checks()["tokens.raw_spans"]["status"], "fail")

    def test_corrupt_surface_without_changing_token_total_is_detected(self):
        self.mutate("UPDATE tokens SET plain='اختبار' WHERE token_id='9:1:1'")
        checks = self.checks()
        self.assertEqual(checks["frequencies.file.plain"]["status"], "fail")
        self.assertEqual(checks["tokens.row_count"]["status"], "pass")

    def test_internal_basmala_cannot_be_excluded(self):
        self.mutate("UPDATE tokens SET is_opening_basmala=1 WHERE token_id='27:30:5'")
        checks = self.checks()
        self.assertEqual(checks["tokens.independent_values"]["status"], "fail")
        self.assertEqual(checks["frequencies.numbered.raw"]["status"], "fail")

    def test_positions_include_removed_prefix_tokens(self):
        token = next(t for t in self.reference["tokens"] if t["token_id"] == "2:1:5")
        self.assertEqual(token["token_in_ayah"], 5)
        self.assertEqual(token["global_token"], 9)
        self.assertFalse(token["is_opening_basmala"])

    def test_shadda_variant_is_still_an_opening_prefix(self):
        self.assertEqual(sum(t["is_opening_basmala"] for t in self.reference["tokens"] if t["verse_id"] == "95:1"), 4)
        self.assertTrue(all(not t["is_opening_basmala"] for t in self.reference["tokens"] if t["verse_id"] in {"1:1", "27:30", "9:1"}))

    def test_significant_combining_marks_and_teh_marbuta_survive(self):
        self.assertEqual(independent_profiles("ب\u0654َةى")["plain"], "ب\u0654ةى")
        self.assertEqual(independent_profiles("ب\u0654َةى")["search"], "ب\u0654ةي")
        self.assertEqual(independent_profiles("ا\u0653")["plain"], "آ")

    def test_manual_letter_mark_token_counts_and_editorial_exclusion(self):
        # The expectations are handwritten, not computed with either tokenizer.
        result = independent_source("1|1|بِّ تَ ۚ ةى\n")
        self.assertEqual([t["raw"] for t in result["tokens"]], ["بِّ", "تَ", "ةى"])
        self.assertEqual([t["letter_count"] for t in result["tokens"]], [1, 1, 2])
        self.assertEqual([t["mark_count"] for t in result["tokens"]], [2, 1, 0])
        self.assertEqual(result["verses"][0]["mark_count_all"], 4)
        self.assertEqual(result["tokens"][1]["raw_start"], 8)
        self.assertEqual(result["tokens"][1]["raw_end"], 10)

    def test_late_span_error_is_detected_after_many_value_errors(self):
        self.mutate("UPDATE tokens SET plain='broken', search='broken'")
        self.mutate("UPDATE tokens SET raw_start = raw_start + 1 WHERE global_token=25")
        checks = self.checks()
        self.assertEqual(checks["tokens.raw_spans"]["status"], "fail")

    def test_editorial_marks_cannot_silently_disappear_from_verse_total(self):
        self.mutate("UPDATE verses SET mark_count=mark_count-1 WHERE verse_id='9:1'")
        self.assertEqual(self.checks()["aggregates.mark_conservation"]["status"], "fail")

    def test_missing_97_prefix_cannot_claim_112_prefix_success(self):
        checks = []
        _validate_basmala(self.reference, checks)
        checks = {c["check_id"]: c for c in checks}
        self.assertEqual(checks["basmala.prefixes"]["status"], "fail")
        self.assertEqual(checks["basmala.orthographic_variants"]["status"], "fail")

    def test_merged_registry_is_not_counted_as_new_hypotheses(self):
        import json
        root = Path(self.temp.name)
        (root / "results/hypotheses").mkdir(parents=True)
        rows = [{"hypothesis_id": "ONE", "evidence_path": "corpus.sqlite"}]
        for name in ("core", "registry"):
            (root / f"results/hypotheses/{name}.json").write_text(json.dumps(rows))
        checks = []
        _, hypotheses = _validate_catalogues(root, checks)
        self.assertEqual(len(hypotheses), 1)
        self.assertEqual(next(c for c in checks if c["check_id"] == "hypotheses.unique_ids")["status"], "pass")


if __name__ == "__main__":
    unittest.main()
