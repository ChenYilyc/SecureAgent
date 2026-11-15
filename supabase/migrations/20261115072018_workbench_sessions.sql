create table secureagent.wb_sessions (
    token_hash text primary key,
    login text not null references secureagent.wb_accounts (login) on delete cascade,
    csrf text not null,
    expires_at timestamptz not null
);

create index wb_sessions_expires_at_idx on secureagent.wb_sessions (expires_at);
