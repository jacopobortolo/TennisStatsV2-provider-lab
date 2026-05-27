from pathlib import Path
import sqlite3

LOCAL_SQL = [
    (
        "Monte Carlo F",
        """
        SELECT rowid, tour, tourney_id, tourney_name, tourney_date, round,
               winner_name, loser_name, scrape_provider, source_match_date,
               is_upcoming
        FROM matches
        WHERE tourney_date LIKE '2026%'
          AND LOWER(tourney_name) LIKE '%monte carlo%'
          AND round='F'
          AND (winner_name LIKE '%Sinner%' OR loser_name LIKE '%Sinner%')
        ORDER BY tourney_date DESC, rowid DESC
        """,
    ),
    (
        "Roland Garros R128",
        """
        SELECT rowid, tour, tourney_id, tourney_name, tourney_date, round,
               winner_name, loser_name, scrape_provider, source_match_date,
               is_upcoming
        FROM matches
        WHERE tourney_date LIKE '2026%'
          AND (LOWER(tourney_name) LIKE '%roland garros%' OR LOWER(tourney_name) LIKE '%french open%')
          AND round='R128'
          AND (winner_name LIKE '%Sinner%' OR loser_name LIKE '%Sinner%')
        ORDER BY tourney_date DESC, rowid DESC
        """,
    ),
]


def run_local():
    path = Path.home() / '.tennis_analytics' / 'data' / 'tennis.db'
    print('LOCAL_DB', path, path.exists())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for label, sql in LOCAL_SQL:
        rows = conn.execute(sql).fetchall()
        print(f'\n## LOCAL {label}: {len(rows)} rows')
        for row in rows:
            print(dict(row))
    conn.close()


def run_remote():
    from cloud.db import RemoteConnection

    conn = RemoteConnection()
    try:
        for label, sql in LOCAL_SQL:
            rows = conn.execute(sql).fetchall()
            print(f'\n## REMOTE {label}: {len(rows)} rows')
            for row in rows:
                print(row)
    finally:
        conn.close()


if __name__ == '__main__':
    run_local()
    print('\n' + '=' * 60)
    run_remote()
