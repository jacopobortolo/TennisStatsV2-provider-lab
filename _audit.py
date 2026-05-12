from tennis_app.core.database import TennisDatabase
db = TennisDatabase()
rows = db.conn.execute(
    "SELECT ranking_date, tour, COUNT(*) FROM rankings "
    "WHERE ranking_date >= '20250101' AND ranking_date <= '20269999' "
    "GROUP BY ranking_date, tour ORDER BY ranking_date"
).fetchall()
print(f"Total dated 2025-2026 snapshots: {len(rows)}")
print(f"First: {rows[0][0] if rows else None}, Last: {rows[-1][0] if rows else None}")
