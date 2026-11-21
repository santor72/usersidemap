"""Serialize snapshot deletion with publication and creation of dataset pins."""

DATASET_LIFECYCLE_LOCK = 79124002


def lock_dataset_lifecycle(conn):
    # Call inside a transaction, before dataset/pointer row or table locks.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (DATASET_LIFECYCLE_LOCK,))
