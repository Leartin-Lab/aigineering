"""SQLite schema declarations for the reference Store adapter."""

from __future__ import annotations

DDL_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

DDL_CREATE_ASSETS = """
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    created_by TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'system',
    trust_tier TEXT NOT NULL DEFAULT 'untrusted',
    minted_by TEXT NOT NULL DEFAULT '',
    source_uri TEXT NOT NULL DEFAULT '',
    signed_by TEXT NOT NULL DEFAULT '',
    signer_kind TEXT NOT NULL DEFAULT 'deterministic',
    provenance_seal TEXT NOT NULL DEFAULT '',
    promptable INTEGER NOT NULL DEFAULT 1,
    disclosure_view TEXT NOT NULL DEFAULT 'original',
    definition_hash TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    keep_flag INTEGER NOT NULL DEFAULT 0,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    tombstoned_at TEXT,
    lineage_id TEXT NOT NULL DEFAULT '',
    derivation_version TEXT NOT NULL DEFAULT '',
    range_spec TEXT NOT NULL DEFAULT ''
)
"""

DDL_CREATE_CONTRACTS = """
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    inputs TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]',
    activation TEXT NOT NULL DEFAULT '',
    budget INTEGER NOT NULL DEFAULT 0,
    tool_scope TEXT NOT NULL DEFAULT '[]',
    labels TEXT NOT NULL DEFAULT '[]',
    context_asset_ids TEXT NOT NULL DEFAULT '[]',
    worker_capabilities TEXT NOT NULL DEFAULT '[]',
    worker_pools TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'human',
    minting_authority TEXT NOT NULL DEFAULT '[]',
    sensitive_input_policy TEXT,
    acceptance_policy TEXT
)
"""

DDL_CREATE_TRACE_EVENTS = """
CREATE TABLE IF NOT EXISTS trace_events (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    contract_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    disclosed_assets TEXT NOT NULL DEFAULT '[]',
    accepted_fragments TEXT NOT NULL DEFAULT '[]',
    accepted_asset_names TEXT NOT NULL DEFAULT '[]',
    rejected_fragments TEXT NOT NULL DEFAULT '[]',
    worker_id TEXT,
    candidate_raw TEXT,
    authority_policy TEXT,
    authority_result TEXT,
    budget_remaining INTEGER NOT NULL DEFAULT 0,
    relation_type TEXT,
    relation_target TEXT,
    timestamp TEXT NOT NULL DEFAULT '',
    usage_metadata TEXT
)
"""

DDL_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    root_contract_id TEXT NOT NULL DEFAULT '',
    contract_ids TEXT NOT NULL DEFAULT '[]',
    asset_ids TEXT NOT NULL DEFAULT '[]',
    trace_ids TEXT NOT NULL DEFAULT '[]',
    config_snapshot TEXT NOT NULL DEFAULT '{}',
    worker_snapshot TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
)
"""

DDL_CREATE_CLAIMS = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL DEFAULT '',
    replacement_asset_id TEXT NOT NULL DEFAULT '',
    definition_hash TEXT NOT NULL DEFAULT '',
    claim_type TEXT NOT NULL DEFAULT '',
    signed_by TEXT NOT NULL DEFAULT '',
    provenance_seal TEXT NOT NULL DEFAULT '',
    lineage_id TEXT NOT NULL DEFAULT ''
)
"""

DDL_CREATE_REPLACEMENT_CLAIMS = """
CREATE TABLE IF NOT EXISTS replacement_claims (
    claim_id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL,
    replacement_asset_id TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    signed_by TEXT DEFAULT '',
    provenance_seal TEXT DEFAULT ''
)
"""

DDL_CREATE_WORKER_CLAIMS = """
CREATE TABLE IF NOT EXISTS worker_claims (
    claim_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    status TEXT NOT NULL,
    package_id TEXT NOT NULL DEFAULT '',
    epoch INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

DDL_CREATE_IDEMPOTENCY = """
CREATE TABLE IF NOT EXISTS idempotency_records (
    contract_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (contract_id, idempotency_key)
)
"""

DDL_CREATE_ACTIVATION_REFS = """
CREATE TABLE IF NOT EXISTS contract_activation_refs (
    contract_id TEXT NOT NULL,
    asset_name TEXT NOT NULL,
    PRIMARY KEY (contract_id, asset_name)
)
"""

DDL_CREATE_RUNTIME_LIFECYCLE = """
CREATE TABLE IF NOT EXISTS runtime_lifecycle (
    runtime_id TEXT PRIMARY KEY,
    heartbeat_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL,
    stopped_at TEXT
)
"""

DDL_CREATE_DECLARED_OUTPUTS = """
CREATE TABLE IF NOT EXISTS contract_declared_outputs (
    contract_id TEXT NOT NULL,
    output_name TEXT NOT NULL,
    PRIMARY KEY (contract_id, output_name)
)
"""

DDL_CREATE_WORKER_REGISTRATIONS = """
CREATE TABLE IF NOT EXISTS worker_registrations (
    worker_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL DEFAULT '',
    key_id TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '[]',
    pools TEXT NOT NULL DEFAULT '[]',
    profile_id TEXT NOT NULL DEFAULT '',
    capacity INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    version TEXT NOT NULL DEFAULT '1',
    updated_at TEXT NOT NULL
)
"""

DDL_CREATE_RUNTIME_RECORDS = """
CREATE TABLE IF NOT EXISTS runtime_records (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    record_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    causal_parents TEXT NOT NULL DEFAULT '[]',
    recorded_at TEXT NOT NULL
)
"""

DDL_CREATE_ASSET_CONTENTS = """
CREATE TABLE IF NOT EXISTS asset_contents (
    content_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    source_record_id TEXT NOT NULL
)
"""

DDL_CREATE_ASSET_DEFINITIONS = """
CREATE TABLE IF NOT EXISTS asset_definitions (
    definition_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_record_id TEXT NOT NULL
)
"""

DDL_CREATE_DEFINITION_CONTENT_ASSERTIONS = """
CREATE TABLE IF NOT EXISTS asset_definition_content_assertions (
    assertion_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_record_id TEXT NOT NULL
)
"""

DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_assets_definition_hash ON assets(definition_hash)",
    "CREATE INDEX IF NOT EXISTS idx_assets_content_hash ON assets(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_assets_lineage_id ON assets(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name)",
    "CREATE INDEX IF NOT EXISTS idx_assets_created_by ON assets(created_by)",
    "CREATE INDEX IF NOT EXISTS idx_assets_tombstoned ON assets(tombstoned)",
    "CREATE INDEX IF NOT EXISTS idx_contracts_parent_id ON contracts(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_contract_id ON trace_events(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_trace_events_event_type ON trace_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_worker_claims_contract_status ON worker_claims(contract_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_claims_one_active ON worker_claims(contract_id) WHERE status = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_idempotency_contract ON idempotency_records(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_activation_refs_asset ON contract_activation_refs(asset_name)",
    "CREATE INDEX IF NOT EXISTS idx_declared_outputs_name ON contract_declared_outputs(output_name)",
    "CREATE INDEX IF NOT EXISTS idx_worker_registrations_enabled ON worker_registrations(enabled)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_records_type_revision ON runtime_records(record_type, revision)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_records_one_genesis ON runtime_records(record_type) WHERE record_type = 'domain.genesis'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_records_one_terminal_per_contract ON runtime_records(json_extract(payload_json, '$.contract_id')) WHERE record_type = 'lifecycle.terminal'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_records_one_actor_key ON runtime_records(json_extract(payload_json, '$.actor_id'), json_extract(payload_json, '$.key_id')) WHERE record_type = 'actor.authorized'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_records_one_actor_revocation ON runtime_records(json_extract(payload_json, '$.actor_id'), json_extract(payload_json, '$.key_id')) WHERE record_type = 'actor.revoked'",
    "CREATE INDEX IF NOT EXISTS idx_asset_assertions_definition ON asset_definition_content_assertions(definition_id)",
    "CREATE INDEX IF NOT EXISTS idx_asset_assertions_content ON asset_definition_content_assertions(content_id)",
]
TABLE_DDL = (
    DDL_CREATE_SCHEMA_VERSION,
    DDL_CREATE_ASSETS,
    DDL_CREATE_CONTRACTS,
    DDL_CREATE_TRACE_EVENTS,
    DDL_CREATE_SESSIONS,
    DDL_CREATE_CLAIMS,
    DDL_CREATE_REPLACEMENT_CLAIMS,
    DDL_CREATE_WORKER_CLAIMS,
    DDL_CREATE_IDEMPOTENCY,
    DDL_CREATE_ACTIVATION_REFS,
    DDL_CREATE_DECLARED_OUTPUTS,
    DDL_CREATE_RUNTIME_LIFECYCLE,
    DDL_CREATE_WORKER_REGISTRATIONS,
    DDL_CREATE_RUNTIME_RECORDS,
    DDL_CREATE_ASSET_CONTENTS,
    DDL_CREATE_ASSET_DEFINITIONS,
    DDL_CREATE_DEFINITION_CONTENT_ASSERTIONS,
)
