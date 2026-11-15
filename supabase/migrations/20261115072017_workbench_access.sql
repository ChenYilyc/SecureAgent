create schema if not exists secureagent;

revoke all on schema secureagent from public, anon, authenticated;

create table secureagent.wb_accounts (
    login text primary key,
    salt bytea not null,
    digest bytea not null,
    is_company_admin boolean not null default false,
    is_active boolean not null default true
);

create table secureagent.projects (
    project_id text primary key,
    display_name text not null check (btrim(display_name) <> '')
);

create table secureagent.project_members (
    login text not null references secureagent.wb_accounts (login),
    project_id text not null references secureagent.projects (project_id),
    role text not null check (role in ('clerk', 'reviewer', 'employee')),
    primary key (login, project_id)
);