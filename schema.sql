-- Run once in the Supabase SQL editor.
create extension if not exists vector;

create table if not exists payment_failures (
  id uuid primary key default gen_random_uuid(),
  event_id text unique not null,          -- Stripe event id: makes webhook retries idempotent
  payment_intent_id text,
  customer_id text,
  amount bigint,
  currency text,
  decline_code text,
  failure_message text,
  category text,
  root_cause text,
  actions jsonb not null default '[]',
  logs jsonb not null default '[]',
  status text not null default 'received', -- received|link_sent|manual_review|recovered|failed
  embedding vector(1024),                  -- voyage-3.5
  created_at timestamptz not null default now(),
  recovered_at timestamptz
);
create index if not exists payment_failures_customer on payment_failures (customer_id);
create index if not exists payment_failures_pi on payment_failures (payment_intent_id);

-- Only the backend (service key) touches this table.
alter table payment_failures enable row level security;
-- New Supabase projects don't auto-grant API roles; give the service key access only.
grant select, insert, update, delete on payment_failures to service_role;

-- Same customer's past failures first, then the most similar failures overall.
create or replace function match_failures(query_embedding vector(1024), customer text, exclude uuid, k int default 5)
returns table (id uuid, customer_id text, decline_code text, category text, status text, created_at timestamptz, similarity float)
language sql stable as $$
  select f.id, f.customer_id, f.decline_code, f.category, f.status, f.created_at,
         1 - (f.embedding <=> query_embedding)
  from payment_failures f
  where f.embedding is not null and f.id <> exclude
  order by coalesce(f.customer_id = customer, false) desc, f.embedding <=> query_embedding
  limit k
$$;
