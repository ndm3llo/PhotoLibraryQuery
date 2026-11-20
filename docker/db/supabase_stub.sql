-- Minimal stand-in for the parts of Supabase that our migrations and RLS policies rely on.
-- Used for local dev and CI only. NEVER run this against a real Supabase project.
-- Idempotent: safe to apply repeatedly.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end $$;

create schema if not exists auth;

-- Supabase's user table (we only need id and email for FKs and tests).
create table if not exists auth.users (id uuid primary key, email text);

-- Same contract as Supabase: reads the JWT subject from the request settings.
create or replace function auth.uid() returns uuid
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

grant usage on schema public to anon, authenticated, service_role;
grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

-- Supabase grants broad table privileges to these roles by default and relies on RLS.
-- Mirror that so tests exercise the same (permissive) starting point.
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
