"""External QAC enrichment. The user's corpus remains the sole analysed text.

No predictive morphology: only annotated QAC segments whose surface group has
been aligned under the explicit qac_alignment_v1 profile are admitted.
"""
from __future__ import annotations
import csv
import gzip
import hashlib
import json
import math
import re
import sqlite3
import unicodedata as ud
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import combinations
from pathlib import Path

QAC_SHA256 = 'a1d12923815341face765083805d2148ed2d9f5cc3f7d6665219d887675d8c46'
BW = dict(zip("'>&<}AbptvjHxd*rzs$SDTZEg_fqklmnhwYyFNKaui~o`{", "ءأؤإئابةتثجحخدذرزسشصضطظعغـفقكلمنهوىيًٌٍَُِّْٰٱ"))
BW.update({'|':'آ','^':'\u0653','#':'\u0654',':':'\u06dc','@':'\u06df','"':'\u06e0','[':'\u06e2',';':'\u06e3',',':'\u06e5','.':'\u06e6','!':'\u06e8','-':'\u06ea','+':'\u06eb','%':'\u06ec',']':'\u06ed'})
BW.update({str(i): str(i) for i in range(10)})  # Lexeme disambiguation suffixes, not Arabic letters.
BW[' '] = ' '  # QAC 37:130:3 is the single externally indexed form <ilo yaAsiyna.
PROFILES = ('raw','nfc','plain','search')


def bw_arabic(text: str) -> str:
    unknown = set(text) - set(BW)
    if unknown:
        raise ValueError(f'Unmapped Buckwalter characters: {unknown!r}')
    return ''.join(BW[x] for x in text)


@lru_cache(maxsize=150000)
def alignment_keys(text: str) -> frozenset[str]:
    """Alignment only, never frequency normalization; preserve hamza and taa.

    Remove harakat, recitation marks, tatweel and maddah; alif-wasla -> alif;
    superscript alif can be absent or an ordinary alif. Hamza U+0654/55,
    taa-marbuta, alif-maqsura and ordinary letters remain distinct.
    """
    text = ud.normalize('NFD', text.replace('ٱ','ا'))
    keys = {''}
    for ch in text:
        c = ord(ch)
        if ch.isspace() or ch == 'ـ' or 0x064B <= c <= 0x0653 or 0x06D6 <= c <= 0x06ED:
            continue
        if ch == 'ٰ':
            keys = {s + x for s in keys for x in ('', 'ا')}
        else:
            keys = {s + ch for s in keys}
    return frozenset(ud.normalize('NFC', s) for s in keys)


def load_qac(path: Path) -> list[dict]:
    if hashlib.sha256(path.read_bytes()).hexdigest() != QAC_SHA256:
        raise ValueError('QAC file hash differs from the reviewed v0.4 artifact')
    segments = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.startswith('('):
            continue
        loc, form, pos, feats = line.split('\t')
        surah, ayah, word, segment = map(int, loc.strip('()').split(':'))
        parts = feats.split('|')
        features = {x.split(':',1)[0]: x.split(':',1)[1] for x in parts if ':' in x}
        lemma, root = features.get('LEM',''), features.get('ROOT','')
        segments.append({'segment_id':loc.strip('()'), 'qac_word_id':f'{surah}:{ayah}:{word}',
                         'verse_id':f'{surah}:{ayah}', 'surah_id':surah,'ayah_id':ayah,
                         'qac_token_index':word,'segment_index':segment,'segment_arabic':bw_arabic(form),
                         'segment_bw':form,'pos':pos,'segment_type':parts[0],
                         'lemma':bw_arabic(lemma),'lemma_bw':lemma,
                         'root':bw_arabic(root),'root_bw':root,'features':feats})
    ids=[s['segment_id'] for s in segments]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate external segment IDs')
    return segments


def align_verse(source: list[dict], words: list[dict]) -> list[dict]:
    """Global edit alignment, 1:1, 1:2, 2:1 groups, unique optimum required.

    Exact compatible groups cost 0, joined groups cost 1; mismatch costs 12;
    insertion/deletion costs 8. No fuzzy edit-distance label transfer.
    A verse with multiple equally optimal paths is withheld in full.
    """
    n,m = len(source),len(words)
    a=[s['raw'] for s in source]; b=[w['surface'] for w in words]
    ka={};kb={}
    for i in range(n):
        for k in (1,2):
            if i+k<=n:ka[i,k]=alignment_keys(''.join(a[i:i+k]))
    for j in range(m):
        for k in (1,2):
            if j+k<=m:kb[j,k]=alignment_keys(''.join(b[j:j+k]))
    dp=[[10**9]*(m+1) for _ in range(n+1)]
    count=[[0]*(m+1) for _ in range(n+1)]
    back={};dp[0][0]=0;count[0][0]=1
    for i in range(n+1):
        for j in range(m+1):
            if count[i][j]==0:continue
            edges=[]
            if i<n and j<m:
                edges.append((1,1,0 if ka[i,1]&kb[j,1] else 12))
                if i+2<=n and ka[i,2]&kb[j,1]:edges.append((2,1,1))
                if j+2<=m and ka[i,1]&kb[j,2]:edges.append((1,2,1))
            if i<n:edges.append((1,0,8))
            if j<m:edges.append((0,1,8))
            for u,v,cost in edges:
                ni,nj=i+u,j+v;new=dp[i][j]+cost
                if new<dp[ni][nj]:
                    dp[ni][nj]=new;count[ni][nj]=count[i][j];back[ni,nj]=(i,j,u,v,cost)
                elif new==dp[ni][nj]:count[ni][nj]=min(2,count[ni][nj]+count[i][j])
    if count[n][m]>1:
        return [{'qac_word_id':w['qac_word_id'],'token_ids':'','source_surface':' '.join(a),
                 'qac_surface':w['surface'],'status':'ambiguous_verse','rule':'multiple_optimal_paths',
                 'verse_id':w['verse_id']} for w in words]
    out=[];i,j=n,m
    while i or j:
        pi,pj,u,v,cost=back[i,j]
        toks=source[pi:pi+u];qw=words[pj:pj+v]
        verified=cost in (0,1)
        for w in reversed(qw) if qw else [None]:
            out.append({'qac_word_id':w['qac_word_id'] if w else '',
                        'token_ids':'|'.join(t['token_id'] for t in toks),
                        'source_surface':' '.join(t['raw'] for t in toks),
                        'qac_surface':w['surface'] if w else '',
                        'status':'verified' if verified else ('surface_mismatch' if u and v else 'unmatched'),
                        'rule':f'{u}:{v};qac_alignment_v1' if verified else f'{u}:{v};no_annotation_transferred',
                        'verse_id':(qw[0] if qw else toks[0])['verse_id']})
        i,j=pi,pj
    return list(reversed(out))


def write_csv(path: Path, rows: list[dict], fields=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=fields or (list(rows[0]) if rows else ['empty'])
    op=gzip.open if path.suffix=='.gz' else open
    with op(path,'wt',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def frequencies(segments: list[dict], field: str) -> list[dict]:
    groups=defaultdict(list)
    for s in segments:
        if s[field]:groups[s[field]].append(s)
    rows=[]; denominator=len(segments)
    annotated=sum(len(ss) for ss in groups.values())
    for value,ss in groups.items():
        rows.append({'unit':value,'unit_bw':ss[0].get(field+'_bw',''),
                     'segment_count':len(ss),
                     'token_count':len({t for s in ss for t in s['token_ids'].split('|')}),
                     'verse_count':len({s['verse_id'] for s in ss}),
                     'surah_count':len({s['surah_id'] for s in ss}),
                     'first_segment_id':ss[0]['segment_id'],
                     'last_segment_id':ss[-1]['segment_id'],
                     'first_global_token_file':min(s.get('first_global_token_file',0) for s in ss),
                     'last_global_token_file':max(s.get('last_global_token_file',0) for s in ss),
                     'all_verified_segments_denominator':denominator,
                     'nonempty_field_segments_denominator':annotated,
                     'share_of_verified_segments':len(ss)/denominator,
                     'share_of_nonempty_field_segments':len(ss)/annotated,
                     'scope':'numbered_aligned','profile_id':'qac_v0.4_aligned'})
    rows.sort(key=lambda x:(-x['segment_count'],x['unit']))
    running=0
    for rank,row in enumerate(rows,1):
        running+=row['segment_count']
        row.update(rank=rank,cumulative_share_of_nonempty_field_segments=running/annotated)
    return rows


def morph_catalogues(segments: list[dict], tokens: list[dict], tables: Path):
    """Form families and complete within-surah lexicons; each segment counted once."""
    token_map={t['token_id']:t for t in tokens}
    surface_groups=defaultdict(list);surah_groups=defaultdict(list)
    for s in segments:
        source_surface=' '.join(token_map[tid]['plain'] for tid in s['token_ids'].split('|'))
        for field in ('lemma','root'):
            if s[field]:
                surface_groups[field,s[field],source_surface].append(s)
                surah_groups[field,s[field],s['surah_id']].append(s)
    totals=Counter((field,unit) for field,unit,surah in surah_groups)
    forms=[]
    for (field,unit,surface),ss in surface_groups.items():
        forms.append({'unit_type':field,'unit':unit,'source_plain_group':surface,
                      'segment_count':len(ss),'qac_word_count':len({s['qac_word_id'] for s in ss}),
                      'source_token_count':len({t for s in ss for t in s['token_ids'].split('|')}),
                      'first_segment_id':ss[0]['segment_id'],'last_segment_id':ss[-1]['segment_id'],
                      'profile_id':'qac_v0.4_aligned+plain','scope':'numbered_aligned'})
    write_csv(tables/'morphology_form_families.csv',forms)
    surah_denominators=Counter((field,s['surah_id']) for s in segments for field in ('lemma','root') if s[field])
    rows=[]
    for (field,unit,surah),ss in surah_groups.items():
        denominator=surah_denominators[field,surah]
        rows.append({'surah_id':surah,'unit_type':field,'unit':unit,'segment_count':len(ss),
                     'source_token_count':len({t for s in ss for t in s['token_ids'].split('|')}),
                     'verse_count':len({s['verse_id'] for s in ss}),
                     'surahs_containing_unit':totals[field,unit],
                     'unique_to_surah':int(totals[field,unit]==1),
                     'nonempty_field_segments_in_surah':denominator,
                     'share_of_nonempty_field_segments_in_surah':len(ss)/denominator,
                     'profile_id':'qac_v0.4_aligned','scope':'numbered_aligned'})
    write_csv(tables/'morphology_surah_lexicons.csv.gz',rows)


def linguistic(segments: list[dict], tokens: list[dict], tables: Path, all_segments=None):
    families={'negation':lambda s:s['pos']=='NEG', 'vocative':lambda s:s['pos']=='VOC',
              'interrogative':lambda s:s['pos']=='INTG','imperative':lambda s:s['pos']=='V' and 'IMPV' in s['features'].split('|'),
              'proper_name':lambda s:s['pos']=='PN'}
    rows=[];findings=[]
    for family, pred in families.items():
        ss=[s for s in segments if pred(s)]
        for s in ss:rows.append({'family':family,**s})
        findings.append({'hypothesis_id':f'I-morph-{family}','family':'I','title_ru':{'negation':'Сегменты отрицания','vocative':'Обращения','interrogative':'Вопросительные сегменты','imperative':'Повелительные глаголы','proper_name':'Имена собственные'}[family],
                         'status':'описательное','profile_id':'qac_v0.4_aligned','scope':'numbered_aligned',
                         'observed':{'segments':len(ss),'tokens':len({t for s in ss for t in s['token_ids'].split('|')}),'verses':len({s['verse_id'] for s in ss})},
                         'method_ru':'Полный перебор выровненных сегментов по явным меткам QAC v0.4; формы и все адреса сохранены.',
                         'limitation_ru':'Это функция сегмента по внешней разметке, а не полное семантическое или синтаксическое толкование аята. Невыровненные токены исключены.',
                         'evidence_path':'results/tables/morphology_linguistic_occurrences.csv.gz','p_value':None})
    # Numeric lexical candidates, fixed root inventory; never conflated with numbers of verses.
    numeric_roots={'wHd','vny','vlv','rbE','xms','sds','sbE','vmn','tsE','E$r','mAy','Alf'}
    nums=[{'family':'numeric_root_candidate',**s} for s in segments if s['root_bw'] in numeric_roots]
    rows+=nums
    findings.append({'hypothesis_id':'I-numeric-root-candidates','family':'I','title_ru':'Кандидаты числовых выражений по 12 корням','status':'кандидат','profile_id':'qac_v0.4_aligned','scope':'numbered_aligned',
                     'observed':{'segments':len(nums),'root_inventory_bw':sorted(numeric_roots)},
                     'method_ru':'Фиксированный список 12 корней. Все сегменты сохранены для ручной проверки контекста.',
                     'limitation_ru':'Корень может выражать не число: единство, повторение, долю и другие значения. Сумма значений чисел и полный семантический каталог не заявляются.',
                     'evidence_path':'results/tables/morphology_linguistic_occurrences.csv.gz','p_value':None})
    write_csv(tables/'morphology_linguistic_occurrences.csv.gz',rows)
    # Adjacent verbs in the complete external sequence prevent a missing verb
    # from being silently bridged. These are grammatical candidates, not iltifat.
    aligned_map={s['segment_id']:s for s in segments}
    verbs=defaultdict(list)
    for s in all_segments if all_segments is not None else segments:
        if s['pos']=='V':verbs[s['verse_id']].append(s)
    transition_rows=[];eligible=0
    for vv in verbs.values():
        for left,right in zip(vv,vv[1:]):
            eligible+=1
            if left['segment_id'] not in aligned_map or right['segment_id'] not in aligned_map:continue
            grammar=[]
            for s in (left,right):
                grammar.append(next((f for f in s['features'].split('|') if re.fullmatch(r'[123][MF]?[SDP]',f)),''))
            transition_rows.append({'verse_id':left['verse_id'],'left_segment_id':left['segment_id'],
                                    'right_segment_id':right['segment_id'],
                                    'left_token_ids':aligned_map[left['segment_id']]['token_ids'],
                                    'right_token_ids':aligned_map[right['segment_id']]['token_ids'],
                                    'left_person_gender_number':grammar[0],'right_person_gender_number':grammar[1],
                                    'person_changes':int(bool(grammar[0] and grammar[1] and grammar[0][0]!=grammar[1][0])),
                                    'profile_id':'qac_v0.4_aligned','scope':'numbered_aligned'})
    write_csv(tables/'morphology_verb_transitions.csv',transition_rows)
    findings.append({'hypothesis_id':'I-verb-person-transition','family':'I','title_ru':'Смена грамматического лица соседних глаголов внутри аята',
                     'status':'кандидат','profile_id':'qac_v0.4_aligned','scope':'numbered_aligned',
                     'observed':{'external_adjacent_verb_pairs':eligible,'verified_pairs':len(transition_rows),
                                 'pairs_with_person_change':sum(r['person_changes'] for r in transition_rows)},
                     'method_ru':'Соседние глагольные основы в полной последовательности QAC одного аята; сохраняются только пары с обоими выровненными членами. Смена — различие первого знака признака лица/рода/числа.',
                     'limitation_ru':'Смена может быть обычной сменой субъекта, цитатой или обращением. Это кандидаты для чтения контекста, не автоматическая разметка риторического ильтифата. Пропущенные глаголы не перекрываются.',
                     'evidence_path':'results/tables/morphology_verb_transitions.csv','p_value':None})
    return findings


def cooccurrence(segments: list[dict], tokens: list[dict], tables: Path, top_n=60, min_support=5):
    """Exhaust top-N unordered lemma/root pairs in three binary context schemes."""
    byverse=defaultdict(list)
    segments_byverse=defaultdict(list)
    for segment in segments:segments_byverse[segment['verse_id']].append(segment)
    for t in tokens:
        if not t['is_opening_basmala']:byverse[t['verse_id']].append(t)
    out=[];counts={}
    for field in ('lemma','root'):
        frequencies_=Counter(s[field] for s in segments if s[field])
        units=[u for u,_ in sorted(frequencies_.items(),key=lambda x:(-x[1],x[0]))[:top_n]]
        allowed=set(units);membership=defaultdict(set)
        for s in segments:
            if s[field] in allowed:
                for tid in s['token_ids'].split('|'):membership[tid].add(s[field])
        contexts={'verse':[],'surah':[],'window5_within_verse':[]};surahs=defaultdict(set)
        for vv in byverse.values():
            members=set().union(*(membership[t['token_id']] for t in vv))
            contexts['verse'].append(members);surahs[vv[0]['surah_id']].update(members)
            local_index={t['token_id']:i for i,t in enumerate(vv)}
            # Admit a group-aligned annotation only when its entire source
            # token group belongs to the context, not just one overlapping token.
            local_groups=[]
            for s in segments_byverse[vv[0]['verse_id']]:
                if s[field] in allowed:
                    positions=[local_index[t] for t in s['token_ids'].split('|')]
                    local_groups.append((min(positions),max(positions),s[field]))
            for i in range(len(vv)-4):
                contexts['window5_within_verse'].append({u for lo,hi,u in local_groups if i<=lo and hi<i+5})
        contexts['surah']=list(surahs.values())
        for context,sets in contexts.items():
            marginal=Counter(u for ss in sets for u in ss)
            pairs=Counter(pair for ss in sets for pair in combinations(sorted(ss),2))
            denominator=len(sets)
            for a,b in combinations(sorted(units),2):
                both=pairs[a,b];ma,mb=marginal[a],marginal[b]
                lift=both*denominator/(ma*mb) if ma and mb else None
                out.append({'unit_type':field,'context':context,'unit_a':a,'unit_b':b,
                            'contexts_total':denominator,'contexts_a':ma,'contexts_b':mb,
                            'contexts_both':both,'meets_support':int(both>=min_support),
                            'lift':lift if both>=min_support else '',
                            'pmi_bits':math.log2(lift) if both>=min_support and lift else '',
                            'profile_id':'qac_v0.4_aligned','scope':'numbered_aligned'})
            counts[f'{field}/{context}']={'units':len(units),'pairs':len(units)*(len(units)-1)//2,'contexts':denominator}
    write_csv(tables/'morphology_cooccurrence.csv.gz',out)
    return {'top_n_per_level':top_n,'minimum_support_for_association':min_support,
            'pair_context_rows':len(out),'universes':counts,
            'limitation_ru':'Бинарное присутствие по выровненным сегментам. Окна из 5 исходных numbered-токенов перекрываются, не пересекают границу аята. Нулевая совместная частота сохраняется; PMI/lift показаны только при поддержке ≥5. Неполное выравнивание и длина контекста влияют на ассоциации; это описательные меры без p-value.'}


def published_claims(tokens: list[dict], all_segments: list[dict], aligned: list[dict], tables: Path):
    """Predefined inclusion rules and complete lists of included/excluded cases."""
    findings=[];claim_rows=[];occ=[]
    aligned_map={s['segment_id']:s for s in aligned}
    source_map={t['token_id']:t for t in tokens}
    qwords=defaultdict(list)
    for s in all_segments:qwords[s['qac_word_id']].append(s)
    claims=[('J-month-12','شهر',12,'$hr','month'),('J-day-365','يوم',365,'ywm','day')]
    url='https://www.masjidtucson.org/submission/youth/verifying_calendar_months_days.html'
    quote='In Quran: the word "month" occurs 12 times, and "day" 365 times'
    for cid,label,expected,root,kind in claims:
        candidates=[s for s in all_segments if s['root_bw']==root]
        selected=[];missing=[];counts=Counter();variants=Counter()
        for s in candidates:
            feats=s['features'].split('|')
            plural=any(re.fullmatch(r'[123]?[MF]?[DP]',f) for f in feats)
            pron_suffix=any(x['segment_type']=='SUFFIX' and x['pos']=='PRON' for x in qwords[s['qac_word_id']])
            include=not plural and (kind!='day' or not pron_suffix)
            reason='included_singular' if include else ('excluded_dual_plural' if plural else 'excluded_possessive_suffix')
            verified=s['segment_id'] in aligned_map
            counts[reason]+=1
            if include:
                (selected if verified else missing).append(s)
            mapped=aligned_map.get(s['segment_id'],{})
            raw=' '.join(source_map[t]['raw'] for t in mapped.get('token_ids','').split('|') if t)
            variants[reason,s['lemma'],raw or '[не выровнено]']+=1
            occ.append({'claim_id':cid,'segment_id':s['segment_id'],'qac_word_id':s['qac_word_id'],
                        'token_ids':mapped.get('token_ids',''),'verse_id':s['verse_id'],
                        'source_surface':raw,'qac_surface':''.join(x['segment_arabic'] for x in qwords[s['qac_word_id']]),
                        'lemma':s['lemma'],'features':s['features'],'include':int(include),'reason':reason,
                        'alignment_status':'verified' if verified else 'unverified'})
        status='воспроизведено при заданных правилах' if len(selected)==expected and not missing else ('частично: неполное выравнивание' if missing else 'не воспроизведено')
        method='Корень شهر; исключены двойственное и множественное число по QAC.' if kind=='month' else 'Корень يوم; исключены двойственное/множественное число и местоименные суффиксы. Все остальные единственные формы включены.'
        observation={'expected':expected,'verified_included':len(selected),'unverified_included':len(missing),'external_candidates':len(candidates),'external_categories':dict(counts)}
        add={'hypothesis_id':cid,'family':'J','title_ru':f'Публикация: {label} — {expected} вхождений','status':status,
             'profile_id':'qac_v0.4_aligned','scope':'numbered_aligned','observed':observation,
             'method_ru':method,'limitation_ru':'Правило исключения притяжательных форм относится только к опубликованному тесту, а не ко всем частотам слова «день». Неполное выравнивание не позволяет выдавать внешний итог за итог исходника.',
             'evidence_path':'results/tables/morphology_claim_occurrences.csv.gz','source_url':url,'source_quote':quote,'p_value':None}
        findings.append(add);claim_rows.append({**add,'observed':json.dumps(observation,ensure_ascii=False)})
    # Surface claims computed only on the supplied corpus, with every profile shown.
    basmala_by_profile={}
    for profile in PROFILES:
        target=[t[profile] for t in tokens if t['verse_id']=='1:1']
        for scope in ('file','numbered'):
            byverse=defaultdict(list)
            for t in tokens:
                if scope=='numbered' and t['is_opening_basmala']:continue
                byverse[t['verse_id']].append(t)
            hits=[]
            for vv in byverse.values():
                for i in range(len(vv)-3):
                    if [t[profile] for t in vv[i:i+4]]==target:
                        hits.append(vv[i:i+4]);occ.append({'claim_id':'J-basmala-114','segment_id':'','qac_word_id':'','token_ids':'|'.join(t['token_id'] for t in vv[i:i+4]),'verse_id':vv[i]['verse_id'],'source_surface':' '.join(t['raw'] for t in vv[i:i+4]),'qac_surface':'','lemma':'','features':profile,'include':1,'reason':scope,'alignment_status':'source_exact'})
            basmala_by_profile[f'{profile}/{scope}']=len(hits)
    v1=[t for t in tokens if t['verse_id']=='1:1']
    if len(v1)!=4:
        raise ValueError('Basmala source rule requires exactly four source tokens in 1:1')
    letter_count=sum(t['letter_count'] for t in v1)
    for t in v1:
        occ.append({'claim_id':'J-basmala-19','segment_id':'','qac_word_id':'',
                    'token_ids':t['token_id'],'verse_id':'1:1','source_surface':t['raw'],
                    'qac_surface':'','lemma':'','features':'written_letters_v1','include':1,
                    'reason':'file','alignment_status':'source_exact','source_letter_count':t['letter_count']})
    for cid,expected,observed,title,source,quote2,method in [
        ('J-basmala-114',114,basmala_by_profile,'Публикация: басмала встречается 114 раз','https://19.org/blog/prime/','The Basmalah occurs 114 times','Точное совпадение четырёх токенов с аятом 1:1 внутри аята, каждый профиль и две области подсчёта.'),
        ('J-basmala-19',19,letter_count,'Публикация: 19 букв в басмале','https://qurantalkblog.com/2023/07/16/bismillah-word-count/','This statement consists of exactly 19 letters','Сумма базовых письменных букв четырёх токенов 1:1 по спецификации корпуса; огласовки и татвил не считаются буквами.')]:
        stat='воспроизведено при заданных правилах' if (observed==expected or isinstance(observed,dict) and observed.get('plain/file')==expected) else 'не воспроизведено'
        add={'hypothesis_id':cid,'family':'J','title_ru':title,'status':stat,'profile_id':'plain' if isinstance(observed,dict) else 'written_letters_v1','scope':'file','observed':{'expected':expected,'observed':observed},'method_ru':method,
             'limitation_ru':'Результат зависит от области и определения письменной единицы; сам по себе не является статистическим свидетельством необычности.',
             'evidence_path':'results/tables/morphology_claim_occurrences.csv.gz','source_url':source,'source_quote':quote2,'p_value':None}
        findings.append(add);claim_rows.append({**add,'observed':json.dumps(add['observed'],ensure_ascii=False)})
    # Pair claim intentionally does not invent a semantic inclusion rule.
    pair=[]
    for target in ('d~unoyaA','A^xir'):
        ss=[s for s in all_segments if s['lemma_bw']==target]
        if not ss:
            raise ValueError(f'Published claim lemma key not present in QAC: {target}')
        good=[aligned_map[s['segment_id']] for s in ss if s['segment_id'] in aligned_map]
        pair.append({'lemma_bw':target,'lemma':bw_arabic(target),'external_segments':len(ss),'verified_segments':len(good),
                     'external_feminine_singular':sum('FS' in s['features'].split('|') for s in ss),
                     'verified_feminine_singular':sum('FS' in s['features'].split('|') for s in good)})
        for s in ss:
            mapped=aligned_map.get(s['segment_id'],{})
            occ.append({'claim_id':'J-world-hereafter','segment_id':s['segment_id'],'qac_word_id':s['qac_word_id'],'token_ids':mapped.get('token_ids',''),'verse_id':s['verse_id'],'source_surface':' '.join(source_map[t]['raw'] for t in mapped.get('token_ids','').split('|') if t),'qac_surface':''.join(x['segment_arabic'] for x in qwords[s['qac_word_id']]),'lemma':s['lemma'],'features':s['features'],'include':1,'reason':'fixed_lemma_all_contexts','alignment_status':'verified' if mapped else 'unverified'})
    add={'hypothesis_id':'J-world-hereafter','family':'J','title_ru':'Пара الدنيا / الآخرة: по 115','status':'недостаточно специфицировано','profile_id':'qac_v0.4_aligned','scope':'numbered_aligned','observed':{'expected':[115,115],'fixed_lemma_counts':pair},'method_ru':'Фиксированные ключи QAC d~unoyaA и A^xir: все формы и отдельно FS (женский род, единственное число). Полная лемма A^xir включает также мужские формы «последний». FS — явная грамматическая чувствительность, а не заявленное семантическое определение автора.','limitation_ru':'В публикации не дана полная формальная методика включения значений; внешние частоты не являются частотами исходника. Орфографические различия хамзы резко ограничивают выравнивание второй леммы. Это не опровержение всех возможных семантических определений.','evidence_path':'results/tables/morphology_claim_occurrences.csv.gz','source_url':'https://19.org/blog/prime/','source_quote':'The words “this world” (dunya) and “hereafter” (ahirah), each occur 115 times.','p_value':None}
    findings.append(add);claim_rows.append({**add,'observed':json.dumps(add['observed'],ensure_ascii=False)})
    for row in occ:
        direct=row['alignment_status']=='source_exact'
        row['profile_id']=row['features'] if direct else 'qac_v0.4_aligned'
        row['scope']=row['reason'] if direct else 'numbered_aligned'
        row['included_in_source_count']=int(row['include'] and row['alignment_status'] in ('verified','source_exact'))
        row.setdefault('source_letter_count','')
    write_csv(tables/'morphology_claim_occurrences.csv.gz',occ)
    form_counts=Counter((r['claim_id'],r['profile_id'],r['scope'],r['source_surface'],r['lemma'],r['include'],r['included_in_source_count'],r['reason'],r['alignment_status']) for r in occ)
    form_fields=('claim_id','profile_id','scope','source_surface','lemma','rule_selected','included_in_source_count','reason','alignment_status')
    write_csv(tables/'morphology_claim_forms.csv',[{**dict(zip(form_fields,key)),'occurrences':n} for key,n in form_counts.items()])
    write_csv(tables/'morphology_claims.csv',claim_rows)
    return findings


def run(root: Path, config: dict) -> dict:
    root=Path(root);tables=root/'results/tables';qac_path=root/'data/external/quranic-corpus-morphology-0.4.txt'
    db=sqlite3.connect(f'file:{root / "data/processed/corpus.sqlite"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    tokens=[dict(r) for r in db.execute('SELECT * FROM tokens ORDER BY global_token')];db.close()
    all_segments=load_qac(qac_path)
    qwords={};external_byverse=defaultdict(list)
    for s in all_segments:
        wid=s['qac_word_id']
        if wid not in qwords:qwords[wid]={'qac_word_id':wid,'verse_id':s['verse_id'],'surface':''}
        qwords[wid]['surface']+=s['segment_arabic']
    for w in qwords.values():external_byverse[w['verse_id']].append(w)
    source_byverse=defaultdict(list)
    for t in tokens:
        if not t['is_opening_basmala']:source_byverse[t['verse_id']].append(t)
    alignment=[]
    for vid,ss in source_byverse.items():alignment.extend(align_verse(ss,external_byverse.get(vid,[])))
    amap={a['qac_word_id']:a for a in alignment if a['status']=='verified'}
    token_map={t['token_id']:t for t in tokens}
    segments=[{**s,'token_ids':amap[s['qac_word_id']]['token_ids'],'alignment_status':'verified',
               'alignment_rule':amap[s['qac_word_id']]['rule'],
               'first_global_token_file':min(token_map[t]['global_token'] for t in amap[s['qac_word_id']]['token_ids'].split('|')),
               'last_global_token_file':max(token_map[t]['global_token'] for t in amap[s['qac_word_id']]['token_ids'].split('|'))}
              for s in all_segments if s['qac_word_id'] in amap]
    assert len({s['segment_id'] for s in segments})==len(segments), 'Segment duplication in annotation join'
    assert len({a['qac_word_id'] for a in alignment if a['qac_word_id']})==len(qwords), 'Lost external word rows'
    assert all(t in token_map and not token_map[t]['is_opening_basmala'] for s in segments for t in s['token_ids'].split('|'))
    write_csv(tables/'morphology_alignment.csv.gz',alignment)
    write_csv(tables/'morphology_mismatches.csv',[a for a in alignment if a['status']!='verified'])
    write_csv(tables/'morphology_segments.csv.gz',segments)
    freq={}
    for field in ('lemma','root','pos','segment_arabic'):
        name='segment' if field=='segment_arabic' else field
        rows=frequencies(segments,field);write_csv(tables/f'morphology_{name}_frequencies.csv',rows);freq[name]=rows
    feat_counts=Counter((s['pos'],f) for s in segments for f in s['features'].split('|') if not f.startswith(('LEM:','ROOT:')))
    write_csv(tables/'morphology_feature_frequencies.csv',[{'pos':p,'feature':f,'segment_count':n} for (p,f),n in sorted(feat_counts.items(),key=lambda x:-x[1])])
    verified_token_ids={t for s in segments for t in s['token_ids'].split('|')}
    numbered=[t for t in tokens if not t['is_opening_basmala']]
    covered_forms={t['plain'] for t in numbered if t['token_id'] in verified_token_ids}
    all_forms={t['plain'] for t in numbered}
    per_surah=[]
    for surah in sorted({t['surah_id'] for t in tokens}):
        ts=[t for t in numbered if t['surah_id']==surah];covered=sum(t['token_id'] in verified_token_ids for t in ts)
        per_surah.append({'surah_id':surah,'numbered_tokens':len(ts),'verified_tokens':covered,'coverage':covered/len(ts)})
    write_csv(tables/'morphology_coverage_by_surah.csv',per_surah)
    morph_catalogues(segments,tokens,tables)
    mc=config.get('morphology',{})
    if mc.get('cooccurrence_window_tokens',5)!=5:
        raise ValueError('This morphology module supports the registered window of 5 source tokens only')
    associations=cooccurrence(segments,tokens,tables,top_n=mc.get('cooccurrence_top_n',60),min_support=mc.get('cooccurrence_min_support',5))
    findings=linguistic(segments,tokens,tables,all_segments)+published_claims(tokens,all_segments,segments,tables)
    write_json(root/'results/hypotheses/morphology.json',findings)
    summary={'source':'Quranic Arabic Corpus v0.4','source_sha256':QAC_SHA256,'alignment_profile':'qac_alignment_v1',
             'source_tokens_numbered':len(numbered),'source_tokens_file':len(tokens),'external_words':len(qwords),'external_segments':len(all_segments),
             'verified_tokens':len(verified_token_ids),'verified_words':len(amap),'verified_segments':len(segments),
             'token_coverage':len(verified_token_ids)/len(numbered),'plain_types':len(all_forms),'verified_plain_types':len(covered_forms),'type_coverage':len(covered_forms)/len(all_forms),
             'excluded_opening_basmala_tokens':sum(t['is_opening_basmala'] for t in tokens),'alignment_status_counts':dict(Counter(a['status'] for a in alignment)),
             'segments_without_lemma':sum(not s['lemma'] for s in segments),'segments_without_root':sum(not s['root'] for s in segments),
             'unique_lemmas':len(freq['lemma']),'unique_roots':len(freq['root']),'pos_categories':len(freq['pos']),
             'top_lemmas':freq['lemma'][:15],'top_roots':freq['root'][:15],
             'cooccurrence':associations,
             'alignment_group_rule_counts':dict(Counter(a['rule'] for a in alignment if a['status']=='verified')),
             'claims':[{k:f[k] for k in ('hypothesis_id','title_ru','status','observed')} for f in findings if f['family']=='J'],
             'limitations_ru':['Разметка переносится только для единственного оптимального выравнивания и совпавшей поверхности по qac_alignment_v1.','Морфологические итоги описывают выровненную часть numbered, не полный файл.','Отсутствующая лемма/корень остаётся пустой строкой; признак не предсказывается.','Словообразование и значения не выводятся автоматически из совпадения корней.','Ненумерованные начальные басмалы исключены из выравнивания; 1:1 и 27:30 сохранены.']}
    write_json(root/'results/morphology_summary.json',summary)
    coverage=[{'direction':'B','analysis':'Морфология: сегменты, леммы, корни, POS и признаки','status':'частично','universe':len(numbered),'tested':len(verified_token_ids),'limitations':'Только надёжно выровненные токены numbered. Полный внешний источник сохранён отдельно; чужие частоты не подменяют корпус.'},
              {'direction':'G','analysis':'Совместная встречаемость лемм и корней','status':'complete_bounded','universe':'top-60 каждого уровня × все пары × аят/сура/перекрывающееся окно5 внутри аята','tested':associations['pair_context_rows'],'limitations':associations['limitation_ru']},
              {'direction':'I','analysis':'Отрицания, обращения, вопросы, повелительные формы, имена; 12 корней числовых кандидатов; смена лица соседних глаголов','status':'частично','universe':'QAC v0.4, выровненные сегменты','tested':len(segments),'limitations':'Семантическая интерпретация, тематическая разметка, значения числовых конструкций и риторическая интерпретация переходов не валидированы.'},
              {'direction':'J','analysis':'Пять опубликованных утверждений: день, месяц, басмала 114/19, пара الدنيا/الآخرة','status':'выполнено с ограничениями','universe':5,'tested':5,'limitations':'Статусы индивидуальны; пара недостаточно специфицирована, непокрытые формы не считаются отсутствующими.'}]
    write_json(root/'results/morphology_coverage.json',coverage)
    return summary
