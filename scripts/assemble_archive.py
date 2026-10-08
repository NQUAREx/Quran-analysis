"""Reassemble the portable ZIP tracked in GitHub-sized parts."""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path


def main() -> None:
    exports = Path(__file__).resolve().parents[1] / "exports"
    archive = exports / "quran-analysis-portable.zip"
    parts = sorted(exports.glob(archive.name + ".part-*"))
    if len(parts) < 2:
        raise SystemExit("Не найдены все части архива в exports/")
    expected = (exports / (archive.name + ".sha256")).read_text().split()[0]
    temporary = archive.with_suffix(".zip.tmp")
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as output:
            for part in parts:
                with part.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        output.write(block)
                        digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError("SHA-256 архива не совпадает: проверьте все части")
        with zipfile.ZipFile(temporary) as package:
            damaged = package.testzip()
        if damaged:
            raise ValueError(f"Повреждённый файл в ZIP: {damaged}")
        shutil.move(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Готово: {archive} ({archive.stat().st_size} байт, SHA-256 {expected})")


if __name__ == "__main__":
    main()
