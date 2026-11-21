-- Security advisor 0014 (extension_in_public): pg_net was created without a schema
-- in 0300 and landed in `public`. Recreate it in `extensions`. Its functions live in
-- the `net` schema either way, so the vigil-dispatch cron job (net.http_post) is
-- unaffected. In-flight request rows in net.* are transient and may be discarded.
drop extension if exists pg_net;
create extension pg_net with schema extensions;
