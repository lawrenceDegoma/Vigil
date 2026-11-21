-- New notification kind for owner-requested test texts. Separate migration: a new
-- enum value cannot be used in the same transaction that adds it.
alter type public.notification_kind add value if not exists 'test_alert';
