"""Ring A drift pins and the meta-tests that keep the contract inventory honest.

Each contract's hermetic pin runs here as its own parametrized test, so an
accidental edit to a pinned constant reds a named test. The meta-tests enforce
that every contract has a pin and that every live-verifiable contract wires an
existing Ring B test (no dormant drift seam).
"""

from __future__ import annotations

import ast
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _contract_hw import require, require_privileged
from contract_inventory import CONTRACTS


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_ring_a_pin_holds(contract):
    contract.ring_a()


def test_contract_names_are_unique():
    names = [c.name for c in CONTRACTS]
    assert len(names) == len(set(names))


def test_9950x3d2_fixture_matches_dual_vcache_silicon(topo_9950x3d2):
    assert topo_9950x3d2.family == 26
    assert topo_9950x3d2.model == 0x44
    assert topo_9950x3d2.physical_cores == 16
    assert topo_9950x3d2.logical_cpus_count == 32
    assert topo_9950x3d2.ccds == 2
    assert topo_9950x3d2.is_x3d
    assert all(core.ccd == core.core_id // 8 for core in topo_9950x3d2.cores.values())
    assert all(core.logical_cpus == (core.core_id, core.core_id + 16) for core in topo_9950x3d2.cores.values())
    assert all(core.has_vcache for core in topo_9950x3d2.cores.values())


def test_every_contract_has_a_hermetic_pin():
    for c in CONTRACTS:
        assert callable(c.ring_a), f"{c.name}: no Ring A pin"


def test_ycruncher_component_test_pin_is_independent_of_production_mapping(monkeypatch):
    from corecycler.engine.backends import ycruncher

    contract = next(c for c in CONTRACTS if c.name == "ycruncher-component-tests")
    invalid = frozenset({"NOT-A-Y-CRUNCHER-TEST"})
    monkeypatch.setattr(ycruncher, "VALID_COMPONENT_TESTS", invalid)
    monkeypatch.setattr(
        ycruncher,
        "MODE_TO_ALGORITHMS",
        {mode: tuple(invalid) for mode in ycruncher.MODE_TO_ALGORITHMS},
    )

    with pytest.raises(AssertionError):
        contract.ring_a()


def _has_exact_node(path: Path, node: str) -> bool:
    scope: list[ast.stmt] = ast.parse(path.read_text(), filename=str(path)).body
    for part in node.split("::"):
        match = next(
            (
                item
                for item in scope
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == part
            ),
            None,
        )
        if match is None:
            return False
        scope = match.body
    return True


def test_no_dormant_drift_seam():
    tests_dir = Path(__file__).parent
    for c in CONTRACTS:
        if not c.live_verifiable:
            assert c.ring_b_test is None, f"{c.name}: not live-verifiable but names a Ring B test"
            continue
        assert c.ring_b_test is not None, f"{c.name}: live-verifiable but wires no Ring B test"
        rel, _, node = c.ring_b_test.partition("::")
        path = tests_dir / rel
        assert path.exists(), f"{c.name}: Ring B file missing: {rel}"
        assert node, f"{c.name}: Ring B test names no node: {c.ring_b_test}"
        assert _has_exact_node(path, node), f"{c.name}: Ring B node missing: {c.ring_b_test}"


class TestTheNeverSilentSkipPolicy:
    """The guard that decides whether a live check is enforced or waved through."""

    def _hw(self, monkeypatch, *, contracts=False, privileged=False):
        import _contract_hw

        monkeypatch.setattr(_contract_hw, "HW_CONTRACTS", contracts)
        monkeypatch.setattr(_contract_hw, "HW_PRIVILEGED", privileged)

    def test_a_present_resource_is_never_in_the_way(self, monkeypatch):
        self._hw(monkeypatch, contracts=True, privileged=True)
        require(True, "present")
        require_privileged(True, "present")

    def test_an_absent_resource_skips_off_the_contract_machine(self, monkeypatch):
        self._hw(monkeypatch)
        with pytest.raises(pytest.skip.Exception):
            require(False, "no msr")

    def test_an_absent_resource_fails_loud_on_the_contract_machine(self, monkeypatch):
        self._hw(monkeypatch, contracts=True)
        with pytest.raises(pytest.fail.Exception, match="no msr"):
            require(False, "no msr")

    def test_a_privileged_resource_skips_until_the_run_holds_the_rights(self, monkeypatch):
        self._hw(monkeypatch, contracts=True)
        with pytest.raises(pytest.skip.Exception, match="privileged tier"):
            require_privileged(False, "needs root")

    def test_a_privileged_resource_fails_loud_under_its_own_flag(self, monkeypatch):
        self._hw(monkeypatch, contracts=True, privileged=True)
        with pytest.raises(pytest.fail.Exception, match="needs root"):
            require_privileged(False, "needs root")


def test_live_scenario_requires_an_isolated_home(tmp_path, monkeypatch):
    script = Path(__file__).parent.parent / "scripts" / "live_scenarios.py"
    install_home = runpy.run_path(script)["_install_campaign_home"]
    real_home = tmp_path / "real-home"
    campaign_home = tmp_path / "campaign-home"
    monkeypatch.setenv("HOME", str(real_home))

    with pytest.raises(ValueError, match="real home"):
        install_home(real_home, real_home=real_home)

    install_home(campaign_home, real_home=real_home)

    assert Path(os.environ["HOME"]) == campaign_home
    assert Path(os.environ["XDG_CONFIG_HOME"]) == campaign_home / ".config"
    assert Path(os.environ["XDG_CACHE_HOME"]) == campaign_home / ".cache"
    assert Path(os.environ["XDG_DATA_HOME"]) == campaign_home / ".local" / "share"
    assert Path(os.environ["XDG_STATE_HOME"]) == campaign_home / ".local" / "state"
    assert Path(os.environ["XDG_RUNTIME_DIR"]) == campaign_home / "run"


def test_live_wrapper_aborts_before_building_when_any_backend_conflicts(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nix_called = tmp_path / "nix-called"
    pkill_called = tmp_path / "pkill-called"
    (fake_bin / "pgrep").write_text('#!/bin/sh\ncase "$*" in *stressapptest*) exit 0;; *) exit 1;; esac\n')
    (fake_bin / "nix").write_text(f"#!/bin/sh\ntouch {nix_called}\nexit 99\n")
    (fake_bin / "pkill").write_text(f"#!/bin/sh\ntouch {pkill_called}\nexit 99\n")
    (fake_bin / "pgrep").chmod(0o755)
    (fake_bin / "nix").chmod(0o755)
    (fake_bin / "pkill").chmod(0o755)
    result_path = tmp_path / "result.json"
    script = Path(__file__).parent.parent / "scripts" / "run_live_scenario.sh"
    bash = shutil.which("bash")
    assert bash is not None

    result = subprocess.run(
        [bash, str(script), "doctor"],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "RESULT": str(result_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert '"verdict":"ABORT"' in result_path.read_text()
    assert not nix_called.exists()
    assert not pkill_called.exists()


def test_mutants_are_always_built_from_pristine_source(tmp_path):
    script = Path(__file__).parent.parent / "scripts" / "mutate.py"
    build_mutant = runpy.run_path(script)["_build"]
    source = "def f(value):\n    return value == 1 and value > 0\n"
    target = tmp_path / "target.py"
    target.write_text(source)

    first = build_mutant(target, 0, source)
    assert first is not None
    target.write_text(first.source)
    second_after_mutation = build_mutant(target, 1, source)
    target.write_text(source)
    second_from_pristine = build_mutant(target, 1, source)

    assert second_after_mutation == second_from_pristine
