-- Existing projects continue operating after the migration.
alter table secureagent.projects
    add column state text not null default 'active' check (state in ('active', 'archived', 'deleted')),
    add column lifecycle_revision bigint not null default 1 check (lifecycle_revision > 0);

alter table secureagent.admin_events drop constraint admin_events_action_shape;
alter table secureagent.admin_events add constraint admin_events_action_shape check (
    (action in ('project_created', 'project_archived', 'project_deleted')
     and project_id is not null and target_login is null
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
