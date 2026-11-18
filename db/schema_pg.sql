-- Postgres schema for the deployed app (Supabase). Applied once as a migration; the same tables
-- as db/schema.sql (SQLite) plus the run trails, the L6 reply agent and the L5 feedback loop.
create table if not exists runs (run_id text primary key, started_at text not null, finished_at text, dry_run integer not null, summary_json text);
create table if not exists companies (run_id text not null, domain text not null, status text not null, failing_gate text, reason text, score integer, updated_at text not null, primary key (run_id, domain));
create table if not exists pages (id bigserial primary key, domain text not null, url text not null, action text not null, http_status integer, outcome text not null, clean_text text, fetched_at text not null);
create table if not exists facts (id bigserial primary key, run_id text not null, domain text not null, key text not null, value_json text not null, quote text not null, source_url text not null, source_type text not null, fact_date text, created_at text not null);
create table if not exists dropped_facts (id bigserial primary key, run_id text not null, domain text not null, key text not null, value_json text, quote text, source_url text, reason text not null, created_at text not null);
create table if not exists planner_decisions (id bigserial primary key, run_id text not null, domain text not null, pass_no integer not null, decided_by text not null, actions_json text not null, reasoning text, created_at text not null);
create table if not exists gate_results (run_id text not null, domain text not null, gate text not null, status text not null, reason text not null, primary key (run_id, domain, gate));
create table if not exists llm_calls (id bigserial primary key, run_id text, domain text, purpose text not null, model text not null, input_tokens integer, output_tokens integer, ok integer not null, created_at text not null);
create table if not exists hunter_cache (domain text primary key, http_status integer not null, response_json text not null, fetched_at text not null);
create table if not exists hunter_company_cache (domain text primary key, http_status integer not null, response_json text not null, fetched_at text not null);
create table if not exists hunter_verify_cache (email text primary key, http_status integer not null, response_json text not null, fetched_at text not null);
create table if not exists hunter_usage (month text primary key, searches integer not null);
create table if not exists contacts (run_id text not null, domain text not null, first_name text, last_name text, email text, position text, matched_title text, reason text not null, primary key (run_id, domain));
create table if not exists drafts (id bigserial primary key, run_id text not null, domain text not null, status text not null, subject text, body text, pain_points_json text, hook_fact_id integer, hook_url text, reasoning text, created_at text not null);
create table if not exists sends (id bigserial primary key, run_id text not null, domain text not null, recipient text not null, mode text not null, message_id text, created_at text not null);
create table if not exists trails (domain text primary key, trail jsonb not null, updated_at timestamptz not null default now());
create table if not exists agent_threads (domain text primary key, state jsonb not null, status text not null default 'active', updated_at timestamptz not null default now());
create table if not exists agent_events (id bigserial primary key, domain text not null, node text not null, tool text, outcome text not null, detail text, created_at timestamptz not null default now());
create table if not exists feedback (id bigserial primary key, domain text not null, draft_version integer not null default 1, rating integer, edited_body text, notes text, reply_status text, lessons_used jsonb not null default '[]', created_at timestamptz not null default now());
create table if not exists lessons (id bigserial primary key, text text not null, rationale text, evidence jsonb not null default '[]', status text not null default 'pending', created_at timestamptz not null default now(), decided_at timestamptz);
-- Row-level security on every table, with no policies: Supabase's public Data API sees nothing.
-- The app connects directly as the database owner (DATABASE_URL), which bypasses RLS.
