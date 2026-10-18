"""Every smoke-catalog label has an executable oracle. These runs use the fake provider."""

from __future__ import annotations

import pytest

from resolveai.evaluation.catalog import (
    ADAPTIVE_SETUPS,
    catalog_ids,
    execute_catalog_case,
    use_baseline,
)

ADAPTIVE = [case_id for case_id in catalog_ids() if case_id[:4] in {"inv-", "pay-"}]


@pytest.mark.parametrize("case_id", catalog_ids())
def test_catalog_case_is_executable(
    case_id, client, settings, manifest, api_app, restore_runtime
) -> None:
    del restore_runtime
    execute_catalog_case(case_id, client, settings, manifest, api_app)


@pytest.mark.parametrize("case_id", ADAPTIVE)
def test_b1_reaches_the_same_adaptive_outcome(
    case_id, client, settings, manifest, api_app, restore_runtime
) -> None:
    del restore_runtime
    use_baseline(api_app)
    execute_catalog_case(case_id, client, settings, manifest, api_app)


def test_adaptive_families_cover_the_core_minimum() -> None:
    invoice = [item for item in ADAPTIVE_SETUPS if item["family"] == "invoice_consistency"]
    payment = [item for item in ADAPTIVE_SETUPS if item["family"] == "payment_progress"]
    setups = {(item["fixture"], item["mutation"]) for item in ADAPTIVE_SETUPS}
    assert len(ADAPTIVE_SETUPS) >= 12
    assert len(setups) >= 12
    assert len(invoice) >= 4
    assert len({(item["fixture"], item["mutation"]) for item in invoice}) >= 4
    assert len(payment) >= 4
    assert len({(item["fixture"], item["mutation"]) for item in payment}) >= 4
    assert len(catalog_ids()) == 24
