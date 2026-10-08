"""Post-observation exploratory sensitivity analyses, explicitly descriptive."""
from collections import Counter, defaultdict
from pathlib import Path
import csv
import gzip
import json
import sqlite3
import unicodedata as ud


def _write(path,rows):
    if not rows:return
    opener=gzip.open if path.suffix==".gz" else open
    with opener(path,"wt",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,list(rows[0]));w.writeheader();w.writerows(rows)


def _dump(path,obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def equal_pair_count(values):
    return sum(n*(n-1)//2 for n in Counter(values).values())


def run(root: Path, config: dict):
    conn=sqlite3.connect(root/"data/processed/corpus.sqlite"); conn.row_factory=sqlite3.Row
    tokens=[dict(r) for r in conn.execute("SELECT * FROM tokens ORDER BY global_token")]
    verses=[dict(r) for r in conn.execute("SELECT * FROM verses ORDER BY global_ayah")];conn.close()
    vmap={v['verse_id']:v for v in verses}
    surahs=sorted({t['surah_id'] for t in tokens}); S=len(surahs)
    tables=root/'results/tables';findings=[]
    sensitivity=[];universals={};scope_freq={}
    for profile in ['plain','search']:
        info={scope:defaultdict(lambda:{'frequency':0,'surahs':set(),'first':'','last':''}) for scope in ['file','numbered']}
        for t in tokens:
            for scope in ['file','numbered']:
                if scope=='numbered' and t['is_opening_basmala']:continue
                d=info[scope][t[profile]];d['frequency']+=1;d['surahs'].add(t['surah_id'])
                if not d['first']:d['first']=t['token_id']
                d['last']=t['token_id']
        universals[profile]={scope:sorted(w for w,d in info[scope].items() if len(d['surahs'])==S) for scope in info}
        for w,a in sorted(info['file'].items()):
            b=info['numbered'].get(w,{'frequency':0,'surahs':set(),'first':'','last':''})
            sensitivity.append({'profile_id':profile,'form':w,'frequency_file':a['frequency'],'frequency_numbered':b['frequency'],
                'surahs_file':len(a['surahs']),'surahs_numbered':len(b['surahs']),
                'lost_surah_ids':';'.join(map(str,sorted(a['surahs']-b['surahs']))),
                'first_token_id_file':a['first'],'last_token_id_file':a['last'],
                'first_token_id_numbered':b['first'],'last_token_id_numbered':b['last']})
        if profile=='plain':scope_freq={scope:{w:d['frequency'] for w,d in x.items()} for scope,x in info.items()}
    _write(tables/'extension_basmala_lexical_coverage.csv.gz',sensitivity)
    findings.append({'hypothesis_id':'EXT-universality-basmala','family':'exploratory_boundary_sensitivity',
        'title_ru':'Охват всех сур зависит от вступительных басмал','status':'Чувствительно к правилам подсчёта',
        'profile_id':'plain/search','scope':'file vs numbered','observed':universals,'registration':'exploratory после core',
        'method_ru':'Пересчитаны частота и охват каждой формы по всем token_id в двух областях; 1:1 и 27:30 остаются.',
        'limitation_ru':'Это свойство включения вступительных префиксов, а не семантическая универсальность понятий.',
        'p_value':None,'evidence_path':'results/tables/extension_basmala_lexical_coverage.csv.gz'})
    # Keep original adjacency: removing repeated verses must not create new edges.
    ending_rows=[];edge_rows=[]
    for scope in ['file','numbered']:
        seq=defaultdict(list)
        for t in tokens:
            if scope=='numbered' and t['is_opening_basmala']:continue
            seq[t['verse_id']].append(t['plain'])
        repeats=Counter(tuple(seq[v['verse_id']]) for v in verses)
        repeated={v['verse_id'] for v in verses if repeats[tuple(seq[v['verse_id']])]>1}
        for n in [2,3]:
            counts=defaultdict(Counter)
            for a,b in zip(verses,verses[1:]):
                if a['surah_id']!=b['surah_id']:continue
                va,vb=a['verse_id'],b['verse_id']
                last=lambda v: ''.join(c for c in seq[v][-1] if ud.category(c).startswith('L') and c!='ـ')[-n:]
                agree=int(last(va)==last(vb));excluded=va in repeated or vb in repeated
                for sid in [0,a['surah_id']]:
                    d=counts[sid];d['all_edges']+=1;d['all_agree']+=agree
                    if not excluded:d['retained_edges']+=1;d['retained_agree']+=agree
                edge_rows.append({'scope':scope,'suffix_letters':n,'left_verse_id':va,'right_verse_id':vb,
                                  'same_ending':agree,'excluded_repeated_endpoint':int(excluded)})
            for sid,d in sorted(counts.items()):
                ending_rows.append({'scope':scope,'profile_id':'plain','suffix_letters':n,'surah_id':sid,
                    'all_edges':d['all_edges'],'all_agree':d['all_agree'],
                    'retained_edges':d['retained_edges'],'retained_agree':d['retained_agree'],
                    'all_rate':d['all_agree']/d['all_edges'] if d['all_edges'] else None,
                    'retained_rate':d['retained_agree']/d['retained_edges'] if d['retained_edges'] else None,
                    'repeated_verse_count_corpus':len(repeated)})
    _write(tables/'extension_refrain_ending_sensitivity.csv',ending_rows)
    _write(tables/'extension_refrain_adjacency_edges.csv.gz',edge_rows)
    findings.append({'hypothesis_id':'EXT-refrain-endings','family':'exploratory_repeat_sensitivity',
        'title_ru':'Исключение повторённых аятов не устраняет сходство письменных окончаний',
        'status':'Исследовательский кандидат','profile_id':'plain','scope':'file/numbered',
        'observed':[r for r in ending_rows if r['surah_id']==0], 'registration':'exploratory после просмотра повторов',
        'method_ru':'Доли исходных соседних пар до/после исключения рёбер с повторённым полным аятом; новое соседство не создаётся.',
        'limitation_ru':'Меняется состав рёбер; разница долей не является причинным эффектом повторов. Окончания письменные, без фонетической модели.',
        'p_value':None,'evidence_path':'results/tables/extension_refrain_adjacency_edges.csv.gz'})
    groups=[];pairs={}
    for scope,freq in scope_freq.items():
        grouped=defaultdict(list)
        for w,n in freq.items():grouped[n].append(w)
        pairs[scope]=sum(len(ws)*(len(ws)-1)//2 for ws in grouped.values())
        for n,ws in sorted(grouped.items()):
            groups.append({'scope':scope,'profile_id':'plain','frequency':n,'type_count':len(ws),
                           'unordered_equal_pairs':len(ws)*(len(ws)-1)//2,'forms_json':json.dumps(sorted(ws),ensure_ascii=False)})
    intersection=equal_pair_count((n,scope_freq['numbered'][w]) for w,n in scope_freq['file'].items() if w in scope_freq['numbered'])
    equality={'pairs_file':pairs['file'],'pairs_numbered':pairs['numbered'],'preserved_pairs':intersection,
              'lost_pairs':pairs['file']-intersection,'new_pairs':pairs['numbered']-intersection,
              'pair_intersection_over_union':intersection/(pairs['file']+pairs['numbered']-intersection)}
    _write(tables/'extension_frequency_equality_groups.csv.gz',groups)
    findings.append({'hypothesis_id':'EXT-equality-search-volume','family':'exploratory_equal_frequency',
        'title_ru':'Пространство равных частот содержит множество пар','status':'Точный факт для указанного файла и правил',
        'profile_id':'plain','scope':'file/numbered','observed':equality,'registration':'exploratory после core',
        'method_ru':'Полная группировка всех форм по частоте; число пар в группе k(k−1)/2. Пересечение — группировка по паре частот в двух областях.',
        'limitation_ru':'Совпадение не сообщает о близости значений; p-value отсутствует, поскольку модель случайности не задана.',
        'p_value':None,'evidence_path':'results/tables/extension_frequency_equality_groups.csv.gz'})
    summary={'universals':universals,'equality_pairs':equality,'ending_sensitivity':[r for r in ending_rows if r['surah_id']==0],
             'registration':'exploratory','stop_reason_ru':'Исчерпаны три дополнительные конечные семейства из docs/EXPLORATORY_PLAN.md; иные закономерности не исключаются.'}
    _dump(root/'results/extension_summary.json',summary)
    _dump(root/'results/hypotheses/extension.json',findings)
    _dump(root/'results/extension_coverage.json',[
        {'direction':'B','analysis':'Чувствительность охвата каждой формы к басмале','status':'complete_bounded','universe':'Все plain/search формы × file/numbered','tested':len(sensitivity),'limitations':'Словоформа не равна понятию'},
        {'direction':'E','analysis':'Окончания с исключением рёбер повторённых аятов','status':'complete_bounded','universe':'Все исходные внутрисуровые соседства × 2 области × 2/3 буквы','tested':len(edge_rows),'limitations':'Без новых рёбер; не причинный и не фонетический вывод'},
        {'direction':'F','analysis':'Все пары одинаковой частоты и их чувствительность','status':'complete_bounded','universe':'Неупорядоченные пары plain-форм; группы эквивалентности','tested':equality,'limitations':'Нет статистической интерпретации равенств'}])
    return summary
