"""Record the ShotGrid golden fixture from a reference implementation.

Run this ONLY to (re)establish the reference behaviour, against a ``shotgrid.py``
whose behaviour you have decided is correct (the pre-refactor version, or a
deliberate behaviour change you have reviewed):

    git show <commit>:backend/src/dna/prodtrack_providers/shotgrid.py > ref_shotgrid.py
    python tests/golden/generate_shotgrid_calls.py ref_shotgrid.py

The result, ``shotgrid_calls.json``, is what ``test_shotgrid_golden.py`` holds the
current provider to.  Never regenerate it from the code under test.
"""

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

from sg_scenarios import run_scenarios  # noqa: E402


def main(reference: str) -> None:
    spec = importlib.util.spec_from_file_location("reference_shotgrid", reference)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reference_shotgrid"] = module
    spec.loader.exec_module(module)
    recorded = run_scenarios(module)
    out = HERE / "shotgrid_calls.json"
    out.write_text(
        json.dumps(recorded, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"recorded {len(recorded)} scenarios -> {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
