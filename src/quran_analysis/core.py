"""Deterministic import and exhaustive surface-level catalogue of the supplied file."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
import shutil
import sqlite3
import statistics
import unicodedata as ud
from collections import Counter, defaultdict
from pathlib import Path

import regex

PROFILES = ('raw', 'nfc', 'plain', 'search')
SCOPES = ('file', 'numbered')
HARAKAT = set(map(chr, range(0x064B, 0x0653)))
EDITORIAL = set(map(chr, range(0x06D6, 0x06DD))) | {'\u06DE', '\u06E9'}
REMOVED = HARAKAT | {'\u0670', '\u0640'} | EDITORIAL
SEARCH_MAP = str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا', 'ى': 'ي'})
VERSION = '1.0.0'


def normalize(text: str, profile: str) -> str:
    if profile == 'raw':
        return text
    value = ud.normalize('NFC', text)
    if profile == 'nfc':
        return value
    value = ''.join(c for c in value if c not in REMOVED)
    if profile == 'plain':
        return value
    if profile == 'search':
        return value.translate(SEARCH_MAP)
    raise ValueError(f'Unknown profile: {profile}')


def is_letter(char: str) -> bool:
    """A written base letter, not a phoneme; tatweel is not a letter."""
    return ud.category(char).startswith('L') and char != '\u0640'


def metrics(text: str) -> dict:
    return {
        'codepoints': len(text),
        'graphemes': len(regex.findall(r'\X', text)),
        'letters': sum(is_letter(c) for c in text),
        'marks': sum(ud.category(c).startswith('M') for c in text),
        'harakat': sum(c in HARAKAT for c in text),
        'editorial': sum(c in EDITORIAL for c in text),
        'tatweel': text.count('\u0640'),
        'superscript_alef': text.count('\u0670'),
    }


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_csv(path: Path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = iter(rows)
    first = next(rows, None)
    if fields is None:
        fields = list(first) if first is not None else []
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'wt', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        if first is not None:
            writer.writerow(first)
        writer.writerows(rows)


def parse_source(blob: bytes):
    """Strict UTF-8; preserve raw code-point coordinates and every input line."""
    text = blob.decode('utf-8', errors='strict')
    records, errors = [], []
    if not text:
        errors.append({'line_number': '', 'issue': 'empty_source', 'raw': '',
                       'impact_ru': 'Исходный файл пуст; импорт остановлен.', 'status': 'blocking'})
    start = 0
    seen = set()
    previous = (0, 0)
    for line_no, full_line in enumerate(text.splitlines(keepends=True), 1):
        line = full_line.removesuffix('\n').removesuffix('\r')
        match = re.fullmatch(r'([1-9][0-9]*)\|([1-9][0-9]*)\|([^\r\n]*)', line)
        if not match:
            errors.append({'line_number': line_no, 'issue': 'invalid_record', 'raw': line,
                           'impact_ru': 'Строка не включена в структурированные таблицы.', 'status': 'blocking'})
            start += len(full_line)
            continue
        surah, ayah = map(int, match.group(1, 2))
        key = (surah, ayah)
        if key in seen:
            errors.append({'line_number': line_no, 'issue': 'duplicate_identifier', 'raw': f'{surah}:{ayah}',
                           'impact_ru': 'Неоднозначный идентификатор; импорт остановлен.', 'status': 'blocking'})
        if key <= previous:
            errors.append({'line_number': line_no, 'issue': 'non_increasing_identifier', 'raw': f'{surah}:{ayah}',
                           'impact_ru': 'Порядок отличается от числового.', 'status': 'warning'})
        seen.add(key)
        previous = key
        surface = match.group(3)
        if not surface.strip():
            errors.append({'line_number': line_no, 'issue': 'empty_verse', 'raw': f'{surah}:{ayah}',
                           'impact_ru': 'Пустой текст аята.', 'status': 'warning'})
        offset = start + match.start(3)
        records.append({'verse_id': f'{surah}:{ayah}', 'surah_id': surah, 'ayah_id': ayah,
                        'global_ayah': len(records) + 1, 'raw_text': surface,
                        'raw_start': offset, 'raw_end': offset + len(surface),
                        'source_line': line_no, 'line_start': start, 'line_end': start + len(full_line),
                        'line_ending': full_line[len(line):]})
        start += len(full_line)
    return text, records, errors


def tokenize(records):
    tokens, spans = [], []
    surah_positions = Counter()
    reference = next((v['raw_text'] for v in records if v['verse_id'] == '1:1'), '')
    reference_chunks = re.findall(r'\S+', reference)
    # Only the first four contiguous whitespace tokens, at the very start, qualify.
    # Compare their explicit plain surface: 95:1 and 97:1 have an extra written shadda.
    valid_reference = len(reference_chunks) == 4 and normalize(reference, 'plain') == 'بسم الله الرحمن الرحيم'
    for verse in records:
        text = verse['raw_text']
        first_chunks = list(re.finditer(r'\S+', text))[:4]
        has_prefix = (valid_reference and len(first_chunks) == 4 and first_chunks[0].start() == 0
                      and normalize(' '.join(m.group() for m in first_chunks), 'plain')
                      == normalize(reference, 'plain'))
        opening = verse['ayah_id'] == 1 and verse['surah_id'] not in (1, 9) and has_prefix
        in_ayah = 0
        for span_no, match in enumerate(re.finditer(r'\s+|\S+', text), 1):
            value = match.group()
            lexical = any(is_letter(c) for c in value)
            kind = 'token' if lexical else ('whitespace' if value.isspace() else 'editorial')
            tok_id = ''
            if lexical:
                in_ayah += 1
                surah_positions[verse['surah_id']] += 1
                tok_id = f"{verse['verse_id']}:{in_ayah}"
                m = metrics(value)
                tokens.append({'token_id': tok_id, 'verse_id': verse['verse_id'],
                    'surah_id': verse['surah_id'], 'ayah_id': verse['ayah_id'],
                    'global_ayah': verse['global_ayah'], 'global_token': len(tokens) + 1,
                    'token_in_ayah': in_ayah, 'token_in_surah': surah_positions[verse['surah_id']],
                    **{p: normalize(value, p) for p in PROFILES},
                    'raw_start': verse['raw_start'] + match.start(), 'raw_end': verse['raw_start'] + match.end(),
                    'letter_count': m['letters'], 'mark_count': m['marks'],
                    'is_opening_basmala': int(opening and in_ayah <= 4)})
            spans.append({'span_id': f"{verse['verse_id']}@{span_no}", 'verse_id': verse['verse_id'],
                          'kind': kind, 'token_id': tok_id, 'raw': value,
                          'raw_start': verse['raw_start'] + match.start(), 'raw_end': verse['raw_start'] + match.end()})
        m = metrics(text)
        verse.update({p: normalize(text, p) for p in PROFILES if p != 'raw'})
        verse.update(token_count=in_ayah, letter_count=m['letters'], mark_count=m['marks'])
    return tokens, spans, {'reference': reference, 'reference_valid': valid_reference}


def _sql_insert(conn, table, rows, fields):
    conn.executemany(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                     ([r[k] for k in fields] for r in rows))


def create_database(path, verses, tokens, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript('''
    PRAGMA journal_mode=DELETE;
    PRAGMA foreign_keys=ON;
    CREATE TABLE verses (verse_id TEXT PRIMARY KEY, surah_id INTEGER NOT NULL, ayah_id INTEGER NOT NULL,
       global_ayah INTEGER UNIQUE NOT NULL, raw_text TEXT NOT NULL, nfc TEXT NOT NULL, plain TEXT NOT NULL,
       search TEXT NOT NULL, raw_start INTEGER NOT NULL, raw_end INTEGER NOT NULL,
       token_count INTEGER NOT NULL, letter_count INTEGER NOT NULL, mark_count INTEGER NOT NULL,
       UNIQUE(surah_id,ayah_id));
    CREATE TABLE tokens (token_id TEXT PRIMARY KEY, verse_id TEXT NOT NULL REFERENCES verses(verse_id),
       surah_id INTEGER NOT NULL, ayah_id INTEGER NOT NULL, global_ayah INTEGER NOT NULL,
       global_token INTEGER UNIQUE NOT NULL, token_in_ayah INTEGER NOT NULL, token_in_surah INTEGER NOT NULL,
       raw TEXT NOT NULL, nfc TEXT NOT NULL, plain TEXT NOT NULL, search TEXT NOT NULL,
       raw_start INTEGER NOT NULL, raw_end INTEGER NOT NULL, letter_count INTEGER NOT NULL,
       mark_count INTEGER NOT NULL, is_opening_basmala INTEGER NOT NULL);
    CREATE TABLE spans (span_id TEXT PRIMARY KEY, verse_id TEXT NOT NULL REFERENCES verses(verse_id),
       kind TEXT NOT NULL, token_id TEXT, raw TEXT NOT NULL, raw_start INTEGER NOT NULL, raw_end INTEGER NOT NULL);
    CREATE TABLE source_records (verse_id TEXT PRIMARY KEY REFERENCES verses(verse_id), source_line INTEGER,
       line_start INTEGER, line_end INTEGER, line_ending TEXT);
    CREATE TABLE surahs (surah_id INTEGER PRIMARY KEY, ayah_count INTEGER, token_count INTEGER,
       numbered_token_count INTEGER, first_verse_id TEXT, last_verse_id TEXT);
    CREATE TABLE profiles (profile_id TEXT PRIMARY KEY, description_ru TEXT NOT NULL);
    CREATE TABLE token_scopes (token_id TEXT REFERENCES tokens(token_id), scope TEXT NOT NULL,
       scope_token_index INTEGER NOT NULL, scope_token_in_ayah INTEGER NOT NULL, scope_token_in_surah INTEGER NOT NULL,
       PRIMARY KEY(token_id,scope), UNIQUE(scope,scope_token_index));
    CREATE TABLE vocabulary (unit_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, scope TEXT NOT NULL,
       form TEXT NOT NULL, frequency INTEGER NOT NULL, rank INTEGER NOT NULL, share REAL NOT NULL,
       cumulative_share REAL NOT NULL, ayah_count INTEGER NOT NULL, surah_count INTEGER NOT NULL,
       first_token_id TEXT NOT NULL, last_token_id TEXT NOT NULL, first_scope_token INTEGER NOT NULL,
       last_scope_token INTEGER NOT NULL, mean_normalized_position REAL NOT NULL,
       UNIQUE(profile_id,scope,form));
    CREATE TABLE occurrences (unit_id TEXT NOT NULL REFERENCES vocabulary(unit_id), token_id TEXT NOT NULL REFERENCES tokens(token_id),
       scope_token_index INTEGER NOT NULL, PRIMARY KEY(unit_id,token_id));
    CREATE INDEX tokens_verse ON tokens(verse_id);
    CREATE INDEX tokens_surah ON tokens(surah_id);
    CREATE INDEX tokens_plain ON tokens(plain);
    CREATE INDEX tokens_search ON tokens(search);
    CREATE INDEX spans_verse ON spans(verse_id);
    CREATE INDEX occurrences_token ON occurrences(token_id);
    ''')
    vfields = ['verse_id','surah_id','ayah_id','global_ayah','raw_text','nfc','plain','search','raw_start','raw_end','token_count','letter_count','mark_count']
    tfields = ['token_id','verse_id','surah_id','ayah_id','global_ayah','global_token','token_in_ayah','token_in_surah','raw','nfc','plain','search','raw_start','raw_end','letter_count','mark_count','is_opening_basmala']
    _sql_insert(conn, 'verses', verses, vfields)
    _sql_insert(conn, 'tokens', tokens, tfields)
    _sql_insert(conn, 'spans', spans, ['span_id','verse_id','kind','token_id','raw','raw_start','raw_end'])
    _sql_insert(conn, 'source_records', verses, ['verse_id','source_line','line_start','line_end','line_ending'])
    by_surah = defaultdict(list)
    for v in verses:
        by_surah[v['surah_id']].append(v)
    opening_counts = Counter(t['surah_id'] for t in tokens if t['is_opening_basmala'])
    conn.executemany('INSERT INTO surahs VALUES (?,?,?,?,?,?)',
        [(s,len(vs),sum(v['token_count'] for v in vs),sum(v['token_count'] for v in vs)-opening_counts[s],
          vs[0]['verse_id'],vs[-1]['verse_id']) for s,vs in by_surah.items()])
    conn.executemany('INSERT INTO profiles VALUES (?,?)',[
        ('raw','Точный исходный текст; редакторские самостоятельные знаки не являются токенами.'),
        ('nfc','Только Unicode NFC; огласовки сохранены.'),
        ('plain','NFC; удалены только явно перечисленные огласовки, надстрочный алиф, татвиль и редакторские знаки.'),
        ('search','plain; أإآٱ → ا, ى → ي; ة сохранена.')])
    conn.commit()
    return conn


def quantile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    pos = (len(ordered)-1)*p
    lo = math.floor(pos)
    return ordered[lo] + (ordered[min(lo+1,len(ordered)-1)]-ordered[lo])*(pos-lo)


def distribution_summary(values):
    return {'count': len(values), 'sum': sum(values), 'min': min(values) if values else None,
            'q05': quantile(values,.05), 'q25': quantile(values,.25), 'median': quantile(values,.5),
            'q75': quantile(values,.75), 'q95': quantile(values,.95), 'max': max(values) if values else None,
            'mean': statistics.mean(values) if values else None,
            'population_sd': statistics.pstdev(values) if values else None}


def run(root: Path, config: dict) -> dict:
    root = Path(root)
    src = root / config.get('input', config.get('input_file', 'Quran_text.txt'))
    if not src.exists():
        raise FileNotFoundError(src)
    tables = root / 'results/tables'
    tables.mkdir(parents=True, exist_ok=True)
    blob = src.read_bytes()
    raw_copy = root / 'data/raw/Quran_text.txt'
    raw_copy.parent.mkdir(parents=True, exist_ok=True)
    raw_copy.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (root/'data/raw/SHA256SUMS').write_text(f'{digest}  Quran_text.txt\n', encoding='ascii')
    text, verses, issues = parse_source(blob)
    if any(x['status'] == 'blocking' for x in issues):
        write_csv(tables/'core_quality_issues.csv', issues)
        raise ValueError('Source has blocking parse/identifier defects; see core_quality_issues.csv')
    tokens, spans, basmala = tokenize(verses)
    verse_lookup = {v['verse_id']:v for v in verses}
    counts_by_surah = Counter(v['surah_id'] for v in verses)
    actual_surahs = sorted(counts_by_surah)
    gaps = []
    for s in actual_surahs:
        present = {v['ayah_id'] for v in verses if v['surah_id']==s}
        gaps += [f'{s}:{a}' for a in range(1,max(present)+1) if a not in present]
    missing_surahs = sorted(set(range(1,max(actual_surahs)+1))-set(actual_surahs))
    for key in gaps:
        issues.append({'line_number':'','issue':'missing_internal_ayah','raw':key,'impact_ru':'Неполное покрытие нумерации.','status':'warning'})
    if missing_surahs:
        issues.append({'line_number':'','issue':'missing_surah','raw':str(missing_surahs),'impact_ru':'Пропуск в нумерации сур.','status':'warning'})
    issues += [
        {'line_number':'','issue':'provenance_unverified','raw':'','impact_ru':f'Происхождение, версия, чтение и традиция нумерации не подтверждены; {len(actual_surahs)} сур и непрерывные номера не доказывают тождество внешнему эталону.','status':'documented'},
        {'line_number':'','issue':'editorial_standalone_spans','raw':str(sum(s['kind']=='editorial' for s in spans)),
         'impact_ru':'Самостоятельные знаки сохранены как spans и кодовые точки, исключены из орфографических токенов.','status':'handled'},
        {'line_number':'','issue':'opening_basmala_prefixes','raw':str(sum(t['is_opening_basmala'] for t in tokens)//4),
         'impact_ru':'Основная область file сохраняет префиксы; numbered исключает только начальные четыре токена вне сур 1 и 9, совпадающие с 1:1 в plain. В 95:1 и 97:1 первый токен имеет дополнительную шадду.','status':'handled'}]
    reconstructed = ''.join(text[v['line_start']:v['line_end']] for v in verses)
    by_verse_spans = defaultdict(list)
    for span in spans:
        by_verse_spans[span['verse_id']].append(span)
    checks = {
        'raw_copy_sha256_matches': hashlib.sha256(raw_copy.read_bytes()).hexdigest()==digest,
        'all_source_records_conserved': reconstructed==text,
        'all_verse_offsets_roundtrip': all(text[v['raw_start']:v['raw_end']]==v['raw_text'] for v in verses),
        'all_token_offsets_roundtrip': all(text[t['raw_start']:t['raw_end']]==t['raw'] for t in tokens),
        'all_spans_roundtrip': all(text[s['raw_start']:s['raw_end']]==s['raw'] for s in spans),
        'all_verses_reconstruct_from_spans': all(''.join(s['raw'] for s in by_verse_spans[v['verse_id']])==v['raw_text'] for v in verses),
        'nfc_idempotent': all(normalize(t['nfc'],'nfc')==t['nfc'] for t in tokens),
        'nfc_canonical_equivalence': all(ud.normalize('NFD',t['raw'])==ud.normalize('NFD',t['nfc']) for t in tokens),
        'no_duplicate_identifiers': len(verse_lookup)==len(verses),
        'no_internal_numbering_gaps': not gaps and not missing_surahs,
        'basmala_reference_valid': basmala['reference_valid'],
        'basmala_1_1_retained': not any(t['is_opening_basmala'] for t in tokens if t['verse_id']=='1:1'),
        'basmala_9_1_not_removed': not any(t['is_opening_basmala'] for t in tokens if t['verse_id']=='9:1'),
        'basmala_27_30_retained': not any(t['is_opening_basmala'] for t in tokens if t['verse_id']=='27:30'),
    }
    if not all(checks.values()):
        dump_json(root/'results/core_checks.json',checks)
        raise AssertionError('Core conservation check failed')
    conn = create_database(root/'data/processed/corpus.sqlite',verses,tokens,spans)
    write_csv(root/'data/processed/verses.csv.gz', verses)
    write_csv(root/'data/processed/tokens.csv.gz', tokens)
    write_csv(root/'data/processed/nonlexical_spans.csv.gz', (s for s in spans if s['kind']!='token'))
    # Entire source inventory includes metadata separately from verse text and tokens.
    source_cp = Counter(text)
    verse_cp = Counter(''.join(v['raw_text'] for v in verses))
    token_cp = Counter(''.join(t['raw'] for t in tokens))
    cp_examples = defaultdict(list)
    for v in verses:
        for c in set(v['raw_text']):
            if len(cp_examples[c])<3:
                cp_examples[c].append(v['verse_id'])
    inventory=[]
    for c,n in sorted(source_cp.items(), key=lambda x:ord(x[0])):
        if c in REMOVED:
            handling = 'Удаляется только в plain/search; в raw/nfc сохраняется.'
        elif c in 'أإآٱى':
            handling = 'Сохраняется в raw/nfc/plain; объединяется только в search.'
        elif c in '\u0653\u0654\u0655':
            handling = 'Хамза/мадда сохраняется; NFC может канонически скомпоновать с основой.'
        elif c in '|0123456789\n\r':
            handling = 'Метаданные или разделитель записей; не является текстом аята.'
        elif c.isspace():
            handling = 'Граница токена; сохраняется в тексте и spans.'
        else:
            handling = 'Сохраняется; NFC только каноническая нормализация.'
        inventory.append({'character':c,'display':repr(c) if c.isspace() else c,'codepoint':f'U+{ord(c):04X}',
            'unicode_name':ud.name(c,'UNNAMED'),'unicode_category':ud.category(c),'source_count':n,
            'verse_text_count':verse_cp[c],'token_count':token_cp[c],'examples_verse_ids':';'.join(cp_examples[c]),
            'is_base_letter':int(is_letter(c)),'rule_ru':handling})
    write_csv(tables/'core_unicode_inventory.csv',inventory)
    write_csv(tables/'core_quality_issues.csv',issues)
    write_csv(tables/'core_basmala_locations.csv',[
        {'verse_id':v['verse_id'],'surah_id':v['surah_id'],'ayah_id':v['ayah_id'],
         'raw_text':v['raw_text'],'kind':('numbered_first_verse' if v['verse_id']=='1:1' else
          'internal_retained' if v['verse_id']=='27:30' else 'absent_at_tawba' if v['verse_id']=='9:1' else 'opening_prefix'),
         'excluded_tokens_numbered':sum(t['is_opening_basmala'] for t in tokens if t['verse_id']==v['verse_id'])}
        for v in verses if v['ayah_id']==1 or v['verse_id']=='27:30'])
    # Quantities are surface metrics; no lemma/root inference is attempted here.
    scopes={s:[t for t in tokens if s=='file' or not t['is_opening_basmala']] for s in SCOPES}
    scope_indices={s:{t['token_id']:i+1 for i,t in enumerate(ts)} for s,ts in scopes.items()}
    for scope,ts in scopes.items():
        ayah_pos=Counter(); surah_pos=Counter(); scope_rows=[]
        for i,t in enumerate(ts,1):
            ayah_pos[t['verse_id']]+=1; surah_pos[t['surah_id']]+=1
            scope_rows.append((t['token_id'],scope,i,ayah_pos[t['verse_id']],surah_pos[t['surah_id']]))
        conn.executemany('INSERT INTO token_scopes VALUES (?,?,?,?,?)',scope_rows)
    vocab_summaries=[]; all_vocab=[]; frequency_hist=[]; surah_vocabulary=[]
    form_metrics = {p:{} for p in PROFILES}
    for p in PROFILES:
        for form in {t[p] for t in tokens}:
            form_metrics[p][form]=metrics(form)
    for profile in PROFILES:
        for scope,ts in scopes.items():
            groups=defaultdict(list)
            for t in ts:
                groups[t[profile]].append(t)
            ordered=sorted(groups.items(),key=lambda kv:(-len(kv[1]),kv[0]))
            cumulative=0; rows=[]
            for rank,(form,occ) in enumerate(ordered,1):
                cumulative+=len(occ)
                positions=[scope_indices[scope][t['token_id']] for t in occ]
                stable_id=hashlib.sha256(f'{profile}\0{scope}\0{form}'.encode()).hexdigest()[:24]
                rows.append({'unit_id':f'{profile}:{scope}:{stable_id}','profile_id':profile,'scope':scope,
                    'form':form,'frequency':len(occ),'rank':rank,'share':len(occ)/len(ts),
                    'cumulative_share':cumulative/len(ts),'ayah_count':len({t['verse_id'] for t in occ}),
                    'surah_count':len({t['surah_id'] for t in occ}),'first_token_id':occ[0]['token_id'],
                    'last_token_id':occ[-1]['token_id'],'first_scope_token':positions[0],'last_scope_token':positions[-1],
                    'mean_normalized_position':statistics.mean((i-1)/(len(ts)-1) for i in positions) if len(ts)>1 else 0.0,
                    **form_metrics[profile][form]})
            fields=['unit_id','profile_id','scope','form','frequency','rank','share','cumulative_share','ayah_count','surah_count','first_token_id','last_token_id','first_scope_token','last_scope_token','mean_normalized_position']
            _sql_insert(conn,'vocabulary',rows,fields)
            conn.executemany('INSERT INTO occurrences VALUES (?,?,?)',
                ((r['unit_id'],t['token_id'],scope_indices[scope][t['token_id']]) for r in rows for t in groups[r['form']]))
            write_csv(tables/f'core_vocabulary_{profile}_{scope}.csv.gz',rows)
            all_vocab+=rows
            freq=Counter(len(g) for g in groups.values())
            frequency_hist += [{'profile_id':profile,'scope':scope,'frequency':n,'types':k,'token_mass':n*k} for n,k in sorted(freq.items())]
            vocab_summaries.append({'profile_id':profile,'scope':scope,'tokens':len(ts),'types':len(groups),
                'hapax_types':freq[1],'dislegomena_types':freq[2],'types_in_one_surah':sum(r['surah_count']==1 for r in rows),
                'types_in_all_surahs':sum(r['surah_count']==len(actual_surahs) for r in rows),'type_token_ratio':len(groups)/len(ts)})
            global_count=Counter(t[profile] for t in ts)
            by_surah=defaultdict(Counter)
            for t in ts:
                by_surah[t['surah_id']][t[profile]]+=1
            for s,counts in by_surah.items():
                local_n=sum(counts.values())
                for form,n in counts.items():
                    outside_count=global_count[form]-n; outside_n=len(ts)-local_n
                    # Jeffreys additive smoothing; a descriptive specificity score, no p-value.
                    log_odds=math.log((n+.5)/(local_n-n+.5))-math.log((outside_count+.5)/(outside_n-outside_count+.5))
                    surah_vocabulary.append({'profile_id':profile,'scope':scope,'surah_id':s,'form':form,
                        'frequency':n,'surah_tokens':local_n,'per_1000_tokens':1000*n/local_n,
                        'global_frequency':global_count[form],'exclusive_to_surah':int(global_count[form]==n),
                        'log_odds_vs_remainder_smoothed':log_odds})
    conn.commit()
    write_csv(tables/'core_vocabulary_summary.csv',vocab_summaries)
    write_csv(tables/'core_frequency_of_frequencies.csv',frequency_hist)
    write_csv(tables/'core_surah_vocabulary.csv.gz',surah_vocabulary)
    # Profile comparisons enumerate every collision, including canonical NFC collisions.
    collisions=[]
    for src_profile,dst_profile in [('raw','nfc'),('nfc','plain'),('plain','search'),('raw','plain'),('raw','search')]:
        maps=defaultdict(Counter); example={}
        for t in tokens:
            maps[t[dst_profile]][t[src_profile]]+=1
            example.setdefault((t[dst_profile],t[src_profile]),t['token_id'])
        for form,variants in maps.items():
            if len(variants)>1:
                for variant,n in sorted(variants.items()):
                    collisions.append({'from_profile':src_profile,'to_profile':dst_profile,'normalized_form':form,
                        'source_form':variant,'frequency':n,'variants':len(variants),'example_token_id':example[(form,variant)]})
    write_csv(tables/'core_normalization_collisions.csv.gz',collisions)
    # All token metrics and all verse/surah lengths for all profiles and both scopes.
    token_metrics=[]; verse_lengths=[]; surah_lengths=[]; distribution_rows=[]; extrema=[]; summary_rows=[]
    surface_text_lengths=[]
    tokens_per_verse=defaultdict(list)
    for t in tokens:
        tokens_per_verse[t['verse_id']].append(t)
    for profile in PROFILES:
        for t in tokens:
            m=form_metrics[profile][t[profile]]
            token_metrics.append({'profile_id':profile,'token_id':t['token_id'],'verse_id':t['verse_id'],
                'is_opening_basmala':t['is_opening_basmala'],**m,
                'mark_letter_ratio':m['marks']/m['letters'] if m['letters'] else '',
                'harakat_letter_ratio':m['harakat']/m['letters'] if m['letters'] else ''})
        for scope,ts in scopes.items():
            rows=[]
            for v in verses:
                selected=[t for t in tokens_per_verse[v['verse_id']] if scope=='file' or not t['is_opening_basmala']]
                raw_surface = v['raw_text']
                prefix = [t for t in tokens_per_verse[v['verse_id']] if t['is_opening_basmala']]
                if scope == 'numbered' and prefix:
                    # Remove only the anchored prefix and its following separator;
                    # preserve all later whitespace and standalone editorial signs.
                    raw_surface = raw_surface[prefix[-1]['raw_end']-v['raw_start']:].lstrip()
                surface_text_lengths.append({'profile_id':profile,'scope':scope,'verse_id':v['verse_id'],
                    'surah_id':v['surah_id'],'ayah_id':v['ayah_id'],
                    **metrics(normalize(raw_surface, profile))})
                counts=Counter({k:0 for k in ('codepoints','graphemes','letters','marks','harakat','editorial','tatweel','superscript_alef')})
                for t in selected:
                    counts.update(form_metrics[profile][t[profile]])
                row={'profile_id':profile,'scope':scope,'verse_id':v['verse_id'],'surah_id':v['surah_id'],
                    'ayah_id':v['ayah_id'],'global_ayah':v['global_ayah'],'tokens':len(selected),
                    'unique_forms':len({t[profile] for t in selected}),**counts}
                rows.append(row)
            verse_lengths+=rows
            by_surah=defaultdict(list)
            for row in rows:
                by_surah[row['surah_id']].append(row)
            for s,vs in by_surah.items():
                st=[t for t in ts if t['surah_id']==s]
                surah_lengths.append({'profile_id':profile,'scope':scope,'surah_id':s,'ayahs':len(vs),
                    'tokens':sum(v['tokens'] for v in vs),'unique_forms':len({t[profile] for t in st}),
                    **{k:sum(v[k] for v in vs) for k in counts}})
            token_items=[{'unit_id':t['token_id'],**form_metrics[profile][t[profile]]} for t in ts]
            surah_items=[v for v in surah_lengths if v['profile_id']==profile and v['scope']==scope]
            for kind,items,metrics_list,id_key in [('token',token_items,['letters','marks','codepoints','graphemes'],'unit_id'),
                ('verse',rows,['tokens','letters','marks','codepoints','graphemes','unique_forms'],'verse_id'),
                ('surah',surah_items,['ayahs','tokens','letters','marks','codepoints','graphemes','unique_forms'],'surah_id')]:
                for metric in metrics_list:
                    values=[i[metric] for i in items]
                    stats=distribution_summary(values)
                    summary_rows.append({'profile_id':profile,'scope':scope,'unit':kind,'metric':metric,**stats})
                    distribution_rows += [{'profile_id':profile,'scope':scope,'unit':kind,'metric':metric,'value':n,'frequency':f}
                                          for n,f in sorted(Counter(values).items())]
                    extrema += [{'profile_id':profile,'scope':scope,'unit':kind,'metric':metric,'extreme':extreme,
                                 'unit_id':str(i[id_key]),'value':i[metric]}
                                for extreme in ['min','max'] for i in items if i[metric]==stats[extreme]]
    write_csv(tables/'core_token_metrics.csv.gz',token_metrics)
    write_csv(tables/'core_verse_lengths.csv.gz',verse_lengths)
    write_csv(tables/'core_surface_text_lengths.csv.gz',surface_text_lengths)
    write_csv(tables/'core_surah_lengths.csv',surah_lengths)
    write_csv(tables/'core_length_distributions.csv.gz',distribution_rows)
    write_csv(tables/'core_length_summary.csv',summary_rows)
    write_csv(tables/'core_length_extrema_all_ties.csv.gz',extrema)
    # Exhaustive combining-mark sequences, and their examples, are descriptive orthography diagnostics.
    mark_sequences=defaultdict(list); unusual=[]
    for t in tokens:
        for match in re.finditer(r'[\u064B-\u065F\u0670\u06D6-\u06ED]+',t['raw']):
            seq=match.group(); mark_sequences[seq].append(t['token_id'])
            reasons=[]
            if len(set(seq))!=len(seq): reasons.append('repeated_identical_mark')
            if match.start()==0: reasons.append('leading_combining_mark')
            vowels=[c for c in seq if c in set(map(chr,range(0x064B,0x0651)))]
            if len(vowels)>1: reasons.append('multiple_vocalization_marks')
            if reasons:
                unusual.append({'token_id':t['token_id'],'raw':t['raw'],'sequence':seq,
                                'codepoints':' '.join(f'U+{ord(c):04X}' for c in seq),'reason':';'.join(reasons)})
    write_csv(tables/'core_mark_sequences.csv',[{'sequence':seq,'codepoints':' '.join(f'U+{ord(c):04X}' for c in seq),
        'frequency':len(occ),'token_count':len(set(occ)),'example_token_ids':';'.join(dict.fromkeys(occ))[:150]}
        for seq,occ in sorted(mark_sequences.items(),key=lambda kv:(-len(kv[1]),kv[0]))])
    write_csv(tables/'core_unusual_mark_combinations.csv',unusual,
              fields=['token_id','raw','sequence','codepoints','reason'])
    # Counts of each actual character under each profile/scope, with distinct containing-unit counts.
    char_rows=[]
    for profile in PROFILES:
        for scope,ts in scopes.items():
            info={}
            for t in ts:
                for ch,n in Counter(t[profile]).items():
                    d=info.setdefault(ch,{'count':0,'tokens':set(),'ayahs':set(),'surahs':set(),'first':t['token_id'],'last':t['token_id']})
                    d['count']+=n; d['tokens'].add(t['token_id']); d['ayahs'].add(t['verse_id']); d['surahs'].add(t['surah_id']); d['last']=t['token_id']
            denominator=sum(d['count'] for d in info.values())
            cumulative=0
            for rank,(ch,d) in enumerate(sorted(info.items(),key=lambda kv:(-kv[1]['count'],kv[0])),1):
                cumulative+=d['count']
                char_rows.append({'profile_id':profile,'scope':scope,'character':ch,'codepoint':f'U+{ord(ch):04X}',
                    'kind':'letter' if is_letter(ch) else 'mark' if ud.category(ch).startswith('M') else 'other',
                    'frequency':d['count'],'rank':rank,'share_all_token_codepoints':d['count']/denominator,
                    'denominator_all_token_codepoints':denominator,
                    'cumulative_share':cumulative/denominator,'token_count':len(d['tokens']),'ayah_count':len(d['ayahs']),
                    'surah_count':len(d['surahs']),'first_token_id':d['first'],'last_token_id':d['last']})
    write_csv(tables/'core_character_frequencies.csv',char_rows)
    # Byte offsets are deliberately separate from the canonical code-point offsets.
    byte_offsets=[0]
    for char in text:
        byte_offsets.append(byte_offsets[-1]+len(char.encode('utf-8')))
    write_csv(tables/'core_source_offsets_bytes.csv.gz',({'token_id':t['token_id'],'codepoint_start':t['raw_start'],
        'codepoint_end':t['raw_end'],'byte_start':byte_offsets[t['raw_start']],'byte_end':byte_offsets[t['raw_end']]}
        for t in tokens))
    sqlite_checks={'integrity_check':conn.execute('PRAGMA integrity_check').fetchone()[0],
        'foreign_key_violations':len(conn.execute('PRAGMA foreign_key_check').fetchall()),
        'occurrences':conn.execute('SELECT count(*) FROM occurrences').fetchone()[0]}
    conn.close()
    assert sqlite_checks['integrity_check']=='ok' and sqlite_checks['foreign_key_violations']==0
    surface_counts=[{'profile_id':p,'scope':s,'tokens':len(ts),
                    **{k:sum(form_metrics[p][t[p]][k] for t in ts) for k in metrics('')}}
                   for p in PROFILES for s,ts in scopes.items()]
    write_csv(tables/'core_basmala_sensitivity.csv',surface_counts)
    summary={
        'module':'core','version':VERSION,'input_file':str(src.relative_to(root)),'sha256':digest,
        'encoding':'UTF-8 strict, no BOM','format':'surah_id|ayah_id|Arabic text; one record per line',
        'source_bytes':len(blob),'source_codepoints':len(text),'source_lines':len(text.splitlines()),
        'verse_text_codepoints':sum(len(v['raw_text']) for v in verses),
        'surahs':len(actual_surahs),'ayahs':len(verses),'tokens_file':len(tokens),
        'tokens_numbered':len(scopes['numbered']),'opening_basmala_tokens':len(tokens)-len(scopes['numbered']),
        'opening_basmala_surahs':len({t['surah_id'] for t in tokens if t['is_opening_basmala']}),
        'standalone_editorial_spans':sum(s['kind']=='editorial' for s in spans),
        'unique_codepoints':len(source_cp),'nfc_changed_tokens':sum(t['raw']!=t['nfc'] for t in tokens),
        'unexpected_non_arabic_letters':sorted({c for c in verse_cp if is_letter(c) and not regex.fullmatch(r'\p{Arabic}',c)}),
        'presentation_forms':sorted({c for c in verse_cp if 0xFB50<=ord(c)<=0xFDFF or 0xFE70<=ord(c)<=0xFEFF}),
        'numbering_gaps':gaps,'missing_surahs':missing_surahs,'provenance_ru':'Не установлено по самому файлу.',
        'orthography_ru':'В файле есть огласовки, надстрочный алиф, татвиль и коранические редакторские знаки. Конкретная редакция, чтение и традиция нумерации не установлены.',
        'completeness_ru':f'Все {len(blob)} байт и {len(verses)} записей обработаны; {len(actual_surahs)} последовательно пронумерованных сур без внутренних пропусков. Тождество полному внешнему эталону не подтверждено.',
        'length_basis_ru':'surface_counts и core_verse_lengths: сумма метрик орфографических токенов, без пробелов и самостоятельных редакторских знаков; core_surface_text_lengths: полный текст аята с пробелами и редакторскими знаками.',
        'profiles':list(PROFILES),'scopes':list(SCOPES),'surface_counts':surface_counts,'vocabulary':vocab_summaries,
        'unusual_mark_combination_occurrences':len(unusual),'checks':checks,'sqlite_checks':sqlite_checks,
        'software':{'unicode_version':ud.unidata_version,'regex_version':regex.__version__},
    }
    dump_json(root/'results/core_summary.json',summary)
    dump_json(root/'results/core_checks.json',{**checks,**sqlite_checks})
    coverage=[{'direction':'A','analysis':'Полный аудит, Unicode, длины и огласовки всех единиц',
        'status':'complete','universe':'Все строки, кодовые точки, токены, аяты и суры; 4 профиля × 2 области',
        'tested':f'{len(verses)} аятов; {len(tokens)} токенов; {len(source_cp)} кодовых точек',
        'limitations':'Полнота относительно внешнего эталона и происхождение не установлены; письменные буквы не являются звуками.'},
        {'direction':'B','analysis':'Полные поверхностные словари, частоты частот и словарь каждой суры',
        'status':'complete','universe':'Каждая наблюдаемая токен-форма; raw/nfc/plain/search; file/numbered',
        'tested':len(all_vocab),'limitations':'Это орфографические формы, не леммы; морфология в отдельном модуле. Специфичность — описательный сглаженный log-odds.'}]
    dump_json(root/'results/core_coverage.json',coverage)
    findings=[{'hypothesis_id':'core_basmala_sensitivity','family':'corpus_audit','title_ru':'Вступительные басмалы меняют поверхностные частоты',
        'status':'descriptive','profile_id':'raw','scope':'file vs numbered','observed':{'file':len(tokens),'numbered':len(scopes['numbered']),
        'difference_tokens':len(tokens)-len(scopes['numbered']),'opening_surahs':summary['opening_basmala_surahs']},
        'method_ru':'Распознавание только первых четырёх токенов, совпадающих с 1:1 в plain, у первых аятов сур, кроме 1 и 9; внутренний текст 27:30 сохранён. 95:1 и 97:1 имеют дополнительную шадду в raw.',
        'limitation_ru':'Альтернативная область является документированным аналитическим соглашением; исходные номера аятов не меняются.',
        'evidence_path':'results/tables/core_basmala_sensitivity.csv','p_value':None},
        {'hypothesis_id':'core_normalization_changes_vocabulary','family':'normalization','title_ru':'Нормализация меняет число различаемых письменных форм',
        'status':'descriptive','profile_id':'raw/nfc/plain/search','scope':'file','observed':{r['profile_id']:r['types'] for r in vocab_summaries if r['scope']=='file'},
        'method_ru':'Исчерпывающий словарь каждого профиля и полная таблица коллизий между профилями.',
        'limitation_ru':'Объединённая форма не является доказательством совпадения леммы или значения.',
        'evidence_path':'results/tables/core_normalization_collisions.csv.gz','p_value':None}]
    dump_json(root/'results/hypotheses/core.json',findings)
    return summary


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    args=parser.parse_args()
    result=run(args.root,{})
    print(json.dumps({k:result[k] for k in ['sha256','surahs','ayahs','tokens_file','tokens_numbered']},ensure_ascii=False))
