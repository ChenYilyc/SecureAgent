create table secureagent.admin_events (
    event_id bigint generated always as identity primary key,
    created_at timestamptz not null default current_timestamp,
    request_id text not null,
    actor_login text not null,
    action text not null,
    project_id text not null,
    target_login text,
    project_role text check (project_role in ('clerk', 'reviewer', 'employee')),
    check (
        (action = 'project_created' and target_login is null and project_role is null)
        or (action = 'user_created' and target_login is not null and project_role is not null)
    )
);
