"""Tests for automatic N/A (``na_if_missing``) and ``--not-applicable`` on cube suites (CB-4)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("climateeval")

import ibis  # noqa: E402
from climateeval.data import DataSourceInformation  # noqa: E402
from climateeval.exceptions import MissingDataError  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climatebench2._thresholds import use_thresholds  # noqa: E402
from climatebench2.diags.pass_fail import GateCheck  # noqa: E402
from climatebench2.diags.tier1_physics import CB2ComplexDiagnostic  # noqa: E402

INFO = DataSourceInformation(name="Emulator", category="model")

NA_BLOCK = {
    "toy": {
        "na": {
            "requirement": "required",
            "tier": "I",
            "na_if_missing": ["evspsbl", "hfls"],
        },
        "plain": {"requirement": "required", "tier": "I"},
    },
}


class NaGate(CB2ComplexDiagnostic):
    """Needs `amip`; its block lists optional variables."""

    _required_data_keys = ("amip",)
    _threshold_block = "toy.na"
    _gate_checks = (
        GateCheck("toy_a", "toy_x", upper=2.0, requirement="required"),
        GateCheck("toy_b", "toy_x", upper=0.5, requirement="required"),
    )
    raises: Exception | None = None

    def _calculate_raw_output(self, complex_data_source):  # noqa: ANN001, ANN202
        if self.raises is not None:
            raise self.raises
        return self._scalar_outputs({"toy_x": 1.0})


class PlainGate(NaGate):
    """Same checks, block without `na_if_missing`."""

    _threshold_block = "toy.plain"


def _cubes(*names: str) -> CubeList:
    return CubeList([Cube(np.zeros(3), var_name=n) for n in names])


def _gate(cls=NaGate, **kwargs):  # noqa: ANN001, ANN003, ANN202
    return cls("toy_gate", fail_on_missing_data=False, **kwargs)


def test_missing_optional_variable_gives_na_rows_with_a_reason() -> None:
    with use_thresholds(NA_BLOCK):
        out = _gate().get_output({"amip": _cubes("pr", "hfls")}, INFO)
    assert out.raw_output is None
    rows = out.metrics.to_pandas()
    assert sorted(rows["var_id"]) == ["toy_a", "toy_b"]
    assert (rows["applicable"] == 0.0).all()
    assert rows["value"].isna().all()
    assert rows["passes"].isna().all()
    assert set(rows["na_reason"]) == {"missing output: evspsbl"}
    assert (rows["requirement"] == "required").all()


def test_several_missing_variables_are_all_named() -> None:
    with use_thresholds(NA_BLOCK):
        out = _gate().get_output({"amip": _cubes("pr")}, INFO)
    assert set(out.metrics.to_pandas()["na_reason"]) == {
        "missing output: evspsbl, hfls",
    }


def test_present_variables_evaluate_normally_with_an_empty_reason() -> None:
    with use_thresholds(NA_BLOCK):
        out = _gate().get_output(
            {"amip": _cubes("evspsbl", "hfls", "pr")},
            INFO,
        )
    rows = out.metrics.to_pandas().set_index("var_id")
    assert rows.loc["toy_a", "applicable"] == 1.0
    assert rows.loc["toy_a", "passes"] == 1.0
    assert rows.loc["toy_b", "passes"] == 0.0  # 1.0 > 0.5: a real fail
    assert set(rows["na_reason"]) == {""}


def test_missing_experiment_still_writes_no_row() -> None:
    """The required experiment key absent: 'not run', not N/A."""
    with use_thresholds(NA_BLOCK):
        out = _gate().get_output({"picontrol": _cubes("pr")}, INFO)
    assert out.metrics is None


def test_missing_data_error_naming_a_listed_variable_becomes_na() -> None:
    gate = _gate()
    gate.raises = MissingDataError("No data found for variable 'evspsbl' in x")
    with use_thresholds(NA_BLOCK):
        # an unknowable (stub) experiment is not pre-checked; the error converts
        out = gate.get_output({"amip": ["stub"]}, INFO)
    rows = out.metrics.to_pandas()
    assert (rows["applicable"] == 0.0).all()
    assert set(rows["na_reason"]) == {"missing output: evspsbl"}


def test_na_conversion_ignores_fail_on_missing_data() -> None:
    gate = NaGate("toy_gate", fail_on_missing_data=True)
    with use_thresholds(NA_BLOCK):
        out = gate.get_output({"amip": _cubes("pr", "hfls")}, INFO)
    assert (out.metrics.to_pandas()["applicable"] == 0.0).all()


def test_missing_data_error_for_an_unlisted_variable_keeps_old_handling() -> None:
    gate = _gate()
    gate.raises = MissingDataError("No data found for variable 'rsds'")
    with use_thresholds(NA_BLOCK):
        assert gate.get_output({"amip": ["stub"]}, INFO).metrics is None
    strict = NaGate("toy_gate", fail_on_missing_data=True)
    strict.raises = MissingDataError("No data found for variable 'rsds'")
    with use_thresholds(NA_BLOCK), pytest.raises(MissingDataError):
        strict.get_output({"amip": ["stub"]}, INFO)


def test_without_the_key_nothing_changes() -> None:
    """ClimateBench2's thresholds have no `na_if_missing`: no N/A, no new column."""
    gate = _gate(PlainGate)
    with use_thresholds(NA_BLOCK):
        # variable absent, but the block does not list it: computed as before
        out = gate.get_output({"amip": _cubes("pr")}, INFO)
    rows = out.metrics.to_pandas()
    assert "na_reason" not in rows.columns
    assert (rows["applicable"] == 1.0).all()

    class NoBlock(NaGate):
        _threshold_block = None

    assert "na_reason" not in (
        _gate(NoBlock).get_output({"amip": _cubes()}, INFO).metrics.to_pandas().columns
    )


def test_block_is_read_from_the_checks_when_the_class_names_none() -> None:
    class Blocked(NaGate):
        _threshold_block = None
        _gate_checks = (GateCheck("toy_c", "toy_x", block="toy.na"),)

    with use_thresholds(NA_BLOCK):
        out = _gate(Blocked).get_output({"amip": _cubes("pr", "hfls")}, INFO)
    assert set(out.metrics.to_pandas()["na_reason"]) == {"missing output: evspsbl"}


def test_declared_na_is_unchanged_and_has_no_reason() -> None:
    gate = _gate(not_applicable=["toy_gate"])
    with use_thresholds(NA_BLOCK):
        rows = gate.get_output({}, INFO).metrics.to_pandas()
    assert (rows["applicable"] == 0.0).all()
    assert "na_reason" not in rows.columns


def test_leaderboard_treats_automatic_and_declared_na_alike(tmp_path) -> None:  # noqa: ANN001
    from climatebench2.leaderboard import _gate_states, build_scores

    def write(name: str, gate) -> object:  # noqa: ANN001
        with use_thresholds(NA_BLOCK):
            metrics = gate.get_output({"amip": _cubes("pr", "hfls")}, INFO).metrics
        path = tmp_path / f"{name}.ddb"
        conn = ibis.connect(f"duckdb://{path}")
        conn.create_database("toy_gate")
        conn.create_table("metrics", metrics, database="toy_gate")
        conn.disconnect()
        return build_scores([path]).gates

    auto = write("auto", _gate())
    declared = write("declared", _gate(not_applicable=["toy_gate"]))
    assert not auto.empty
    assert _gate_states(auto) == _gate_states(declared) == {
        "model_Emulator": {"toy_a": "na", "toy_b": "na"},
    }


# ---------------------------------------------------------------------------
# --not-applicable for gate-bearing diagnostics of cube suites
# ---------------------------------------------------------------------------


def test_materialised_cube_suite_declares_only_the_named_gate(tmp_path) -> None:  # noqa: ANN001
    from climateeval.suites import Suite

    from climatebench2._cli import _resolve_suite, materialise_not_applicable_suite

    resolved = _resolve_suite("ClimateBench2_TierI_variability")
    path, matched = materialise_not_applicable_suite(
        resolved,
        tmp_path,
        ["enso_gate", "no_such_entry"],
    )
    assert matched == {"enso_gate"}
    diags = Suite(path)._get_diagnostics()
    assert diags["enso_gate"].declared_not_applicable
    assert not any(d.declared_not_applicable for n, d in diags.items() if n != "enso_gate")

    # Nothing matched: the suite is passed through untouched.
    same, none = materialise_not_applicable_suite(resolved, tmp_path, ["nope"])
    assert same == resolved
    assert none == set()


def test_cube_gate_declared_na_writes_na_rows_without_data(tmp_path) -> None:  # noqa: ANN001
    from climateeval.suites import Suite

    from climatebench2._cli import _resolve_suite, materialise_not_applicable_suite

    path, _ = materialise_not_applicable_suite(
        _resolve_suite("ClimateBench2_TierI_variability"),
        tmp_path,
        ["enso_gate"],
    )
    gate = Suite(path)._get_diagnostics()["enso_gate"]
    out = gate.get_output(CubeList(), INFO)  # no data needed
    rows = out.metrics.to_pandas()
    assert sorted(rows["var_id"]) == ["enso_amplitude", "enso_spectral_ratio"]
    assert (rows["applicable"] == 0.0).all()
    assert rows["passes"].isna().all()
    assert out.raw_output is None


def test_a_cube_gate_not_declared_na_still_accepts_no_unknown_kwargs() -> None:
    """GateMixin's new kwarg must not change how an undeclared gate is built."""
    from climateeval.suites import Suite

    from climatebench2._cli import _resolve_suite

    diags = Suite(_resolve_suite("ClimateBench2_TierI_variability"))._get_diagnostics()
    assert not diags["enso_gate"].declared_not_applicable


def test_score_cli_declares_a_cube_suite_gate_na(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    import climateeval._loader as loader
    from climateeval.suites import Suite

    from climatebench2._cli import main

    seen: dict[str, bool] = {}

    def fake_get_database(self, data, information, **_kw):  # noqa: ANN001, ANN202, ARG001
        for name, diag in self._get_diagnostics().items():
            seen[name] = getattr(diag, "declared_not_applicable", False)

    monkeypatch.setattr(loader, "load_cmor_dir", lambda *a, **k: ["cubes"])  # noqa: ARG005
    monkeypatch.setattr(Suite, "get_database", fake_get_database)
    model = tmp_path / "m"
    model.mkdir()
    main(
        [
            "score",
            str(model),
            "--name",
            "M",
            "--suite",
            "ClimateBench2_TierI_variability",
            "--not-applicable",
            "enso_gate",
            "--no-score",
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert seen["enso_gate"] is True
