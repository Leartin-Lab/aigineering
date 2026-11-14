# Exact JSON value citations

The business artifact adapter can quote one scalar from an ordinary, disclosed
JSON Asset. `aig artifact cite ASSET_ID --pointer /result/value --name evidence`
uses the existing signed publication path. Page-based citations remain supported;
selector modes cannot be mixed.

The evidence envelope records `asset-json-pointer-v1`, one exact parent ID,
source UTF-8 SHA-256, JSON Pointer, scalar type and quote. Consumers recompute
all fields. Numbers retain their original JSON lexeme; strings contain decoded
text. Duplicate keys, nonfinite values, invalid pointers, composite targets and
unavailable sources fail closed. A JSON source is a quotation leaf, while any
separate artifact role still requires its complete business closure.

Existing Markdown report footnotes link to escaped evidence pages. Complete JSON
sources require `--include-sources`. Source producer metadata is an assertion,
not authenticated generating-task proof. Quotation does not establish semantic
support or canonical derivation qualification, and task completion rules are
unchanged. No scheduler, effect, durable table or authority is added.

Artifact read operations select exact Asset publication facts and bounded causal
ancestors under a deferred read snapshot. The Store holds its shared connection
throughout that interval without acquiring the SQLite writer lock. Startup's
existing implicit asset-projection rebuild remains separate debt; this change
does not claim history-independent connection startup.

Behavior checks cover signed publication, precise scalar selection, forged and
withheld evidence, mixed source/artifact roles, CLI mode errors, escaped export,
manifest hashes, reconstruction and read concurrency. Provider execution and
automatic model citation compliance are not established by these checks.
