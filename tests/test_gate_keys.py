"""CB-1: gates read their experiments through ``self.keys[role]``.

A subclass overriding ``keys`` alone must compute the same numbers from a
renamed data dict as its parent does from the original one.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
import pytest

pytest.importorskip("climateeval")

from climateeval.data import ComplexDataSource, DataSourceInformation  # noqa: E402
from climateeval.diags.complex import LandOceanWarmingRatio  # noqa: E402

from climatebench2 import diags  # noqa: E402
from climatebench2.diags.tier1_physics import (  # noqa: E402
    ClearSkyFeedbackGate,
    ClosureGate,
    LandOceanWarmingGate,
)
from tests.test_tier1_physics import _clear_sky_experiments, _info, _picontrol_cubes  # noqa: E402


def test_default_keys_are_the_cb2_experiments_and_derive_required_keys() -> None:
    assert ClosureGate.keys == {"control": "picontrol"}
    assert ClosureGate._required_data_keys == ("picontrol",)
    assert ClearSkyFeedbackGate._required_data_keys == ("picontrol", "4xco2")
    assert diags.AerosolForcingGate.keys == {
        "control": "picontrol",
        "abrupt": "4xco2",
        "aerosol": "histaer",
    }
    assert diags.ECSGate._required_data_keys == ("4xco2", "picontrol")
    assert diags.MidHoloceneMonsoonGate._required_data_keys == ("midholocene", "picontrol")
    assert diags.PaleoProxyScore._required_data_keys == ("picontrol",)
    assert diags.PaleoProxyScore.optional_keys == {"period": "lgm"}


def test_every_complex_gate_declares_keys() -> None:
    for name in diags.__all__:
        cls = getattr(diags, name)
        if getattr(cls, "_required_data_keys", ()) and hasattr(cls, "_gate_checks"):
            assert cls.keys, name
            assert tuple(dict.fromkeys(cls.keys.values())) == cls._required_data_keys, name


def test_subclass_overriding_keys_updates_required_keys() -> None:
    class AmipClosure(ClosureGate):
        keys: ClassVar[dict[str, str]] = {"control": "amip"}

    assert AmipClosure._required_data_keys == ("amip",)
    assert ClosureGate._required_data_keys == ("picontrol",)  # parent untouched


def test_subclass_dropping_a_role_is_rejected() -> None:
    with pytest.raises(TypeError, match="must keep the roles"):

        class Broken(ClearSkyFeedbackGate):
            keys: ClassVar[dict[str, str]] = {"control": "x"}


def _metrics(diag, data):  # noqa: ANN001, ANN202
    return diag.get_output(data, _info()).metrics.to_pandas().set_index("var_id")


def test_native_gate_with_remapped_keys_gives_identical_numbers() -> None:
    class AmipClosure(ClosureGate):
        keys: ClassVar[dict[str, str]] = {"control": "amip"}

    cubes = _picontrol_cubes()
    parent = _metrics(ClosureGate("closure", fail_on_missing_data=True), {"picontrol": cubes})
    child = _metrics(AmipClosure("closure", fail_on_missing_data=True), {"amip": cubes})
    assert not parent.empty
    pd.testing.assert_frame_equal(parent, child)


def test_native_gate_with_two_roles_remapped() -> None:
    class AmipClearSky(ClearSkyFeedbackGate):
        keys: ClassVar[dict[str, str]] = {"control": "amip", "abrupt": "amip_p4k"}

    data = _clear_sky_experiments(slope=1.9)
    renamed = {"amip": data["picontrol"], "amip_p4k": data["4xco2"]}
    parent = _metrics(ClearSkyFeedbackGate("c", fail_on_missing_data=True), data)
    child = _metrics(AmipClearSky("c", fail_on_missing_data=True), renamed)
    assert parent.loc["clear_sky_lw_feedback", "value"] == child.loc["clear_sky_lw_feedback", "value"]


def test_missing_remapped_key_skips_like_a_missing_experiment() -> None:
    class AmipClosure(ClosureGate):
        keys: ClassVar[dict[str, str]] = {"control": "amip"}

    diag = AmipClosure("closure", fail_on_missing_data=False)
    output = diag.get_output({"picontrol": _picontrol_cubes()}, _info())
    assert output.metrics is None  # the parent's key is not this gate's key


def test_upstream_wrapper_hands_upstream_a_view_under_its_own_keys(monkeypatch) -> None:  # noqa: ANN001
    seen: list[dict] = []

    def fake_upstream(self, source):  # noqa: ANN001, ANN202
        seen.append(source.data)
        return {}

    monkeypatch.setattr(LandOceanWarmingRatio, "_calculate_raw_output", fake_upstream)

    class AmipLandOceanGate(LandOceanWarmingGate):
        keys: ClassVar[dict[str, str]] = {"control": "amip", "abrupt": "amip_p4k"}

    assert AmipLandOceanGate._required_data_keys == ("amip", "amip_p4k")
    control, abrupt = object(), object()
    info = DataSourceInformation(name="m", category="model", institute="i", exp="e", variant="v")
    diag = AmipLandOceanGate("land_ocean_warming")
    diag._calculate_raw_output(
        ComplexDataSource(data={"amip": control, "amip_p4k": abrupt, "extra": 1}, information=info),
    )
    assert seen[-1] == {"picontrol": control, "4xco2": abrupt}

    # upstream's own comparison-ensemble sources already use upstream's keys
    own = {"picontrol": control, "4xco2": abrupt}
    diag._calculate_raw_output(ComplexDataSource(data=own, information=info))
    assert seen[-1] is own

    # the parent is unchanged: a plain pass-through
    parent_data = {"picontrol": control, "4xco2": abrupt, "historical": 1}
    LandOceanWarmingGate("land_ocean_warming")._calculate_raw_output(
        ComplexDataSource(data=parent_data, information=info),
    )
    assert seen[-1] is parent_data
