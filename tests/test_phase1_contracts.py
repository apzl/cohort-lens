"""Approval and contract invariants for current and later phases."""

from datetime import datetime, timezone

import pytest

from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    VariantSpec,
    canonical_json,
    contract_digest,
    validate_contract_approval,
)


def test_contract_digest_is_canonical(phase1_contract: AnalysisContract) -> None:
    round_tripped = AnalysisContract.model_validate_json(canonical_json(phase1_contract))
    assert contract_digest(round_tripped) == contract_digest(phase1_contract)


def test_changed_variant_invalidates_approval(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    changed = phase1_contract.model_copy(
        update={
            "variant": VariantSpec(
                reference_build="SYNTHETIC_V1",
                key="demo:1:2000:C:T",
                effect_allele="T",
            )
        }
    )
    with pytest.raises(ValueError, match="hash does not match"):
        validate_contract_approval(changed, phase1_approval)


def test_changed_phenotype_invalidates_approval(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    changed = phase1_contract.model_copy(update={"phenotype_id": "other_biomarker"})
    with pytest.raises(ValueError, match="hash does not match"):
        validate_contract_approval(changed, phase1_approval)


def test_changed_template_invalidates_approval(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    changed = phase1_contract.model_copy(update={"template_id": "other_template"})
    with pytest.raises(ValueError, match="hash"):
        validate_contract_approval(changed, phase1_approval)


def test_expired_approval_is_rejected(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    with pytest.raises(ValueError, match="expired"):
        validate_contract_approval(
            phase1_contract,
            phase1_approval,
            now=datetime(2100, 1, 1, tzinfo=timezone.utc),
        )
