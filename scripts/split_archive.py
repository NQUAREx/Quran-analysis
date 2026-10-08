"""Split the portable ZIP into parts small enough for ordinary GitHub files."""
from __future__ import annotations

import hashlib
from pathlib import Path


PART_BYTES = 35 * 1024 * 1024


def main() -> None:
    exports = Path(__file__).resolve().parents[1] / "exports"
    archive = exports / "quran-analysis-portable.zip"
    if not archive.is_file():
        raise SystemExit("Сначала выполните python -m quran_analysis.package")
    old_parts = list(exports.glob(archive.name + ".part-*"))
    for old in old_parts:
        old.unlink()
    digest = hashlib.sha256()
    parts = []
    with archive.open("rb") as source:
        for number in range(1, 100):
            block = source.read(PART_BYTES)
            if not block:
                break
            digest.update(block)
            part = exports / f"{archive.name}.part-{number:03d}"
            part.write_bytes(block)
            parts.append(part)
    if len(parts) < 2:
        raise SystemExit("Архив слишком мал для разбиения; проверьте его содержимое")
    (exports / (archive.name + ".sha256")).write_text(f"{digest.hexdigest()}  {archive.name}\n")
    print(f"Готово: {len(parts)} части, SHA-256 {digest.hexdigest()}")


if __name__ == "__main__":
    main()
