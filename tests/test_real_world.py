import json
import sqlite3
from pathlib import Path

from quran_analysis.real_world import run


ROOT = Path(__file__).resolve().parents[1]


def test_calendar_comparisons_trace_to_corpus_and_sources():
    data = run(ROOT)
    cases = {case['id']: case for case in data['cases']}
    assert len(cases) == 10
    assert len(data['plain_findings']) == 46
    assert {row['id'] for row in data['plain_findings']} == {row['hypothesis_id'] for row in json.loads((ROOT / 'results/hypotheses/registry.json').read_text())}
    with sqlite3.connect(ROOT / 'data/processed/corpus.sqlite') as db:
        for case_id, verse_id in [('verse_twelve_months', '9:36'), ('solar_lunar_years', '18:25'), ('moon_as_calendar', '2:189'), ('sun_moon_light', '10:5')]:
            source = db.execute('SELECT raw_text FROM verses WHERE verse_id=?', (verse_id,)).fetchone()[0]
            assert cases[case_id]['evidence'][0]['text'] == source
    calculation = cases['solar_lunar_years']['calculation']
    assert calculation['lunar_years'] == 300 * 365.25 / (12 * 29.53059)
    assert 309 < calculation['lunar_years'] < 310
    day = cases['day_word_year']
    assert day['kind'] == 'unconfirmed'
    assert '362' in day['number'] and '3' in day['number']
    assert all(data['sources'][key]['url'].startswith('https://') for case in cases.values() for key in case['sources'])
    assert json.loads((ROOT / 'results/real_world.json').read_text()) == data
