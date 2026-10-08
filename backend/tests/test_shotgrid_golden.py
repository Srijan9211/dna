"""The profile-driven ShotGrid provider behaves exactly like the original.

``tests/golden/shotgrid_calls.json`` was recorded from the hard-coded
implementation (see ``tests/golden/generate_shotgrid_calls.py``).  Each scenario
there is a provider operation with every ShotGrid call it made and the DNA
result it returned.  The refactored provider must reproduce all of it: same
queries, same fields, same payloads, same entities, same errors.
"""

import json
import sys
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
sys.path.insert(0, str(GOLDEN_DIR))

from sg_scenarios import run_scenarios  # noqa: E402

import dna.prodtrack_providers.shotgrid as shotgrid_module  # noqa: E402
from dna import configsys  # noqa: E402

RECORDED = json.loads((GOLDEN_DIR / "shotgrid_calls.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def replayed():
    configsys.reset_config()
    try:
        return run_scenarios(shotgrid_module)
    finally:
        configsys.reset_config()


def test_the_same_scenarios_are_covered(replayed):
    assert set(replayed) == set(RECORDED)


@pytest.mark.parametrize("scenario", sorted(RECORDED))
def test_scenario_matches_the_original_implementation(replayed, scenario):
    expected, actual = RECORDED[scenario], replayed[scenario]
    assert actual["calls"] == expected["calls"], "ShotGrid calls differ"
    assert actual["result"] == expected["result"], "returned entities/errors differ"


def test_packaged_profile_reproduces_the_original_field_mapping():
    from dna.prodtrack_providers.profile import load_profile

    golden = json.loads((GOLDEN_DIR / "shotgrid_field_mapping.json").read_text("utf-8"))
    mapping = load_profile("shotgrid").field_mapping()
    assert mapping == golden
    for kind in golden:  # key order is the order fields are requested in
        assert list(mapping[kind]["fields"]) == list(golden[kind]["fields"])
        assert list(mapping[kind]["linked_fields"]) == list(
            golden[kind]["linked_fields"]
        )
