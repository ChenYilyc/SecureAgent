alter table secureagent.wb_accounts
    add column access_revision bigint not null default 1 check (access_revision > 0),
    add column deleted_at timestamptz,
    add constraint wb_accounts_retired_state
        check (deleted_at is null or (not is_active and not is_company_admin));

alter table secureagent.admin_events
    alter column project_id drop not null,
    add column roles_before jsonb check (jsonb_typeof(roles_before) = 'object'),
    add column roles_after jsonb check (jsonb_typeof(roles_after) = 'object');

-- The preceding migration let PostgreSQL name the multi-column action constraint.
-- Resolve that constraint by column, leaving the independent role check in place.
do $migration$
declare
    old_shape name;
begin
    select c.conname into strict old_shape
    from pg_catalog.pg_constraint as c
    join pg_catalog.pg_attribute as a
      on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
    where c.conrelid = 'secureagent.admin_events'::regclass
      and c.contype = 'c' and a.attname = 'action';
    execute format('alter table secureagent.admin_events drop constraint %I', old_shape);
end;
$migration$;

update secureagent.admin_events
set roles_after = jsonb_build_object(project_id, project_role)
where action = 'user_created';

alter table secureagent.admin_events add constraint admin_events_action_shape check (
    (action = 'project_created' and project_id is not null and target_login is null
     and project_role is null and roles_before is null and roles_after is null)
    or
    (action = 'user_created' and target_login is not null and roles_before is null
     and ((project_id is not null and project_role is not null
           and (roles_after is null or roles_after = jsonb_build_object(project_id, project_role)))
          or (project_id is null and project_role is null
              and roles_after is not null and roles_after <> '{}'::jsonb)))
    or
    (action in ('memberships_changed', 'user_deleted') and target_login is not null
     and project_id is null and project_role is null
     and roles_before is not null and roles_after is not null
     and (action <> 'user_deleted' or roles_after = '{}'::jsonb))
);
