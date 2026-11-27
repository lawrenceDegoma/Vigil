-- New notification kind: the enrollment confirmation text sent when a phone number
-- is added (or re-enabled). Separate migration: a new enum value cannot be used in
-- the same transaction that adds it.
alter type public.notification_kind add value if not exists 'opt_in_confirmation';
