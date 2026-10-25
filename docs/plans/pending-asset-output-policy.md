# D1b0: preserve output policy for pending Assets

Follow the [workflow adapter](../development-workflow-adapter.md) and [stage plan](sustained-engineering-delivery.md). Status: exact five-file correctness implementation independently Passed; root full/build/installed and separate independent overall review Passed; final metadata/source-package light closure is tracked in report113. Luna `plan_audit_v2` authored plan and implementation; root, not the five-file author, reviewed both. Original Ready plan `2cbde43dc3075c9b84e38833d57f866e0315e0db8344132659e9e808662b95ec`; independent Ready `ac310c92a3f3f391b0e5c3037a16e29b986d43057ba5fdf702315051c0967f3c`.

## Confirmed defect and scope

Signed ordinary publication on both bare3d2 and corrected D1a completed a two-output Contract when one pending Asset was business output and the other had origin tool, mcp or label_placeholder. The shared stored-output predicate was false. The all-human control was valid. The initial private probe's wrong import failed setup and is not product evidence.

Fix only two production owners (`core/output_satisfaction.py`, `core/fact_reducer.py`) and three existing tests (`test_output_satisfaction.py`, `test_fact_reducer.py`, architecture terminal facts). The shared helper adds keyword-only `pending_assets: tuple[Asset, ...] = ()`. FactReducer passes the actual tuple to output completion and descendant-output checks. It keeps names for activation, where observations are valid inputs. All same-name values remain available; no last-value overwrite.

Ordinary output must pass the existing business policy. System/recovery output requires its own Contract as producer. Independent output still needs exact existing qualification and a matching stored eligible Asset; pending presence is insufficient. Other callers and legacy `extra_output_names` retain their old behavior. No Store/schema/cache/index, authority, claim, acceptance or scheduler change. This package makes no performance optimization or qualification claim.

## Matrix and exact acceptance

The five-file scope and API were independently Ready before implementation. Freeze original source1021 map38ace05fe690aa10869957826a90ab73de687d43eab865aa0154b704ac34c7b0 and236 Runtime map2fe67cd6343846da2d104337d47dbb1ac81da3503c55c0e5ddc668ed8ff6350f. D1a metadata and unrelated transport/observer work are not start dependencies.

Tests cover invalid origins and all non-output prefixes, both batch orders, same-name valid/invalid values, stored-plus-pending positives, exact system/recovery creator, independent qualification, legitimate activation, valid child completion, bad pending child cancellation, terminal intermediary traversal and live outer verifier preservation. Real signed SQLite tests check accepted Assets, terminal outcomes, reopen and strict rebuild. Author retains an original48-test red with23failures;16 are the new API's absence and seven reach wrong behavior. This is not23 distinct defects or the later final snapshot.

Root independently reconstructed final five-file map `a3f6b0e509ea669c5377f1c7cecffc11aee5877ee983a90025d6e075ac368524`; full1021 map `b94cd3cec751f3db930007d2b86e5336f92caafc8744702c0d99c358e59f67f9`;236 Runtime map `a420a4ea9e02d5033afcc176a848849feb1237464d0e7591ad81af38d27f8b08`. Independent49 tests, four real signed origin vectors and five-file Ruff/format pass. Root additionally created a genuinely bad terminal using old code, reopened/rebuilt with new code, and verified the original fact ID/digest remains unchanged. Review `57faf18ef11d07b11b157d2cf28c92b9ba920bdc4541aa39262b6876d5fea229` Passed the component only.

## Integration and limits

Root's predeclared combination is corrected D1a, G0b, D0c and D1b0. Independent overall review must bind the exact final combined code/docs/artifacts. No provider/key/Redis or performance was used for this component. [Change125](../../changes/125-pending-asset-output-policy.md) records implemented behavior; [report113](../../reports/113-asset-context-direct-transport-development-2026-10-25.md) owns combined evidence.

Historical terminals are immutable assertions, including previously wrong completion; rebuild does not repair them. Follow-on work uses authorized new signed tasks. All writer processes must use corrected code to benefit: already-running old versions retain their old projection behavior. No schema gate is added. Asset reducer H/CPU locality and claim paths remain open.

Root预声明四切片及三文件hook维护组合：源码3,532项与树外656项、Ruff/格式、构建及安装驱动检查通过。717文件代码/夹具摘要e87654c18b07100d555247e2e15989ffaa28c598e8e602d43158f1c2ee9da9ca；非作者对准确组合独立总体验收Passed；最终验收元数据/源码包按同一717文件代码快照轻闭合，详见report113。以上不是本组件作者单独完成的全套，也不替代本轮未做的真实provider/整体规模资格。
