from pathlib import Path
import pytest
from quran_analysis.morphology import alignment_keys, align_verse, bw_arabic, frequencies, load_qac


def source(*forms):
    return [dict(raw=w,token_id=f'1:1:{i}',verse_id='1:1') for i,w in enumerate(forms,1)]


def external(*forms):
    return [dict(surface=w,qac_word_id=f'1:1:{i}',verse_id='1:1') for i,w in enumerate(forms,1)]


def test_alignment_conserves_letter_distinctions():
    assert alignment_keys('إِنَّ') & alignment_keys('إِن')
    assert not alignment_keys('إِن') & alignment_keys('ان')
    assert not alignment_keys('رحمة') & alignment_keys('رحمه')
    assert not alignment_keys('على') & alignment_keys('علي')


def test_alignment_explicit_superscript_alef_alternatives():
    assert alignment_keys('الرَّحْمَٰن') & alignment_keys('الرحمن')
    assert alignment_keys('الرَّحْمَٰن') & alignment_keys('الرحمان')


def test_group_alignment_is_not_forced_one_to_one():
    rows=align_verse(source('و','قال'),external('وقال'))
    assert rows[0]['status']=='verified' and rows[0]['token_ids']=='1:1:1|1:1:2'
    rows=align_verse(source('وقال'),external('و','قال'))
    assert len(rows)==2 and all(r['status']=='verified' and r['token_ids']=='1:1:1' for r in rows)


def test_ambiguous_optima_withheld_not_arbitrarily_assigned():
    rows=align_verse(source('قال','قال'),external('قال'))
    assert rows[0]['status']=='ambiguous_verse' and rows[0]['token_ids']==''


def test_different_words_do_not_transfer_annotation():
    assert align_verse(source('قال'),external('كلب'))[0]['status']=='surface_mismatch'


def test_buckwalter_preserves_internal_whitespace_and_rejects_unknown():
    assert bw_arabic('<ilo yaAsiyna')=='إِلْ يَاسِينَ'
    with pytest.raises(ValueError):bw_arabic('Ω')


def test_frequency_denominators_and_group_linked_token_deduplication():
    ss=[dict(lemma='قال',lemma_bw='qaAl',token_ids='1:1:1|1:1:2',verse_id='1:1',surah_id=1,segment_id='1:1:1:1'),
        dict(lemma='قال',lemma_bw='qaAl',token_ids='1:1:2',verse_id='1:1',surah_id=1,segment_id='1:1:2:1'),
        dict(lemma='',token_ids='1:1:3',verse_id='1:1',surah_id=1,segment_id='1:1:3:1')]
    row=frequencies(ss,'lemma')[0]
    assert row['segment_count']==2 and row['token_count']==2
    assert row['share_of_verified_segments']==2/3 and row['share_of_nonempty_field_segments']==1


def test_saved_external_annotation_has_unique_expected_segments():
    path=Path(__file__).resolve().parents[1]/'data/external/quranic-corpus-morphology-0.4.txt'
    if not path.exists():pytest.skip('Внешний снимок не установлен')
    ss=load_qac(path)
    assert len(ss)==128219 and len({s['qac_word_id'] for s in ss})==77429
