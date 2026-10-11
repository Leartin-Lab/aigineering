"""Read-only Store facade over one immutable projection context."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.types import Asset, Contract


class ProjectionStoreView:
    """Implement the Store read shape without retaining a live Store."""

    def __init__(self, context: EffectProjectionContext) -> None:
        self._assets = context.assets
        self._contracts = {contract.id: contract for contract in context.contracts}
        self._records = context.runtime_records

    def get_asset(self, asset_id: str) -> Asset | None:
        return next((asset for asset in self._assets if asset.id == asset_id), None)

    def get_assets_by_ids(self, asset_ids: tuple[str, ...]) -> list[Asset]:
        wanted = set(asset_ids)
        return [asset for asset in self._assets if asset.id in wanted]

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return [asset for asset in self._assets if asset.name == name]

    def get_contract(self, contract_id: str) -> Contract | None:
        return self._contracts.get(contract_id)

    def scan_runtime_records(self, *, record_type: str | None = None):
        return [
            (revision, record)
            for revision, record in enumerate(self._records, start=1)
            if record_type is None or record.record_type == record_type
        ]

    def get_claims_for_replacement_asset(self, asset_id: str):
        from aigineering.core.asset_versions import replacement_claim_from_record

        return [
            claim
            for record in self._records
            if (claim := replacement_claim_from_record(record)) is not None
            and claim.replacement_asset_id == asset_id
        ]

    def get_asset_definitions(self) -> list[dict]:
        return [
            dict(record.payload["definition"])
            for record in self._records
            if record.record_type == "asset.definition.published"
        ]

    def get_content_objects(self) -> list[dict]:
        return [
            dict(record.payload["content"])
            for record in self._records
            if record.record_type == "asset.content.published"
        ]

    def get_definition_content_assertions(
        self, *, definition_id: str = "", content_id: str = ""
    ) -> list[dict]:
        result = []
        for record in self._records:
            if record.record_type != "asset.definition-content.asserted":
                continue
            assertion = record.payload.get("assertion")
            if not isinstance(assertion, Mapping):
                continue
            if definition_id and assertion.get("definition_id") != definition_id:
                continue
            if content_id and assertion.get("content_id") != content_id:
                continue
            result.append(dict(assertion))
        return result
