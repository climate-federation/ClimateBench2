"""The cloud runner's object-storage layer (``deploy/cb2_run.py``).

No ``s5cmd`` binary and no network here: ``subprocess.run`` and the anonymous
GCS JSON-API helpers are monkeypatched, and the tests check the commands and
environments the runner would hand them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "cb2_run.py"

HMAC = {"GCS_HMAC_ACCESS_ID": "GOOG1EXAMPLE", "GCS_HMAC_SECRET": "not-a-real-secret"}


@pytest.fixture
def cb2_run(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """``deploy/cb2_run.py`` imported by path, with no HMAC key set."""
    for name in HMAC:
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("cb2_run_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def calls(cb2_run, monkeypatch: pytest.MonkeyPatch) -> list[dict]:  # noqa: ANN001
    """Record every ``subprocess.run`` instead of running it."""
    recorded: list[dict] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
        recorded.append({"cmd": list(cmd), "env": kwargs.get("env"), "check": kwargs.get("check")})

    monkeypatch.setattr(cb2_run.subprocess, "run", fake_run)
    return recorded


@pytest.fixture
def anonymous(cb2_run, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:  # noqa: ANN001
    """Replace the anonymous JSON-API listing/download; record the downloads."""
    got: list[tuple] = []

    def fake_list(bucket, prefix):  # noqa: ANN001, ANN202
        return [
            {"name": f"{prefix}a/x.nc", "size": "3"},
            {"name": f"{prefix}b/", "size": "0"},  # a "directory" placeholder
        ]

    def fake_get(bucket, name, dest, size):  # noqa: ANN001, ANN202
        got.append((bucket, name, dest, size))

    monkeypatch.setattr(cb2_run, "_gcs_list", fake_list)
    monkeypatch.setattr(cb2_run, "_gcs_get", fake_get)
    return got


# -- the shared HMAC handling ------------------------------------------------


def test_hmac_env_maps_the_key_to_s3_credentials(cb2_run) -> None:  # noqa: ANN001
    env = cb2_run.gcs_hmac_env({**HMAC, "PATH": "/usr/bin"})
    assert env["AWS_ACCESS_KEY_ID"] == HMAC["GCS_HMAC_ACCESS_ID"]
    assert env["AWS_SECRET_ACCESS_KEY"] == HMAC["GCS_HMAC_SECRET"]
    assert env["AWS_REGION"] == "auto"
    assert env["PATH"] == "/usr/bin"  # the rest of the environment is kept


def test_hmac_env_is_none_without_a_key(cb2_run) -> None:  # noqa: ANN001
    assert cb2_run.gcs_hmac_env({"PATH": "/usr/bin"}) is None
    assert cb2_run.gcs_hmac_env({"GCS_HMAC_ACCESS_ID": "", "GCS_HMAC_SECRET": ""}) is None


def test_half_an_hmac_key_is_an_error(cb2_run) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit, match="GCS_HMAC_SECRET"):
        cb2_run.gcs_hmac_env({"GCS_HMAC_ACCESS_ID": "GOOG1EXAMPLE"})
    with pytest.raises(SystemExit, match="GCS_HMAC_ACCESS_ID"):
        cb2_run.gcs_hmac_env({"GCS_HMAC_SECRET": "x"})


def test_s5cmd_on_gcs_uses_the_xml_endpoint(cb2_run, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", HMAC["GCS_HMAC_ACCESS_ID"])
    monkeypatch.setenv("GCS_HMAC_SECRET", HMAC["GCS_HMAC_SECRET"])
    cmd, env = cb2_run.s5cmd("gs", "ls", "s3://b/", workers=4)
    assert cmd == [
        "s5cmd",
        "--endpoint-url",
        "https://storage.googleapis.com",
        "--numworkers",
        "4",
        "ls",
        "s3://b/",
    ]
    assert env["AWS_ACCESS_KEY_ID"] == HMAC["GCS_HMAC_ACCESS_ID"]


def test_s5cmd_on_s3_uses_the_pod_credentials(cb2_run, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", HMAC["GCS_HMAC_ACCESS_ID"])
    monkeypatch.setenv("GCS_HMAC_SECRET", HMAC["GCS_HMAC_SECRET"])
    cmd, env = cb2_run.s5cmd("s3", "ls", "s3://b/")
    assert cmd == ["s5cmd", "ls", "s3://b/"]
    assert env is None  # inherits the environment: IRSA / AWS credentials


def test_s5cmd_on_gcs_without_a_key_is_an_error(cb2_run) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit, match="GCS_HMAC_ACCESS_ID"):
        cb2_run.s5cmd("gs", "ls", "s3://b/")


# -- reads -------------------------------------------------------------------


def test_private_gcs_submission_is_read_through_s5cmd_with_hmac(
    cb2_run, calls, anonymous, monkeypatch, tmp_path,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", HMAC["GCS_HMAC_ACCESS_ID"])
    monkeypatch.setenv("GCS_HMAC_SECRET", HMAC["GCS_HMAC_SECRET"])
    dest = tmp_path / "model"
    out = cb2_run.fetch("gs://climatebench-submissions/MyModel", dest, workers=8, private=True)
    assert out == dest
    assert anonymous == []  # never the anonymous JSON API
    (call,) = calls
    assert call["cmd"] == [
        "s5cmd",
        "--endpoint-url",
        "https://storage.googleapis.com",
        "--numworkers",
        "8",
        "cp",
        "s3://climatebench-submissions/MyModel/*",
        f"{dest}/",
    ]
    assert call["check"] is True
    assert call["env"]["AWS_ACCESS_KEY_ID"] == HMAC["GCS_HMAC_ACCESS_ID"]
    assert call["env"]["AWS_SECRET_ACCESS_KEY"] == HMAC["GCS_HMAC_SECRET"]


def test_public_bundles_stay_anonymous_even_with_a_key(
    cb2_run, calls, anonymous, monkeypatch, tmp_path,  # noqa: ANN001
) -> None:
    """The key needs no read right on gs://climatebench."""
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", HMAC["GCS_HMAC_ACCESS_ID"])
    monkeypatch.setenv("GCS_HMAC_SECRET", HMAC["GCS_HMAC_SECRET"])
    dest = tmp_path / "references"
    cb2_run.fetch("gs://climatebench/v2/references/", dest)
    assert calls == []
    assert [(b, n) for b, n, _d, _s in anonymous] == [
        ("climatebench", "v2/references/a/x.nc"),
    ]
    assert anonymous[0][2] == dest / "a" / "x.nc"


def test_gcs_submission_without_a_key_is_read_anonymously(
    cb2_run, calls, anonymous, tmp_path,  # noqa: ANN001
) -> None:
    cb2_run.fetch("gs://some-public-bucket/MyModel/", tmp_path / "model", private=True)
    assert calls == []
    assert [n for _b, n, _d, _s in anonymous] == ["MyModel/a/x.nc"]


def test_empty_gcs_listing_is_an_error(cb2_run, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setattr(cb2_run, "_gcs_list", lambda _bucket, _prefix: [])
    with pytest.raises(SystemExit, match="HMAC"):
        cb2_run.fetch("gs://climatebench-submissions/Nope/", tmp_path / "m", private=True)


def test_s3_submission_uses_s5cmd_without_the_gcs_endpoint(
    cb2_run, calls, monkeypatch, tmp_path,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", HMAC["GCS_HMAC_ACCESS_ID"])
    monkeypatch.setenv("GCS_HMAC_SECRET", HMAC["GCS_HMAC_SECRET"])
    dest = tmp_path / "model"
    cb2_run.fetch("s3://subs/MyModel/", dest, workers=2, private=True)
    (call,) = calls
    assert call["cmd"] == ["s5cmd", "--numworkers", "2", "cp", "s3://subs/MyModel/*", f"{dest}/"]
    assert call["env"] is None


def test_local_input_is_used_in_place(cb2_run, calls, tmp_path) -> None:  # noqa: ANN001
    src = tmp_path / "MyModel"
    src.mkdir()
    assert cb2_run.fetch(str(src), tmp_path / "unused") == src.resolve()
    assert calls == []


# -- writes ------------------------------------------------------------------


def test_upload_to_gcs_shares_the_hmac_handling(
    cb2_run, calls, monkeypatch, tmp_path,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", HMAC["GCS_HMAC_ACCESS_ID"])
    monkeypatch.setenv("GCS_HMAC_SECRET", HMAC["GCS_HMAC_SECRET"])
    cb2_run.upload(tmp_path, "gs://climatebench/v2/results/MyModel/20261030T000000Z/")
    (call,) = calls
    assert call["cmd"] == [
        "s5cmd",
        "--endpoint-url",
        "https://storage.googleapis.com",
        "cp",
        f"{tmp_path}/",
        "s3://climatebench/v2/results/MyModel/20261030T000000Z/",
    ]
    assert call["env"]["AWS_ACCESS_KEY_ID"] == HMAC["GCS_HMAC_ACCESS_ID"]


def test_upload_to_gcs_without_a_key_is_an_error(cb2_run, calls, tmp_path) -> None:  # noqa: ANN001
    with pytest.raises(SystemExit, match="GCS_HMAC"):
        cb2_run.upload(tmp_path, "gs://climatebench/v2/results/x/")
    assert calls == []


def test_upload_to_s3(cb2_run, calls, tmp_path) -> None:  # noqa: ANN001
    cb2_run.upload(tmp_path, "s3://results/x")
    (call,) = calls
    assert call["cmd"] == ["s5cmd", "cp", f"{tmp_path}/", "s3://results/x/"]
    assert call["env"] is None


# -- main: which input is read how --------------------------------------------


def test_main_reads_only_the_submission_with_the_key(
    cb2_run, monkeypatch, tmp_path,  # noqa: ANN001
) -> None:
    fetched: list[tuple[str, bool]] = []

    def fake_fetch(uri, dest, *, workers=16, private=False):  # noqa: ANN001, ANN202, ARG001
        fetched.append((uri, private))
        msg = "stop after the fetches"
        if len(fetched) == 3:
            raise RuntimeError(msg)
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(cb2_run, "fetch", fake_fetch)
    monkeypatch.setattr(cb2_run, "upload", lambda _src, _uri: None)
    monkeypatch.setattr(cb2_run, "PROFILE_DIR", RUNNER.parent / "profiles")
    status = cb2_run.main(
        [
            "--model-uri=gs://climatebench-submissions/MyModel/",
            "--name=MyModel",
            "--profile=v2",
            f"--workdir={tmp_path}",
            "--no-verify",
        ],
    )
    assert status != 0
    assert fetched == [
        ("gs://climatebench/v2/references/", False),
        ("gs://climatebench/v2/comparison/", False),
        ("gs://climatebench-submissions/MyModel/", True),
    ]
