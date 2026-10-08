"""Tests protect actual failure modes: Arabic marks, prefix anchoring, and offsets."""
import unittest
import csv
import gzip
import sqlite3
import tempfile
from pathlib import Path
from quran_analysis.core import normalize, parse_source, tokenize, metrics, is_letter, run

BASMALA='بِسْمِ اللَّهِ الرَّحْمَـٰنِ الرَّحِيمِ'

class CoreTests(unittest.TestCase):
    def test_profiles_preserve_letter_distinctions_and_significant_combining_marks(self):
        self.assertEqual(normalize('أ إ آ ٱ ى ي ة ه','plain'),'أ إ آ ٱ ى ي ة ه')
        self.assertEqual(normalize('أ إ آ ٱ ى ي ة ه','search'),'ا ا ا ا ي ي ة ه')
        # An uncomposable combining hamza and madda must survive plain.
        self.assertEqual(normalize('ب\u0654 ب\u0653 ب\u0655','plain'),'ب\u0654 ب\u0653 ب\u0655')
        self.assertEqual(normalize('ا\u0653 ا\u0654 ا\u0655','plain'),'آ أ إ')
        self.assertEqual(normalize('الرَّحْمَـٰنِ','plain'),'الرحمن')
        self.assertNotEqual(normalize('ﻻ','nfc'),'لا')  # no hidden NFKC

    def test_offsets_and_editorial_signs_roundtrip_with_crlf(self):
        raw=('1|1|'+BASMALA+'\r\n2|1|الم ۚ ذَٰلِكَ\r\n').encode()
        text,records,errors=parse_source(raw)
        self.assertFalse(errors)
        tokens,spans,_=tokenize(records)
        self.assertEqual(len(tokens),6)
        for token in tokens:
            self.assertEqual(text[token['raw_start']:token['raw_end']],token['raw'])
        self.assertEqual(records[0]['line_ending'],'\r\n')
        for record in records:
            self.assertEqual(''.join(s['raw'] for s in spans if s['verse_id']==record['verse_id']),record['raw_text'])
        self.assertEqual([s['raw'] for s in spans if s['kind']=='editorial'],['ۚ'])

    def test_basmala_removal_is_anchored_and_surah_scoped(self):
        lines=[f'1|1|{BASMALA}',f'2|1|{BASMALA} الم',f'9|1|بَرَاءَةٌ',
               f'27|30|إِنَّهُ مِن سُلَيْمَانَ وَإِنَّهُ {BASMALA}',
               '95|1|'+BASMALA.replace('بِسْمِ','بِّسْمِ')+' وَالتِّينِ',
               '97|1|'+BASMALA.replace('بِسْمِ','بِّسْمِ')+' إِنَّا',f'114|1|قُلْ {BASMALA}']
        _,records,errors=parse_source(('\n'.join(lines)+'\n').encode())
        self.assertFalse(errors)
        tokens,_,_=tokenize(records)
        excluded=[t for t in tokens if t['is_opening_basmala']]
        self.assertEqual(len(excluded),12)
        self.assertEqual({t['verse_id'] for t in excluded},{'2:1','95:1','97:1'})
        self.assertTrue(all(t['token_in_ayah']<=4 for t in excluded))

    def test_duplicate_ids_and_invalid_utf8_are_not_silently_repaired(self):
        _,_,errors=parse_source('1|1|ب\n1|1|ب\n'.encode())
        self.assertTrue(any(e['issue']=='duplicate_identifier' for e in errors))
        with self.assertRaises(UnicodeDecodeError):
            parse_source(b'1|1|\xff\n')

    def test_empty_source_is_a_reported_blocking_defect(self):
        text,records,errors=parse_source(b'')
        self.assertEqual((text,records),('',[]))
        self.assertEqual(errors[0]['issue'],'empty_source')
        self.assertEqual(errors[0]['status'],'blocking')

    def test_complete_small_import_audits_frequencies_offsets_and_scope_positions(self):
        # The first body token keeps file position 5 but becomes numbered position 1.
        source=(f'1|1|{BASMALA}\r\n1|2|أَ أَ ب\u0654 ۚ\r\n'
                f'2|1|{BASMALA}\t\tأَ  ب\u0654 ۚ').encode('utf-8')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'custom.txt').write_bytes(source)
            summary=run(root,{'input':'custom.txt'})
            self.assertEqual((summary['surahs'],summary['ayahs']),(2,3))
            self.assertIn('3 записей',summary['completeness_ru'])
            self.assertNotIn('6236',summary['completeness_ru'])
            self.assertEqual((summary['tokens_file'],summary['tokens_numbered']),(13,9))
            self.assertEqual((root/'data/raw/Quran_text.txt').read_bytes(),source)
            self.assertTrue(all(summary['checks'].values()))
            conn=sqlite3.connect(root/'data/processed/corpus.sqlite')
            self.assertEqual(conn.execute('SELECT count(*) FROM occurrences').fetchone()[0],4*(13+9))
            for profile in ('raw','nfc','plain','search'):
                for scope,total in [('file',13),('numbered',9)]:
                    frequency,share=conn.execute('SELECT sum(frequency),sum(share) FROM vocabulary WHERE profile_id=? AND scope=?',(profile,scope)).fetchone()
                    self.assertEqual(frequency,total)
                    self.assertAlmostEqual(share,1)
            self.assertEqual(conn.execute("SELECT frequency,ayah_count,surah_count FROM vocabulary WHERE profile_id='plain' AND scope='numbered' AND form='أ'").fetchone(),(3,2,2))
            self.assertEqual(conn.execute("SELECT raw_start,raw_end FROM tokens WHERE token_id='2:1:5'").fetchone(),
                             (source.decode().rindex('أَ'),source.decode().rindex('أَ')+2))
            self.assertEqual(conn.execute("SELECT scope_token_in_ayah FROM token_scopes WHERE token_id='2:1:5' AND scope='numbered'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT token_in_ayah FROM tokens WHERE token_id='2:1:5'").fetchone()[0],5)
            conn.close()
            with gzip.open(root/'results/tables/core_source_offsets_bytes.csv.gz','rt',encoding='utf-8') as handle:
                for row in csv.DictReader(handle):
                    byte_slice=source[int(row['byte_start']):int(row['byte_end'])].decode()
                    cp_slice=source.decode()[int(row['codepoint_start']):int(row['codepoint_end'])]
                    self.assertEqual(byte_slice,cp_slice)
            with gzip.open(root/'results/tables/core_surface_text_lengths.csv.gz','rt',encoding='utf-8') as handle:
                lengths={(r['profile_id'],r['scope'],r['verse_id']):r for r in csv.DictReader(handle)}
            actual=lengths['raw','numbered','2:1']
            self.assertEqual(int(actual['codepoints']),len('أَ  ب\u0654 ۚ'))
            self.assertEqual((int(actual['letters']),int(actual['marks'])),(2,3))
            # plain removes the stop sign but preserves its surrounding spaces and hamza.
            self.assertEqual(int(lengths['plain','numbered','2:1']['codepoints']),len('أ  ب\u0654 '))
            with (root/'results/tables/core_character_frequencies.csv').open(encoding='utf-8') as handle:
                rows=list(csv.DictReader(handle))
            group=[r for r in rows if (r['profile_id'],r['scope'])==('raw','numbered')]
            denominator=sum(int(r['frequency']) for r in group)
            self.assertTrue(all(int(r['denominator_all_token_codepoints'])==denominator for r in group))
            self.assertAlmostEqual(sum(float(r['share_all_token_codepoints']) for r in group),1)

    def test_written_letter_and_grapheme_counts_are_distinct(self):
        value='بَّـٰ'
        result=metrics(value)
        self.assertEqual(result['letters'],1)
        self.assertEqual(result['marks'],3)
        self.assertEqual(result['codepoints'],5)
        self.assertEqual(result['graphemes'],2)
        self.assertFalse(is_letter('ـ'))
        self.assertFalse(is_letter('ٰ'))
        self.assertTrue(is_letter('أ'))

if __name__=='__main__':
    unittest.main()
