-- sqlite3 data/processed/corpus.sqlite < docs/QUERIES.sql
-- Все частоты ниже — plain/file, если не указано иное.

-- Полные вхождения формы الله с исходным контекстом.
SELECT t.token_id, t.global_token, t.raw, t.raw_start, t.raw_end, v.raw_text
FROM tokens AS t JOIN verses AS v USING (verse_id)
WHERE t.plain = 'الله'
ORDER BY t.global_token;

-- Сравнение числа токенов и поверхностного словаря по сурам.
SELECT surah_id, COUNT(*) AS tokens_file,
       SUM(is_opening_basmala=0) AS tokens_numbered,
       COUNT(DISTINCT plain) AS types_plain_file,
       SUM(letter_count) AS written_letters_raw
FROM tokens GROUP BY surah_id ORDER BY surah_id;

-- Редакционные префиксы, исключённые только в numbered.
SELECT token_id, raw, plain FROM tokens
WHERE is_opening_basmala=1 ORDER BY global_token;

-- Все формы заданной частоты: пары раскрываются внутри группы,
-- а не создаются гигантским декартовым произведением всех форм.
SELECT plain AS form, COUNT(*) AS frequency
FROM tokens WHERE is_opening_basmala=0
GROUP BY plain HAVING COUNT(*)=19 ORDER BY plain;

-- Исходный фрагмент 27:30: внутреннюю басмалу не удаляют.
SELECT verse_id, raw_text FROM verses WHERE verse_id='27:30';

-- Отдельные служебные знаки и пробелы хранятся в spans;
-- схему можно осмотреть: PRAGMA table_info(spans).

