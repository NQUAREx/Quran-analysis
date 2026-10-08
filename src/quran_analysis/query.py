"""Export exact surface/character occurrences without loading HTML or Excel."""
import argparse
import csv
import sqlite3
import sys
from pathlib import Path


def main(argv=None):
    p=argparse.ArgumentParser(description="Полный список исходных вхождений формы или символа")
    p.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[2])
    p.add_argument("--profile",choices=['raw','nfc','plain','search'],default='plain')
    p.add_argument("--scope",choices=['file','numbered'],default='file')
    p.add_argument("--unit",choices=['form','character'],default='form')
    p.add_argument("--text",required=True)
    p.add_argument("--surah",type=int)
    p.add_argument("--output",type=Path)
    args=p.parse_args(argv)
    if args.unit=='character' and len(args.text)!=1:
        p.error('--unit character требует ровно одну кодовую точку Unicode')
    db=args.root/'data/processed/corpus.sqlite'
    c=sqlite3.connect(f'file:{db}?mode=ro',uri=True);c.row_factory=sqlite3.Row
    # Profile identifier from argparse allowlist; input values are bound parameters.
    conditions=[f't.{args.profile} = ?' if args.unit=='form' else f'instr(t.{args.profile}, ?) > 0']
    params=[args.text]
    if args.scope=='numbered':conditions.append('t.is_opening_basmala=0')
    if args.surah is not None:conditions.append('t.surah_id=?');params.append(args.surah)
    query='SELECT t.*,v.raw_text AS context_raw FROM tokens t JOIN verses v USING(verse_id) WHERE '+' AND '.join(conditions)+' ORDER BY t.global_token'
    fields=['profile_id','scope','unit','form','token_id','verse_id','global_token_file','raw','source_span_start_cp','source_span_end_cp','character_index_in_profile_token','exact_source_character_cp','context_raw']
    handle=args.output.open('w',encoding='utf-8',newline='') if args.output else sys.stdout
    try:
        w=csv.DictWriter(handle,fields);w.writeheader()
        for row in c.execute(query,params):
            indices=[None] if args.unit=='form' else [i for i,ch in enumerate(row[args.profile]) if ch==args.text]
            for i in indices:
                w.writerow({'profile_id':args.profile,'scope':args.scope,'unit':args.unit,'form':row[args.profile],
                    'token_id':row['token_id'],'verse_id':row['verse_id'],'global_token_file':row['global_token'],
                    'raw':row['raw'],'source_span_start_cp':row['raw_start'],'source_span_end_cp':row['raw_end'],
                    'character_index_in_profile_token':i,
                    'exact_source_character_cp':row['raw_start']+i if args.profile=='raw' and i is not None else None,
                    'context_raw':row['context_raw']})
    finally:
        if args.output:handle.close()
        c.close()


if __name__=='__main__':main()
