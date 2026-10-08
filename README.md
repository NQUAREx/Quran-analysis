# Исследование арабского текста Корана

**Откройте [интерактивный отчёт на GitHub Pages](https://nquarex.github.io/Quran-analysis/dashboard/).** Он содержит корпус, поиск, контексты, фильтры, графики, гипотезы и карту покрытия. Просмотр файла `dashboard/index.html` внутри интерфейса GitHub не запускает JavaScript, поэтому вкладки там не работают. После скачивания [переносимого архива](https://github.com/NQUAREx/Quran-analysis/releases/latest/download/quran-analysis-portable.zip) отчёт также можно открыть в обычном браузере; для связанных полных таблиц сохраняйте структуру папок архива.

- [Текстовый отчёт](report/REPORT.md)
- [Книга Excel](exports/quran_analysis.xlsx)
- [Полные таблицы](results/tables/) и [SQLite с индексами в gzip](data/processed/corpus.sqlite.gz) (после распаковки: `gzip -dk corpus.sqlite.gz`)
- [SVG/PNG-графики и исходные числа](results/figures/)
- [Каталог гипотез](results/hypotheses/registry.json) и [карта покрытия](results/coverage.json)
- [Проверки](results/validation_checks.json), [манифест запуска](run_manifest.json), [состояние проекта](PROJECT_STATE.md)
- [Полный переносимый ZIP в релизе GitHub](https://github.com/NQUAREx/Quran-analysis/releases/latest/download/quran-analysis-portable.zip), [браузерная проверка](report/BROWSER_CHECKS.md)

Единственный основной корпус — исходный `Quran_text.txt` этого репозитория. Он не редактировался и не заменялся внешним текстом. Неизменная копия и SHA-256 лежат в `data/raw/`. Происхождение, чтение и тождество внешней редакции по самому файлу не установлены. Все выводы относятся к этому файлу и определённым правилам.

`raw`, `nfc`, `plain`, `search` — четыре профиля письменной поверхности. `file` следует структуре исходника; `numbered` исключает только явно распознанные вступительные басмалы-префиксы вне сур 1 и 9. Басмалы в 1:1 и 27:30 остаются. Письменная буква не равна звуку; токен не равен морфологическому сегменту, лемме или корню. [Правила](docs/COUNTING_RULES.md) объясняют Unicode, границы, нумерацию и знаменатели.

Внешняя QAC v0.4 используется только для проверенно сопоставленных мест. Охват, неоднозначности, несовпадения и лицензии сохранены; непокрытая морфология не предсказывается. Частотные равенства описательны. Перестановочные тесты сравнивают порядок и позиции с конкретными моделями, используют полный зарегистрированный набор проверок и Holm-поправку; они не устанавливают уникальность, намеренность или происхождение текста.

## Воспроизведение

Требуется Python 3.11+; фактические версии зафиксированы в `requirements.lock.txt` и манифесте.

```bash
cd Quran-analysis
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install -e . --no-deps

# Полный пересчёт всех этапов
python -m quran_analysis --stage all

# Возобновление: пропустить только проверенный кэш
python -m quran_analysis --stage all --resume

# Выбранный этап и необходимые зависимости
python -m quran_analysis --stage statistics --resume
python -m quran_analysis --stage presentation --resume

# Проверки рисков обработки и независимый итоговый аудит
python -m pytest -q
python -m quran_analysis --stage validation --resume

# Переносимый архив готового проекта
python -m quran_analysis.package
```

На Windows активация окружения: `.venv\Scripts\activate`. Все пути относятся к корню проекта. Сохранённые исходник и внешняя разметка позволяют пересчёт без сети после установки библиотек. Для необязательной локальной HTTP-проверки: `python -m http.server 8765 --bind 127.0.0.1`, затем `http://127.0.0.1:8765/dashboard/index.html`.

Полные вхождения можно получить без интерфейса:

```bash
python -m quran_analysis.query --text 'الله' --profile plain --scope numbered --output allah.csv
python -m quran_analysis.query --unit character --text 'ّ' --profile raw --output shadda.csv
```

## Методика и границы

- [Задание пользователя](docs/USER_SPECIFICATION.md)
- [Правила подсчёта](docs/COUNTING_RULES.md), [словарь полей](docs/DATA_DICTIONARY.md), [SQL-примеры](docs/QUERIES.sql)
- [Конечное пространство поиска](docs/SEARCH_SPACE.md)
- [Статистический план](docs/STATISTICAL_PLAN.md) и [дополнительный исследовательский цикл](docs/EXPLORATORY_PLAN.md)
- [Внешние источники, цитаты и лицензии](docs/SOURCES.md)
- [Воспроизводимость и кэш](docs/REPRODUCIBILITY.md)

CSV используют UTF-8 и запятую. `.csv.gz` — полные сжатые таблицы. Адреса вроде `2:255` импортируйте как текст; пустые значения не равны нулю. Большие таблицы вынесены из XLSX по явно указанным ссылкам, без скрытого усечения.

«Полностью» в карте покрытия относится только к объявленному конечному семейству. Длинные и приблизительные повторы найдены ограниченным поиском; смысловые, фонетические и хронологические выводы имеют отдельные ограничения. Для каждого направления A–J сохранён результат или конкретная граница доступности. Публичная публикация не выполнялась.
