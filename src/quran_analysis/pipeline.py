"""Reproducible staged runner, with content-addressed cache verification."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STAGES = ["core", "morphology", "exploration", "statistics", "extension", "presentation", "validation"]
DEPS = {"core": [], "morphology": ["core"], "exploration": ["core"], "statistics": ["core"],
        "extension": ["core", "statistics", "exploration"],
        "presentation": ["core", "morphology", "exploration", "statistics", "extension"],
        "validation": ["presentation"]}


def digest(path):
    h = hashlib.sha256()
    with open(path,"rb") as f:
        for block in iter(lambda:f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def json_write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")


def _files_for_stage(root, stage):
    paths = set((root / "results").glob(f"{stage}*.json"))
    paths.update((root/"results/tables").glob(f"{stage}_*"))
    p=root/f"results/hypotheses/{stage}.json"
    if p.exists(): paths.add(p)
    if stage == "core":
        paths.update((root/"data/processed").glob("*"))
        paths.update((root/"data/raw").glob("*"))
    elif stage == "morphology":
        paths.update((root/"data/external").rglob("*"))
    elif stage == "exploration":
        paths.update((root/"results").glob("exploration*.sqlite"))
    elif stage == "presentation":
        paths.update((root/"dashboard").rglob("*"))
        paths.update((root/"results/figures").rglob("*"))
        paths.update((root/"exports").glob("*.xlsx"))
        # Browser QA and handover notes are independent artifacts, not generated
        # by presentation.run; avoid invalidating all exports when QA is saved.
        paths.add(root/"report/REPORT.md")
    return {str(p.relative_to(root)): digest(p) for p in sorted(paths) if p.is_file()}


def consolidate(root):
    coverage, findings = [], []
    for stage in STAGES:
        p=root/f"results/{stage}_coverage.json"
        if p.exists():
            obj=json.loads(p.read_text())
            coverage.extend(obj if isinstance(obj,list) else obj.get("coverage",[]))
        p=root/f"results/hypotheses/{stage}.json"
        if p.exists():
            obj=json.loads(p.read_text())
            findings.extend(obj if isinstance(obj,list) else obj.get("findings",[]))
    json_write(root/"results/coverage.json",coverage)
    json_write(root/"results/hypotheses/registry.json",findings)
    for filename,rows in [("coverage.csv",coverage),("hypotheses.csv",findings)]:
        if not rows: continue
        keys=list(dict.fromkeys(k for r in rows for k in r))
        with (root/"results/tables"/filename).open("w",encoding="utf-8",newline="") as f:
            w=csv.DictWriter(f,keys); w.writeheader()
            w.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()} for r in rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description="Воспроизводимый анализ Quran_text.txt")
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config",type=Path,default=None)
    parser.add_argument("--stage",choices=["all"]+STAGES,default="all")
    parser.add_argument("--resume",action="store_true",help="Пропускать этапы только после сверки хешей входа, кода, зависимостей и результатов")
    args=parser.parse_args(argv)
    root=args.root.resolve()
    config_path=args.config or root/"config/analysis.json"
    config=json.loads(config_path.read_text(encoding="utf-8"))
    source=root/config.get("input","Quran_text.txt")
    if not source.is_file():
        parser.error(f"Нет авторизованного исходника: {source}. Другой текст не скачивается вместо него.")
    manifest_path=root/"run_manifest.json"
    previous=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest={"schema_version":"1.0.0","started_at":datetime.now(timezone.utc).isoformat(),
              "input":{"path":str(source.relative_to(root)),"sha256":digest(source)},
              "config":config,"config_sha256":digest(config_path),
              "python":sys.version,"platform":platform.platform(),"versions":{},
              "stages":previous.get("stages",{})}
    for package in ["numpy","scipy","pandas","matplotlib","openpyxl","regex","pytest","playwright"]:
        try: manifest["versions"][package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: pass
    try:
        manifest["git_commit"]=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
        manifest["git_dirty"]=bool(subprocess.check_output(["git","status","--porcelain"],cwd=root,text=True).strip())
    except (subprocess.CalledProcessError,FileNotFoundError):
        manifest["git_commit"]=None
    manifest["source_code_sha256"]={str(p.relative_to(root)):digest(p) for p in sorted((root/"src").rglob("*.py"))}
    requested=set(STAGES if args.stage=="all" else [args.stage])
    def add_deps(stage):
        for dep in DEPS[stage]:
            requested.add(dep); add_deps(dep)
    for stage in list(requested): add_deps(stage)
    for stage in STAGES:
        if stage not in requested: continue
        module_path=root/f"src/quran_analysis/{stage}.py"
        auxiliary = {}
        if stage == "presentation":
            auxiliary = {str(p.relative_to(root)):digest(p) for p in sorted((root/"src/quran_analysis").glob("*.html"))}
            auxiliary.update({str(p.relative_to(root)):digest(p) for p in sorted((root/"docs").glob("*.md"))})
        dependencies={}
        for d in DEPS[stage]:
            upstream=manifest['stages'].get(d,{})
            dependencies[d]={
                'cache_key':upstream.get('cache_key'),
                'outputs_sha256':hashlib.sha256(json.dumps(upstream.get('outputs',{}),sort_keys=True).encode()).hexdigest(),
            }
        key_data={"input":digest(source),"config":digest(config_path),"module":digest(module_path),"auxiliary":auxiliary,
                  "dependencies":dependencies}
        key=hashlib.sha256(json.dumps(key_data,sort_keys=True).encode()).hexdigest()
        old=manifest["stages"].get(stage,{})
        cached = bool(old.get("outputs")) and old.get("cache_key")==key and old.get("status")=="complete"
        if cached:
            cached=all((root/p).is_file() and digest(root/p)==h for p,h in old["outputs"].items())
        if args.resume and cached:
            print(f"[{stage}] кэш проверен; пропуск",flush=True)
            continue
        if stage in ["presentation","validation"]:
            consolidate(root)
        print(f"[{stage}] запуск",flush=True)
        t=time.perf_counter()
        manifest["stages"][stage]={"cache_key":key,"status":"running","started_at":datetime.now(timezone.utc).isoformat()}
        json_write(manifest_path,manifest)
        try:
            summary=importlib.import_module(f"quran_analysis.{stage}").run(root,config)
            stage_status="failed" if stage=="validation" and summary.get("status") not in ("pass","passed") else "complete"
            manifest["stages"][stage].update(status=stage_status,elapsed_seconds=round(time.perf_counter()-t,3),outputs=_files_for_stage(root,stage))
            if stage_status=="failed":
                raise RuntimeError(f"Проверка: {summary.get('status')}; см. results/validation_checks.json")
        except Exception as exc:
            manifest["stages"][stage].update(status="failed",error=f"{type(exc).__name__}: {exc}")
            json_write(manifest_path,manifest)
            raise
        json_write(manifest_path,manifest)
    consolidate(root)
    manifest["finished_at"]=datetime.now(timezone.utc).isoformat()
    manifest["source_code_sha256"]={str(p.relative_to(root)):digest(p) for p in sorted((root/"src").rglob("*.py"))}
    json_write(manifest_path,manifest)
    print(f"Готово: {root / 'dashboard/index.html'}",flush=True)


if __name__=="__main__": main()
