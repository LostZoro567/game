-- Run this in the Supabase SQL editor to set up the game tables.

create table if not exists players (
    user_id       bigint primary key,          -- telegram user id
    username      text not null default 'Adventurer',
    hp            integer not null default 100,
    max_hp        integer not null default 100,
    gold          integer not null default 0,
    silver        integer not null default 0,
    sword_level   integer not null default 0,   -- 0..10
    armor_level   integer not null default 0,   -- 0..10
    downed        boolean not null default false,
    last_hp_regen timestamptz not null default now(),
    created_at    timestamptz not null default now()
);

-- one row per group chat, tracks which main boss they're on and its HP
create table if not exists group_boss_state (
    chat_id       bigint primary key,
    boss_index    integer not null default 0,   -- 0=Goblin King .. 3=Dragon
    current_hp    integer not null default 0,   -- 0 means "not started yet, needs init"
    last_reset    timestamptz not null default now()
);

-- who's actively joined the current raid in a group
create table if not exists group_members (
    chat_id    bigint not null,
    user_id    bigint not null,
    joined_at  timestamptz not null default now(),
    primary key (chat_id, user_id)
);

-- daily dungeon attempt counter (solo play)
create table if not exists dungeon_attempts (
    user_id       bigint not null,
    play_date     date not null default current_date,
    attempts_used integer not null default 0,
    primary key (user_id, play_date)
);

-- per-user cooldown on main boss attacks (prevents spam-killing the shared boss)
create table if not exists attack_cooldowns (
    user_id     bigint primary key,
    last_attack timestamptz not null default now()
);
