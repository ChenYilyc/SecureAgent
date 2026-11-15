create table secureagent.document_versions (
    project_id text not null references secureagent.projects (project_id),
    document_id text not null,
    revision text not null,
    title text not null,
    status text not null default 'draft' check (status in ('draft', 'effective', 'superseded')),
    extension text not null check (extension in ('.md', '.docx', '.pdf')),
    original_bytes bytea not null,
    sha256 text generated always as (encode(sha256(original_bytes), 'hex')) stored,
    chunks jsonb not null check (jsonb_typeof(chunks) = 'array'),
    events jsonb not null default '[]'::jsonb check (jsonb_typeof(events) = 'array'),
    primary key (project_id, document_id, revision)
);

create unique index document_versions_one_effective
    on secureagent.document_versions (project_id, document_id) where status = 'effective';

revoke all on secureagent.document_versions from public, anon, authenticated;
