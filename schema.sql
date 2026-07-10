-- DickGrow Bot schema
-- Run this once against your Supabase Postgres database
-- (the bot also auto-runs this on startup, so manual execution is optional)

create table if not exists users (
    telegram_id     bigint primary key,
    username        text,
    first_name      text,
    height_cm       integer not null default 0,
    last_grow       timestamptz,
    grow_streak     integer not null default 0,
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
