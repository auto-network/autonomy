"""Dashboard DAO layer — database access for dashboard state.

Modules:
- dashboard_db: SQLite read-write access to data/dashboard.db (session identity)
- dispatch: SQLite read-only access to data/dispatch.db
- beads: pymysql read-only access to Dolt on :3306
- sessions: Session queries backed by dashboard.db + graph.db
"""
