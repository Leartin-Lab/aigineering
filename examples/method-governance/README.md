# Method governance fixture

This example runs the reusable method path with an explicit deterministic
fixture Worker. It creates a fresh domain and SQLite store, publishes a method
package, executes two held-out cases, assesses the run, publishes an
independent `method.review.v1` attestation, reuses the qualified method, and
checks that closing, reopening, and rebuilding SQLite preserves the runtime
materialization digest.

The fixture demonstrates Candidate signing, authority, claims, projection, and
independent method qualification. It does not demonstrate real model quality.
The command refuses an existing target directory and refuses to run without
`--worker fixture`.

```bash
python examples/method-governance/demo.py \
  --worker fixture \
  --directory ./aig-method-governance-demo
```

For a real Worker, import the checked-in package and cases with the CLI, create
an ordinary method Contract, prepare its test run, and execute the returned
root task through a normal configured fleet:

```bash
aig method import examples/method-governance/method.json --json
aig method cases examples/method-governance/cases.json --name heldout-counts --json
aig method instantiate METHOD_ASSET_ID \
  --input source=SOURCE_ASSET_ID \
  --output assessment=assessment \
  --name trial --budget 2 --allow-unverified --json
aig method test METHOD_ASSET_ID --cases CASES_ASSET_ID \
  --name method-tests --budget 1 --json
aig fleet run --config workers.toml --task TEST_CONTRACT_ID --json
aig method assess TEST_RUN_ID --json
```

After an independent review attestation, instantiate the method again with
`--evaluation EVALUATION_ID`. The regular fleet remains responsible for
claiming and executing ordinary Contracts.
