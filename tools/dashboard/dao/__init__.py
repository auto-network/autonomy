"""Dashboard DAO layer — direct database access for read-only queries.

Two modules:
- dispatch: SQLite read-only access to data/dispatch.db
- beads: pymysql read-only access to Dolt on :3306
"""
