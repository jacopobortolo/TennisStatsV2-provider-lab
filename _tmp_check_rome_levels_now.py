import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path.home() / '.tennis_analytics' / 'data' / 'tennis.db')
queries = [
    (
        'rome_recent',
        """
                SELECT SUBSTR(tourney_date,1,4) AS year, tourney_name, tourney_level,
                             LOWER(COALESCE(scrape_provider,
                                         CASE WHEN winner_seed IS NULL THEN 'sofascore' ELSE 'tennisabstract' END)) AS provider,
                             COUNT(*) AS cnt
        FROM matches
        WHERE tour='wta'
          AND LOWER(tourney_name) LIKE '%rome%'
          AND SUBSTR(tourney_date,1,4) >= '2024'
                GROUP BY year, tourney_name, tourney_level, provider
                ORDER BY year DESC, cnt DESC, tourney_name, tourney_level, provider
        """
    ),
    (
        'wta_rome_w_rows',
        """
                SELECT id, tourney_date, tourney_name, tourney_level,
                             LOWER(COALESCE(scrape_provider,
                                         CASE WHEN winner_seed IS NULL THEN 'sofascore' ELSE 'tennisabstract' END)) AS provider,
                             winner_name, loser_name
        FROM matches
        WHERE tour='wta'
          AND LOWER(tourney_name) LIKE '%rome%'
          AND tourney_level='W'
        ORDER BY tourney_date DESC
        LIMIT 30
        """
    ),
]
for label, query in queries:
    print('SECTION', label)
    for row in conn.execute(query):
        print(row)
    print('END')
