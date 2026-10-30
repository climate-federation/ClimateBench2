"""A frozen comparison ensemble scores a submission exactly like a live one.

``climatebench2.frozen_comparison`` copies the CMIP6 comparison rows out of
finished databases (``freeze``) and merges them into a run that never loaded
the ensemble (``merge``), before the scoring pass forms ``E_ref`` from them.
The tests prove, from the cheapest to the most complete:

- the suite side — only the ensemble *generators* leave ``other_data``;
- the database side — freeze → strip → merge → rescore reproduces every
  score row of the live database, including a full wide table rebuilt from
  split single-variable databases;
- the refusals — a second merge, a variable row produced with a different
  window, a scored pair the bundle lacks, a bundle from another protocol;
- end to end through ``climatebench2 score`` on synthetic cubes with real
  ClimateEval diagnostics: a live full-suite run, live split-suite runs
  frozen into a bundle, and a ``--comparison frozen`` run of the submission
  alone — whose skill and ``E_ref`` rows equal the live run's, without the
  comparison generator ever being called.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

duckdb = pytest.importorskip("duckdb")
climateeval = pytest.importorskip("climateeval")

import yaml  # noqa: E402
from cf_units import Unit  # noqa: E402
from iris.coords import DimCoord  # noqa: E402
from iris.cube import Cube, CubeList  # noqa: E402

from climateeval.data import DataSourceInformation  # noqa: E402

from climatebench2 import frozen_comparison as fc  # noqa: E402
from climatebench2.scoring_pass import SCORER, score_databases  # noqa: E402

#: Fast bootstrap settings — the protocol values live in thresholds.yml.
FAST = {
    "block_length_monthly": 12,
    "block_length_annual": 3,
    "n_boot": 40,
    "alpha": 0.05,
    "resample_members": True,
    "seed": 1,
}

#: The score columns a frozen run must reproduce.
SCORE_COLUMNS = [
    "data_id",
    "data_type",
    "var_id",
    "crps",
    "crps_se",
    "crps_ci_lo",
    "crps_ci_hi",
    "e_ref",
    "n_ref_models",
    "skill",
    "n_members",
    "reason",
    "window",
]
_SELECT_SCORES = ", ".join(f'"{c}"' for c in SCORE_COLUMNS)  # `window` is SQL


# ---------------------------------------------------------------------------
# Suite side
# ---------------------------------------------------------------------------


class FakeEnsemble:
    """An ``other_data`` generator, like ``StagedCMIP6HistoricalSSP245``.

    Counts its calls: a frozen run must never ask it for a member.
    """

    calls: ClassVar[Counter[str]] = Counter()

    def generate(self, variable):  # noqa: ANN001, ANN201
        FakeEnsemble.calls[variable.id] += 1
        for model, n_members, spread in COMPARISON:
            for member in range(1, n_members + 1):
                yield _ComparisonMember(model, member, spread)


def test_strip_drops_only_generators_and_keeps_the_reference() -> None:
    comparison = [f"{__name__}.FakeEnsemble", "climateeval.data.HadISST"]
    definition = [
        {
            "name": "sst",
            "diagnostic": "climatebench2.diags.ScoredAnnualMeanTimeSeries",
            "variables": [
                {
                    "id": "tos",
                    "var_name": "tos",
                    "frequency": "mon",
                    "reference_data": "climateeval.data.ESACCISST",
                    "other_data": comparison,
                },
            ],
        },
        {
            "name": "map",
            "diagnostic": "climateeval.diags.simple.Map",
            "variables": [
                {
                    "id": "tas",
                    "var_name": "tas",
                    "frequency": "mon",
                    "other_data": comparison[:1],  # a YAML anchor shares the list
                },
            ],
        },
        {"name": "ecs_gate", "diagnostic": "climatebench2.diags.ECSGate"},
    ]
    stripped_definition, stripped = fc.strip_comparison_ensemble(definition)

    assert stripped == {"sst": ["tos"], "map": ["tas"]}
    sst, tas_map, gate = stripped_definition
    assert sst["variables"][0]["other_data"] == ["climateeval.data.HadISST"]
    assert sst["variables"][0]["reference_data"] == "climateeval.data.ESACCISST"
    assert "other_data" not in tas_map["variables"][0]
    assert gate == definition[2]
    # The input — and the list it shares between entries — is untouched.
    assert definition[0]["variables"][0]["other_data"] == comparison
    assert len(comparison) == 2


# ---------------------------------------------------------------------------
# Database side, on hand-built databases (no ClimateEval run)
# ---------------------------------------------------------------------------

YEARS = list(range(1985, 2035))
#: (model, members, spread around the truth) of the comparison ensemble.
COMPARISON = [("ModelA", 2, 0.15), ("ModelB", 2, 0.35), ("ModelC", 3, 0.25)]
SUBMISSION_MEMBERS = 3
WIDE = "annual_mean_timeseries"
EOF = "eof_projection"
VARIABLES = ("tas", "pr")


def _id(category: str, name: str, variant: str = "") -> str:
    """Mirror ``DataSourceInformation.id``."""
    exp = "" if category == "observation" else "historical"
    return "_".join(p for p in (category, name, exp, variant) if p)


def _sources() -> list[tuple[str, str, str, str, float]]:
    """``(data_id, data_type, name, category, spread)`` of every source."""
    out = [(_id("observation", "OBS"), "reference", "OBS", "observation", 0.0)]
    out += [
        (_id("model", "MyModel", f"r{m}i1p1f1"), "to_benchmark", "MyModel", "model", 0.2)
        for m in range(1, SUBMISSION_MEMBERS + 1)
    ]
    out += [
        (_id("CMIP6", model, f"r{m}i1p1f1"), "other", model, "CMIP6", spread)
        for model, n, spread in COMPARISON
        for m in range(1, n + 1)
    ]
    return out


def _live_tables(variables: tuple[str, ...] = VARIABLES) -> dict[str, dict[str, pd.DataFrame]]:
    """``schema -> table -> frame`` as ``Suite.get_database`` writes them.

    The wide time series holds one row per (source, year) with a column per
    variable, the EOF table one row per (source, variable, mode). Comparison
    rows are written once per submission member, as a per-member run does.
    """
    rng = np.random.default_rng(3)
    years = pd.to_datetime([f"{y}-07-01" for y in YEARS])
    truth = {v: 0.02 * (np.array(YEARS) - 1985.0) + rng.normal(0, 0.1, len(YEARS)) for v in VARIABLES}
    wide, eof, metrics, sources = [], [], [], []
    for data_id, data_type, name, category, spread in _sources():
        frame = pd.DataFrame({"data_id": data_id, "data_type": data_type, "time": years})
        for v in variables:
            frame[v] = (truth[v] + rng.normal(0, spread, len(YEARS))).astype(np.float32)
        for v in variables:
            eof.append(
                pd.DataFrame(
                    {
                        "data_id": data_id,
                        "data_type": data_type,
                        "var_id": v,
                        "mode": np.arange(1.0, 6.0),
                        "coefficient": rng.normal(0, 1 + spread, 5),
                    },
                ),
            )
            if data_type != "reference":
                metrics.append(
                    {
                        "data_id": data_id,
                        "reference_data_id": _id("observation", "OBS"),
                        "var_id": v,
                        "weighted_rmse": float(spread),
                    },
                )
        wide.append(frame)
        sources.append(
            {
                "id": data_id,
                "name": name,
                "category": category,
                "institute": "",
                "exp": "" if category == "observation" else "historical",
                "variant": data_id.rsplit("_", 1)[-1] if data_type != "reference" else "",
                "references": "",
            },
        )
    wide_df = pd.concat(wide, ignore_index=True)
    eof_df = pd.concat(eof, ignore_index=True)
    others = wide_df["data_type"] == "other"
    # A per-member run appends the comparison rows once per member.
    wide_df = pd.concat([wide_df, *[wide_df[others]] * (SUBMISSION_MEMBERS - 1)], ignore_index=True)
    variables_df = pd.DataFrame(
        {
            "id": list(variables),
            "var_name": list(variables),
            "frequency": "mon",
            "timerange": "19850101/20341231",
            "units": "",
        },
    )
    tables: dict[str, dict[str, pd.DataFrame]] = {}
    for schema, raw in ((WIDE, wide_df), (EOF, eof_df)):
        tables[schema] = {
            "raw_output": raw,
            "metrics": pd.DataFrame(metrics),
            "data_sources": pd.DataFrame(sources),
            "variables": variables_df,
        }
    return tables


def _write(path: Path, tables: dict[str, dict[str, pd.DataFrame]]) -> Path:
    con = duckdb.connect(str(path))
    for schema, frames in tables.items():
        con.execute(f'CREATE SCHEMA "{schema}"')
        for table, frame in frames.items():
            con.register("frame", frame)
            con.execute(f'CREATE TABLE "{schema}"."{table}" AS SELECT * FROM frame')
            con.unregister("frame")
    con.close()
    return path


def _split(tables: dict[str, dict[str, pd.DataFrame]], var: str) -> dict[str, dict[str, pd.DataFrame]]:
    """The tables a split suite of one variable would have written."""
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for schema, frames in tables.items():
        raw = frames["raw_output"]
        if "var_id" in raw.columns:
            raw = raw[raw["var_id"] == var]
        else:
            drop = [v for v in VARIABLES if v != var]
            raw = raw.drop(columns=drop)
        out[schema] = {
            "raw_output": raw.reset_index(drop=True),
            "metrics": frames["metrics"][frames["metrics"]["var_id"] == var],
            "data_sources": frames["data_sources"],
            "variables": frames["variables"][frames["variables"]["id"] == var],
        }
    return out


def _scores(db: Path) -> pd.DataFrame:
    con = duckdb.connect(str(db), read_only=True)
    try:
        frames = []
        for (schema,) in con.execute(
            "SELECT DISTINCT table_schema FROM information_schema.tables "
            "WHERE table_name = 'metrics' ORDER BY 1",
        ).fetchall():
            columns = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = ? AND table_name = 'metrics'",
                    [schema],
                ).fetchall()
            }
            if "scorer" not in columns:
                continue
            frame = con.execute(
                f'SELECT {_SELECT_SCORES} FROM "{schema}".metrics '  # noqa: S608
                f"WHERE scorer = ?",
                [SCORER],
            ).df()
            frames.append(frame.assign(schema=schema))
    finally:
        con.close()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["schema", "var_id", "data_type", "data_id"]).reset_index(drop=True)


def _comparison_rows(db: Path, schema: str) -> int:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return int(
            con.execute(
                f"SELECT count(*) FROM \"{schema}\".raw_output WHERE data_type = 'other'",  # noqa: S608
            ).fetchone()[0],
        )
    finally:
        con.close()


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    (tmp_path / "live").mkdir()
    path = _write(tmp_path / "live" / "ClimateBench2_TierII.ddb", _live_tables())
    score_databases([path], settings=FAST)
    return path


def _copy_stripped(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    shutil.copy(source, target)
    assert fc.strip_comparison_rows(target) > 0
    return target


def test_freeze_strip_merge_reproduces_every_score_row(live_db: Path, tmp_path: Path) -> None:
    manifest = fc.freeze([live_db], tmp_path / "bundle", nominal_timerange="20150101/20341231")
    entries = manifest["databases"]["ClimateBench2_TierII"]["entries"]
    assert set(entries) == {WIDE, EOF}
    tas = entries[WIDE]["tas"]
    assert tas["layout"] == "wide"
    assert tas["members_per_model"] == {"ModelA": 2, "ModelB": 2, "ModelC": 3}
    assert tas["raw_rows"] == 7 * len(YEARS)  # deduplicated: once, not per member
    assert tas["timerange"] == ["19850101/20341231"]
    assert entries[EOF]["pr"]["layout"] == "long"

    run = _copy_stripped(live_db, tmp_path / "run")
    assert _comparison_rows(run, WIDE) == 0
    report = fc.merge(run, tmp_path / "bundle", stripped={WIDE: ["tas", "pr"], EOF: ["tas", "pr"]})
    assert sorted(report.merged) == [(WIDE, "pr"), (WIDE, "tas"), (EOF, "pr"), (EOF, "tas")]
    assert not report.missing
    # Re-assembled into the live layout: one row per (source, year), both
    # variables filled.
    assert _comparison_rows(run, WIDE) == 7 * len(YEARS)

    score_databases([run], settings=FAST)
    live, frozen = _scores(live_db), _scores(run)
    assert np.isfinite(live["skill"]).sum() >= 8  # really scored
    pd.testing.assert_frame_equal(live, frozen)


def test_a_full_table_is_rebuilt_from_split_single_variable_databases(
    live_db: Path,
    tmp_path: Path,
) -> None:
    tables = _live_tables()
    splits = []
    for var in VARIABLES:
        (tmp_path / var).mkdir()
        splits.append(_write(tmp_path / var / "ClimateBench2_TierII.ddb", _split(tables, var)))
    fc.freeze(splits, tmp_path / "bundle")

    run = _copy_stripped(live_db, tmp_path / "run")
    fc.merge(run, tmp_path / "bundle", stripped={WIDE: list(VARIABLES), EOF: list(VARIABLES)})
    score_databases([run], settings=FAST)
    pd.testing.assert_frame_equal(_scores(live_db), _scores(run))

    # And the other way round: a split run takes only its own variable.
    split_run = _copy_stripped(splits[1], tmp_path / "split_run")
    report = fc.merge(split_run, tmp_path / "bundle", stripped={WIDE: ["pr"]})
    assert sorted(report.merged) == [(WIDE, "pr"), (EOF, "pr")]
    con = duckdb.connect(str(split_run), read_only=True)
    columns = {r[0] for r in con.execute("DESCRIBE annual_mean_timeseries.raw_output").fetchall()}
    con.close()
    assert "tas" not in columns


def test_merge_refuses_a_second_copy_unless_replacing(live_db: Path, tmp_path: Path) -> None:
    fc.freeze([live_db], tmp_path / "bundle")
    run = _copy_stripped(live_db, tmp_path / "run")
    fc.merge(run, tmp_path / "bundle")
    with pytest.raises(fc.FrozenComparisonError, match="already holds comparison rows"):
        fc.merge(run, tmp_path / "bundle")
    fc.merge(run, tmp_path / "bundle", replace=True)
    assert _comparison_rows(run, WIDE) == 7 * len(YEARS)


def test_a_variable_produced_over_another_window_is_refused(live_db: Path, tmp_path: Path) -> None:
    fc.freeze([live_db], tmp_path / "bundle")
    run = _copy_stripped(live_db, tmp_path / "run")
    con = duckdb.connect(str(run))
    con.execute(
        "UPDATE annual_mean_timeseries.variables SET timerange = '19850101/20331231' "
        "WHERE id = 'pr'",
    )
    con.close()
    with pytest.raises(fc.FrozenComparisonError, match="timerange run='19850101/20331231'"):
        fc.merge(run, tmp_path / "bundle")
    assert _comparison_rows(run, EOF) == 0  # all or nothing: nothing written


def test_a_scored_pair_without_frozen_rows_is_an_error_unless_allowed(
    live_db: Path,
    tmp_path: Path,
) -> None:
    tables = _live_tables()
    (tmp_path / "tas").mkdir()
    fc.freeze([_write(tmp_path / "tas" / "ClimateBench2_TierII.ddb", _split(tables, "tas"))], tmp_path / "bundle")
    run = _copy_stripped(live_db, tmp_path / "run")
    stripped = {WIDE: ["tas", "pr"], EOF: ["tas", "pr"]}
    with pytest.raises(fc.FrozenComparisonError, match="annual_mean_timeseries/pr"):
        fc.merge(run, tmp_path / "bundle", stripped=stripped)
    report = fc.merge(run, tmp_path / "bundle", stripped=stripped, allow_missing=True)
    assert report.missing == [(WIDE, "pr"), (EOF, "pr")]
    score_databases([run], settings=FAST)
    pr_rows = _scores(run).query("var_id == 'pr' and data_type == 'to_benchmark'")
    assert pr_rows["e_ref"].isna().all()  # no E_ref, but not a wrong one


def test_a_bundle_from_another_protocol_is_refused(live_db: Path, tmp_path: Path) -> None:
    fc.freeze([live_db], tmp_path / "bundle", nominal_timerange="20150101/20341231")
    manifest = fc.load_manifest(tmp_path / "bundle")
    assert fc.check_provenance(manifest, timerange="20150101/20341231") == []
    with pytest.raises(fc.FrozenComparisonError, match="Tier II window 20150101/20341231"):
        fc.check_provenance(manifest, timerange="20150101/20251231")

    tampered = json.loads(json.dumps(manifest))
    tampered["protocol_thresholds"]["tier2.eof"] = {"max_modes": 99}
    tampered["protocol_fingerprint"] = "0" * 64
    with pytest.raises(fc.FrozenComparisonError, match=r"changed: tier2\.eof"):
        fc.check_provenance(tampered, timerange=None)

    tampered = json.loads(json.dumps(manifest))
    tampered["software"]["climatebench2_version"] = "0.0.1"
    with pytest.raises(fc.FrozenComparisonError, match="ClimateBench2 0.0.1"):
        fc.check_provenance(tampered, timerange=None)


def _group_db(directory: Path, var: str, *, seed_shift: float = 0.0) -> Path:
    """A split-suite database of one variable group (named ``var``)."""
    tables = _split(_live_tables(), "tas")
    for frames in tables.values():
        raw = frames["raw_output"]
        if "var_id" in raw.columns:
            raw["var_id"] = var
        else:
            raw.rename(columns={"tas": var}, inplace=True)
            raw[var] = (raw[var] + np.float32(seed_shift)).astype(np.float32)
        frames["metrics"]["var_id"] = var
        frames["variables"]["id"] = var
        frames["variables"]["var_name"] = var
    directory.mkdir(parents=True, exist_ok=True)
    return _write(directory / "ClimateBench2_TierII.ddb", tables)


def test_ts_is_left_out_by_default_and_can_be_kept(tmp_path: Path) -> None:
    ts = _group_db(tmp_path / "ts", "ts")
    manifest = fc.freeze([ts, _group_db(tmp_path / "pr", "pr")], tmp_path / "bundle")
    assert manifest["excluded_variables"] == ["ts"]  # thresholds.yml default
    entries = manifest["databases"]["ClimateBench2_TierII"]["entries"]
    assert {v for e in entries.values() for v in e} == {"pr"}

    kept = fc.freeze([ts], tmp_path / "with_ts", include=["ts"])
    assert kept["excluded_variables"] == []
    assert set(kept["databases"]["ClimateBench2_TierII"]["entries"][WIDE]) == {"ts"}

    # A frozen run that still scores ts gets a note, not an error.
    run = _copy_stripped(ts, tmp_path / "run")
    report = fc.merge(run, tmp_path / "bundle", stripped={WIDE: ["ts"], EOF: ["ts"]})
    assert not report.missing
    assert any("leaves ts out on purpose" in m for m in report.messages)


def test_a_bundle_is_extended_one_variable_group_at_a_time(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    fc.freeze([_group_db(tmp_path / "tas", "tas")], bundle, nominal_timerange="20150101/20341231")
    ohc = _group_db(tmp_path / "ohc", "ohc")

    with pytest.raises(fc.FrozenComparisonError, match="not empty"):
        fc.freeze([ohc], bundle)
    manifest = fc.freeze([ohc], bundle, extend=True, notes={"ohc": "staged later"})
    entries = manifest["databases"]["ClimateBench2_TierII"]["entries"]
    assert set(entries[WIDE]) == {"tas", "ohc"}
    assert entries[WIDE]["tas"]["source"] == 0
    assert entries[WIDE]["ohc"]["source"] == 1
    assert [h["action"] for h in manifest["history"]] == ["freeze", "extend"]
    assert manifest["nominal_timerange"] == "20150101/20341231"
    assert manifest["notes"] == {"ohc": "staged later"}

    # Re-freezing a group: refused, unless replacing; the rest is untouched.
    rerun = _group_db(tmp_path / "ohc_rerun", "ohc", seed_shift=1.0)
    with pytest.raises(fc.FrozenComparisonError, match="already in the bundle"):
        fc.freeze([rerun], bundle, extend=True)
    assert not list(bundle.glob("*.partial"))  # a failed extend leaves no debris
    manifest = fc.freeze([rerun], bundle, extend=True, replace=True)
    entries = manifest["databases"]["ClimateBench2_TierII"]["entries"]
    assert entries[WIDE]["ohc"]["source"] == 2
    assert entries[WIDE]["ohc"]["raw_rows"] == entries[WIDE]["tas"]["raw_rows"]
    assert manifest["history"][-1]["replaced"] == [f"{WIDE}/ohc", f"{EOF}/ohc"]
    con = duckdb.connect(str(bundle / "ClimateBench2_TierII.ddb"), read_only=True)
    ohc_rows = con.execute(
        f"SELECT count(*), avg(ohc) FROM {WIDE}.raw_output WHERE cb2_variable = 'ohc'",
    ).fetchone()
    tas_rows = con.execute(
        f"SELECT count(*), avg(tas) FROM {WIDE}.raw_output WHERE cb2_variable = 'tas'",
    ).fetchone()
    con.close()
    assert ohc_rows[0] == tas_rows[0]  # the old ohc rows are gone, not doubled
    assert ohc_rows[1] == pytest.approx(tas_rows[1] + 1.0, abs=1e-4)

    # An extension must be made under the bundle's protocol and window.
    with pytest.raises(fc.FrozenComparisonError, match="Tier II window"):
        fc.freeze([_group_db(tmp_path / "pr", "pr")], bundle, extend=True, nominal_timerange="20150101/20251231")


# ---------------------------------------------------------------------------
# End to end: `climatebench2 score` on synthetic cubes
# ---------------------------------------------------------------------------

N_LAT, N_LON = 9, 18
WINDOW = "20150101/20241231"
UNITS = {"tas": "K", "pr": "kg m-2 s-1"}


def _cube(var: str, first: int, last: int, *, seed: int, spread: float) -> Cube:
    """A monthly (time, lat, lon) cube: a warming trend, a pattern, noise."""
    rng = np.random.default_rng(seed)
    base = np.random.default_rng(7 if var == "tas" else 8)
    n_time = (last - first + 1) * 12
    time = DimCoord(
        np.arange(n_time, dtype=float) * 30.0 + 15.0,
        standard_name="time",
        units=Unit(f"days since {first}-01-01", calendar="360_day"),
    )
    lat = DimCoord(np.linspace(-80.0, 80.0, N_LAT), standard_name="latitude", units="degrees")
    lon = DimCoord(
        np.linspace(10.0, 350.0, N_LON),
        standard_name="longitude",
        units="degrees",
        circular=True,
    )
    for coord in (time, lat, lon):
        coord.guess_bounds()
    years = first + np.arange(n_time) / 12.0
    # Three fixed spatial modes with source-own amplitudes (an EOF basis to
    # project on), a source-own warming pattern that grows after 2015, noise.
    modes = base.normal(0.0, 1.0, (3, N_LAT, N_LON))
    amplitudes = rng.normal(0.0, 1.0, (n_time, 3)) * np.array([1.5, 1.0, 0.6])
    own_pattern = rng.normal(0.0, spread, (N_LAT, N_LON))
    trend = 0.03 * (years - 1985.0) + 2.0 * np.sin(2 * np.pi * np.arange(n_time) / 12.0)
    data = (
        trend[:, None, None]
        + np.einsum("tk,kij->tij", amplitudes, modes)
        + np.clip(years - 2015.0, 0.0, None)[:, None, None] * own_pattern[None]
        + rng.normal(0.0, spread, (n_time, N_LAT, N_LON))
    )
    if var == "tas":
        data = 280.0 + data
        names = {"standard_name": "air_temperature"}
    else:
        data = 3e-5 * (1.0 + 0.05 * data)
        names = {"standard_name": "precipitation_flux"}
    return Cube(
        data.astype(np.float32),
        var_name=var,
        units=UNITS[var],
        dim_coords_and_dims=[(time, 0), (lat, 1), (lon, 2)],
        **names,
    )


def _clipped(variable, *, seed: int, spread: float) -> Cube:  # noqa: ANN001
    first, last = 1980, 2025
    if variable.timerange != "*":
        start, end = variable.timerange.split("/")
        first, last = max(first, int(start[:4])), min(last, int(end[:4]))
    return _cube(variable.var_name, first, last, seed=seed, spread=spread)


class _Source:
    """A DataSource-shaped fake (ClimateEval only calls these members)."""

    _information: DataSourceInformation
    seed: int = 0
    spread: float = 0.0

    @property
    def id(self) -> str:
        return self.information.id

    @property
    def information(self) -> DataSourceInformation:
        return self._information

    def download(self, variable, data_root_dir):  # noqa: ANN001, ANN201, ARG002
        return []

    def get_cube(self, data_root_dir, variable, *, download_missing_data=True):  # noqa: ANN001, ANN201, ARG002
        return _clipped(variable, seed=self.seed, spread=self.spread)


class FakeObs(_Source):
    """The suite's ``reference_data``."""

    _information = DataSourceInformation(name="FAKEOBS", category="observation")
    seed = 1
    spread = 0.05


class _ComparisonMember(_Source):
    def __init__(self, model: str, member: int, spread: float) -> None:
        self._information = DataSourceInformation(
            name=model,
            category="CMIP6",
            exp="historical-ssp245",
            variant=f"r{member}i1p1f1",
        )
        self.seed = 100 + 10 * sum(map(ord, model)) + member
        self.spread = spread


def _variable(var: str) -> dict:
    return {
        "id": var,
        "var_name": var,
        "frequency": "mon",
        "reference_data": f"{__name__}.FakeObs",
        "other_data": [f"{__name__}.FakeEnsemble"],
    }


def _suite(variables: dict[str, tuple[str, ...]]) -> list[dict]:
    """Entries named as in the protocol suite (anomaly baseline, EOF basis)."""
    diagnostics = {
        "annual_mean_timeseries": "climatebench2.diags.ScoredAnnualMeanTimeSeries",
        "reference_baseline": "climatebench2.diags.ReferenceBaselineRecord",
        "eof_projection": "climatebench2.diags.ReferenceEOFProjection",
        "zonal_line": "climateeval.diags.simple.ZonalLine",
    }
    return [
        {
            "name": name,
            "diagnostic": diagnostics[name],
            "variables": [_variable(v) for v in variables[name]],
        }
        for name in diagnostics
        if variables.get(name)
    ]


FULL = {name: VARIABLES for name in ("annual_mean_timeseries", "reference_baseline", "eof_projection", "zonal_line")}
#: The paper's split: one variable per job; the pr job left out the
#: deterministic zonal line, so the bundle has no `zonal_line/pr`.
SPLITS = {
    "tas": {name: ("tas",) for name in FULL},
    "pr": {name: ("pr",) for name in FULL if name != "zonal_line"},
}


def _suite_file(directory: Path, variables: dict[str, tuple[str, ...]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    # The stem selects the Tier II registry entry: per member, Tier II window.
    path = directory / "ClimateBench2_TierII.yml"
    path.write_text(yaml.safe_dump(_suite(variables), sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def cli(tmp_path_factory):  # noqa: ANN001, ANN201
    """``climatebench2 <argv>`` with the submission's loader stubbed out."""
    import climateeval._loader as loader

    from climatebench2._cli import main
    from climatebench2.data import STAGED_ROOT_ENV

    root = tmp_path_factory.mktemp("frozen_e2e")
    members = []
    for m in (1, 2):
        path = root / "MyModel" / f"r{m}i1p1f1"
        path.mkdir(parents=True)
        members.append(path)
    (root / "data_root").mkdir()

    def fake_load(path, *, timerange=None, **_kwargs):  # noqa: ANN001, ANN202, ARG001
        seed = 50 + int(Path(path).name[1])
        return CubeList(_cube(v, 1980, 2025, seed=seed + i, spread=0.2) for i, v in enumerate(VARIABLES))

    def run(*argv: str) -> None:
        with pytest.MonkeyPatch.context() as patch:
            # `score --data-root` exports the staged-ensemble root; keep it
            # from leaking into later tests.
            patch.delenv(STAGED_ROOT_ENV, raising=False)
            patch.setattr(loader, "load_cmor_dir", fake_load)
            patch.setattr(
                "climatebench2.scoring_pass._bootstrap_settings",
                lambda: dict(FAST),
            )
            main(list(argv))

    def score(suite: Path, out: Path, *extra: str) -> Path:
        run(
            "score",
            str(members[0]),
            "--name",
            "MyModel",
            *[f"--member=r{m}i1p1f1={p}" for m, p in enumerate(members, start=1)],
            "--suite",
            str(suite),
            "--data-root",
            str(root / "data_root"),
            "--out",
            str(out),
            *extra,
        )
        return out / "ClimateBench2_TierII.ddb"

    return root, run, score


@pytest.fixture(scope="module")
def e2e(cli):  # noqa: ANN001, ANN201
    """Live full run, live split runs → bundle, frozen run of the submission."""
    root, run, score = cli
    FakeEnsemble.calls.clear()
    live = score(_suite_file(root / "suites" / "full", FULL), root / "live_full", "--timerange", WINDOW)
    splits = [
        score(_suite_file(root / "suites" / var, SPLITS[var]), root / f"live_{var}", "--timerange", WINDOW)
        for var in SPLITS
    ]
    assert FakeEnsemble.calls  # the live runs did use the ensemble
    bundle = root / "bundle"
    run("freeze-comparison", *map(str, splits), "-o", str(bundle), "--timerange", WINDOW, "--note", "source=test")

    FakeEnsemble.calls.clear()
    frozen = score(
        _suite_file(root / "suites" / "full_again", FULL),
        root / "frozen_full",
        "--comparison",
        "frozen",
        "--frozen-comparison",
        str(bundle),
    )
    frozen_calls = Counter(FakeEnsemble.calls)
    return root, live, bundle, frozen, frozen_calls


def test_frozen_run_reproduces_the_live_skill_and_e_ref(e2e) -> None:  # noqa: ANN001
    _root, live, _bundle, frozen, frozen_calls = e2e
    assert not frozen_calls  # the comparison generator was never asked
    live_scores, frozen_scores = _scores(live), _scores(frozen)
    assert set(live_scores["schema"]) == {"annual_mean_timeseries", "eof_projection"}
    submission = live_scores.query("data_type == 'to_benchmark'")
    skilled = submission[np.isfinite(submission["skill"])]
    assert set(zip(skilled["schema"], skilled["var_id"], strict=True)) == {
        (schema, var)
        for schema in ("annual_mean_timeseries", "eof_projection")
        for var in VARIABLES
    }
    assert (skilled["n_ref_models"] == 3).all()  # ModelA, ModelB, ModelC
    pd.testing.assert_frame_equal(live_scores, frozen_scores, check_exact=False, rtol=1e-12)


def test_frozen_run_has_the_comparison_rows_but_never_loaded_them(e2e) -> None:  # noqa: ANN001
    root, live, bundle, frozen, _calls = e2e
    for schema in ("annual_mean_timeseries", "eof_projection"):
        assert _comparison_rows(frozen, schema) == _comparison_rows(live, schema) // 2  # deduplicated
    # The deterministic zonal line keeps its tas comparison (from the tas
    # split) and goes without pr — a note, not an error: it is never scored.
    assert _comparison_rows(frozen, "zonal_line") > 0
    stripped_suite = yaml.safe_load(
        (root / "frozen_full" / "_comparison_stripped_suites" / "ClimateBench2_TierII.yml").read_text(),
    )
    assert all("other_data" not in v for e in stripped_suite for v in e["variables"])
    manifest = fc.load_manifest(bundle)
    assert manifest["nominal_timerange"] == WINDOW
    assert manifest["notes"] == {"source": "test"}
    assert len(manifest["sources"]) == 2
    # Sources are labelled by run directory and file, never by machine path.
    for source in manifest["sources"]:
        assert not source["path"].startswith("/")
        assert source["path"].count("/") == 1
        assert str(root) not in source["path"]
    assert str(root) not in (bundle / fc.MANIFEST).read_text()


def test_source_label_is_the_run_directory_and_file() -> None:
    assert (
        fc.source_label(Path("/scratch/x/runs/tas/ClimateBench2_TierII.ddb"))
        == "tas/ClimateBench2_TierII.ddb"
    )


def test_frozen_run_fails_fast_on_a_provenance_mismatch(cli, e2e) -> None:  # noqa: ANN001
    root, _run, score = cli
    bundle = e2e[2]
    suite = root / "suites" / "full" / "ClimateBench2_TierII.yml"

    with pytest.raises(SystemExit, match="Tier II window"):
        score(suite, root / "bad_window", "--comparison", "frozen", "--frozen-comparison", str(bundle), "--timerange", "20150101/20231231")
    assert not list((root / "bad_window").glob("*.ddb"))  # before any suite ran

    tampered = root / "tampered"
    shutil.copytree(bundle, tampered)
    manifest = json.loads((tampered / fc.MANIFEST).read_text())
    manifest["databases"]["ClimateBench2_TierII"]["entries"]["annual_mean_timeseries"]["tas"]["timerange"] = ["19850101/20221231"]
    (tampered / fc.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="annual_mean_timeseries/tas: run 19850101/20241231"):
        score(suite, root / "bad_entry", "--comparison", "frozen", "--frozen-comparison", str(tampered))
    assert not list((root / "bad_entry").glob("*.ddb"))

    with pytest.raises(SystemExit, match="needs --frozen-comparison"):
        score(suite, root / "no_bundle", "--comparison", "frozen")


def test_comparison_none_scores_without_e_ref(cli) -> None:  # noqa: ANN001
    root, _run, score = cli
    FakeEnsemble.calls.clear()
    db = score(_suite_file(root / "suites" / "none", SPLITS["pr"]), root / "none", "--timerange", WINDOW, "--comparison", "none")
    assert not FakeEnsemble.calls
    assert _comparison_rows(db, "annual_mean_timeseries") == 0
    scores = _scores(db)
    submission = scores.query("data_type == 'to_benchmark' and var_id == 'pr'")
    assert np.isfinite(submission["crps"]).all()
    assert scores["e_ref"].isna().all()
