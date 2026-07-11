-- DickGrow Bot schema
-- Run this once against your Supabase Postgres database
-- (the bot also auto-runs this on startup, so manual execution is optional)

create table if not exists users (
    telegram_id     bigint primary key,
    username        text,
    first_name      text,
    height_cm       integer not null default 0,
    last_grow       timestamptz,
    loan_active     boolean not null default false,
    loan_amount     integer not null default 0,
    loan_taken_at   timestamptz,
    created_at      timestamptz not null default now()
);

create table if not exists growth_log (
    id              bigserial primary key,
    telegram_id     bigint not null references users(telegram_id),
    amount          integer not null,
    type            text not null, -- grow, attack_win, attack_loss, loan, loan_repay
    created_at      timestamptz not null default now()
);

create table if not exists challenges (
    id              bigserial primary key,
    chat_id         bigint not null,
    message_id      bigint,
    challenger_id   bigint not null references users(telegram_id),
    amount          integer not null,
    status          text not null default 'open', -- open, resolved
    winner_id       bigint,
    loser_id        bigint,
    resolved_at     timestamptz,
    created_at      timestamptz not null default now()
);

create index if not exists idx_growth_log_time on growth_log(created_at);
create index if not exists idx_growth_log_user on growth_log(telegram_id);
create index if not exists idx_challenges_status on challenges(status);

-- Migration: drop unused grow_streak column (was never read/written by the bot).
-- Safe no-op if already applied.
alter table users drop column if exists grow_streak;

-- ---- New feature columns on users ----
alter table users add column if not exists last_pray        timestamptz;
alter table users add column if not exists last_simp        timestamptz;
alter table users add column if not exists last_snitch      timestamptz;
alter table users add column if not exists last_gamble      timestamptz;

alter table users add column if not exists hex_active       boolean not null default false;
alter table users add column if not exists hex_expires_at   timestamptz;
alter table users add column if not exists last_hex_cast    timestamptz; -- caster's own cooldown

alter table users add column if not exists condom_until     timestamptz;

alter table users add column if not exists pussy_active     boolean not null default false;
alter table users add column if not exists pussy_started_at timestamptz;
alter table users add column if not exists pussy_accum      integer not null default 0;
alter table users add column if not exists pussy_chat_id    bigint;
alter table users add column if not exists pussy_hour2_announced boolean not null default false;

-- Tracks which users have used the bot in which chats, so /cursethisgroup only
-- ever picks victims that actually belong to that group.
create table if not exists chat_members (
    chat_id      bigint not null,
    telegram_id  bigint not null references users(telegram_id),
    primary key (chat_id, telegram_id)
);

-- One row per /cursethisgroup activation.
create table if not exists group_curses (
    id             bigserial primary key,
    chat_id        bigint not null,
    initiator_id   bigint not null references users(telegram_id),
    started_at     timestamptz not null default now(),
    ends_at        timestamptz not null
);

create index if not exists idx_group_curses_chat_started on group_curses(chat_id, started_at);

-- Individual scheduled hits belonging to a curse. scheduled_at is a random
-- moment inside the 1h curse window; a periodic job applies them as they come due.
create table if not exists curse_hits (
    id            bigserial primary key,
    curse_id      bigint not null references group_curses(id),
    telegram_id   bigint not null references users(telegram_id),
    amount        integer not null,
    is_initiator  boolean not null default false,
    scheduled_at  timestamptz not null,
    applied       boolean not null default false
);

create index if not exists idx_curse_hits_due on curse_hits(applied, scheduled_at);

-- Enforces "1 fuck per hour-window per actor per target" during a pussy period.
create table if not exists fuck_log (
    id           bigserial primary key,
    target_id    bigint not null references users(telegram_id),
    actor_id     bigint not null references users(telegram_id),
    hour_window  smallint not null, -- 1 or 2
    created_at   timestamptz not null default now(),
    unique (target_id, actor_id, hour_window)
);
