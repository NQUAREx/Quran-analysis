"""Build a portable archive including large data, excluding caches and Git."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def selected_files(root):
    for p in sorted(root.rglob('*')):
        rel=p.relative_to(root)
        if not p.is_file() or any(x in {'.git','.venv','__pycache__','.pytest_cache','.cache'} or x.endswith('.egg-info') for x in rel.parts):continue
        if p.suffix in {'.zip','.pyc'} or rel.as_posix() in {'artifact_manifest.json','data/processed/corpus.sqlite.gz'}:continue
        yield p


def main(argv=None):
    parser=argparse.ArgumentParser(description='Переносимый архив проверенных результатов')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    args=parser.parse_args(argv);root=args.root.resolve()
    for rel in ['dashboard/index.html','exports/quran_analysis.xlsx','results/validation_summary.json','run_manifest.json']:
        if not (root/rel).is_file():parser.error(f'Ещё нет обязательного результата {rel}')
    files=list(selected_files(root));inventory=[]
    for path in files:
        h=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        inventory.append({'path':str(path.relative_to(root)),'bytes':path.stat().st_size,'sha256':h.hexdigest()})
    manifest=root/'artifact_manifest.json'
    manifest.write_text(json.dumps({'files':inventory,'file_count':len(inventory),'total_uncompressed_bytes':sum(x['bytes'] for x in inventory)},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    target=root/'exports/quran-analysis-portable.zip'
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for path in files+[manifest]:z.write(path,Path('Quran-analysis')/path.relative_to(root))
    with zipfile.ZipFile(target) as z:
        bad=z.testzip()
        if bad:raise RuntimeError(f'ZIP integrity failure: {bad}')
    print(json.dumps({'archive':str(target),'bytes':target.stat().st_size,'files':len(files)+1,'integrity':'pass'},ensure_ascii=False))


if __name__=='__main__':main()
