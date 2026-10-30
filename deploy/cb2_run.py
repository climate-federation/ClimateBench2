"""Score one ClimateBench v2 submission inside the container (``cb2-run``).

Pulls three inputs from object storage, runs ``climatebench2 score`` with the
frozen CMIP6 comparison ensemble, renders the leaderboard, and uploads the
result directory:

1. the **reference bundle** (``<source id>/<frequency>/<var>/*.nc`` plus
   ``MANIFEST.json``), verified file by file against its sha256;
2. the **frozen comparison bundle** (``climatebench2 freeze-comparison``);
3. the **submission**: one directory per experiment under the model URI, the
   Tier II ensemble under ``historical-ssp245/<member>/``, e.g.::

       gs://climatebench-submissions/MyModel/historical-ssp245/r1i1p1f1/*.nc
       gs://climatebench-submissions/MyModel/historical-ssp245/r2i1p1f1/*.nc
       gs://climatebench-submissions/MyModel/piControl/*.nc
       gs://climatebench-submissions/MyModel/abrupt-4xCO2/*.nc

   (an ``s3://`` prefix or a local path works the same way).

   Experiment directory names map to ``--experiment`` keys through the
   profile; absent experiments only skip the gates that need them.

What must be identical between submissions (window, suites, bundle URIs) is
the *profile* (``deploy/profiles/<name>.yml``), recorded in ``run.json`` with
the software versions so every result says what it was scored against.

URIs: ``s3://`` goes through ``s5cmd`` with the pod's AWS credentials (an
IRSA role, if any). ``gs://`` goes through ``s5cmd`` against GCS's
S3-compatible XML API, authenticated with a service account's HMAC key
(``$GCS_HMAC_ACCESS_ID`` / ``$GCS_HMAC_SECRET``, the ``cb2-gcs-hmac``
Secret), for **writing** results and for **reading the submission** from a
private bucket (``gs://climatebench-submissions``). The public bundles
(references and comparison, in public-read ``gs://climatebench``) are read
**anonymously** over the JSON API by default, so the key needs no read right
there; a ``gs://`` submission is read anonymously too when no key is set.
Anything else is a local path. Results default to
``gs://climatebench/v2/results/``. Only the standard library is used here;
``climatebench2`` runs as a subprocess in the image's pixi environment.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROFILE_DIR = Path(os.environ.get("CB2_PROFILE_DIR", "/opt/cb2/profiles"))
GCS_API = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"
#: GCS's S3-compatible (XML API) endpoint, used with an HMAC key.
GCS_S3_ENDPOINT = "https://storage.googleapis.com"
#: Environment variables holding the GCS HMAC key (the ``cb2-gcs-hmac`` Secret).
GCS_HMAC_ENV = ("GCS_HMAC_ACCESS_ID", "GCS_HMAC_SECRET")
GCS_MEDIA = "https://storage.googleapis.com/{bucket}/{name}"
VARIANT_RE = re.compile(r"^r\d+i\d+p\d+f\d+$")


def log(msg: str) -> None:
    """Timestamped progress line on stderr (the pod log)."""
    stamp = dt.datetime.now(dt.UTC).strftime("%H:%M:%S")
    print(f"[cb2-run {stamp}] {msg}", file=sys.stderr, flush=True)


# -- profiles ----------------------------------------------------------------


def load_profile(name_or_path: str) -> dict[str, Any]:
    """A profile by name (``v2`` -> ``$CB2_PROFILE_DIR/v2.yml``) or by path."""
    import yaml  # the pixi env has pyyaml; imported late so --help needs nothing

    path = Path(name_or_path)
    if not path.suffix:
        path = PROFILE_DIR / f"{name_or_path}.yml"
    with path.open() as fh:
        profile = yaml.safe_load(fh)
    profile["_path"] = str(path)
    return profile


# -- object storage ----------------------------------------------------------


def split_uri(uri: str) -> tuple[str, str, str]:
    """``(scheme, bucket, prefix)``; scheme is '' for a local path."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme in {"s3", "gs"}:
        return parsed.scheme, parsed.netloc, parsed.path.lstrip("/")
    return "", "", uri


def _gcs_list(bucket: str, prefix: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token = None
    while True:
        query = {"prefix": prefix, "fields": "items(name,size),nextPageToken"}
        if token:
            query["pageToken"] = token
        url = GCS_API.format(bucket=bucket) + "?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
            page = json.load(resp)
        items.extend(page.get("items", []))
        token = page.get("nextPageToken")
        if not token:
            return items


def _gcs_get(bucket: str, name: str, dest: Path, size: int) -> None:
    if dest.exists() and dest.stat().st_size == size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = GCS_MEDIA.format(bucket=bucket, name=urllib.parse.quote(name))
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=300) as resp, tmp.open("wb") as out:  # noqa: S310
                shutil.copyfileobj(resp, out, length=16 << 20)
            tmp.replace(dest)
            return
        except OSError as err:
            log(f"retry {attempt + 1}/5 for gs://{bucket}/{name}: {err}")
            time.sleep(2**attempt)
    msg = f"could not download gs://{bucket}/{name}"
    raise RuntimeError(msg)


def gcs_hmac_env(environ: dict[str, str] | None = None) -> dict[str, str] | None:
    """The environment for ``s5cmd`` against GCS, or None without an HMAC key.

    The one place the HMAC key is read, for reads and writes alike: GCS's
    S3-compatible API takes the key as AWS-style static credentials. A key
    with only one of its two halves set is an error, not "no key".
    """
    environ = dict(os.environ if environ is None else environ)
    access_id, secret = (environ.get(name, "") for name in GCS_HMAC_ENV)
    if not access_id and not secret:
        return None
    if not (access_id and secret):
        missing = GCS_HMAC_ENV[0] if not access_id else GCS_HMAC_ENV[1]
        msg = f"incomplete GCS HMAC key: {missing} is not set"
        raise SystemExit(msg)
    return environ | {
        "AWS_ACCESS_KEY_ID": access_id,
        "AWS_SECRET_ACCESS_KEY": secret,
        "AWS_REGION": "auto",
    }


def s5cmd(scheme: str, *args: str, workers: int | None = None) -> tuple[list[str], dict[str, str] | None]:
    """``(argv, env)`` of an ``s5cmd`` call on ``s3://`` or (via HMAC) ``gs://``.

    ``gs://`` objects are addressed as ``s3://<bucket>/...`` against
    :data:`GCS_S3_ENDPOINT`; without an HMAC key that is an error.
    """
    cmd = ["s5cmd"]
    env = None
    if scheme == "gs":
        env = gcs_hmac_env()
        if env is None:
            msg = (
                f"gs:// through s5cmd needs {GCS_HMAC_ENV[0]} and {GCS_HMAC_ENV[1]} "
                "(a GCS HMAC key; the cb2-gcs-hmac Secret)"
            )
            raise SystemExit(msg)
        cmd += ["--endpoint-url", GCS_S3_ENDPOINT]
    if workers is not None:
        cmd += ["--numworkers", str(workers)]
    return [*cmd, *args], env


def fetch(uri: str, dest: Path, *, workers: int = 16, private: bool = False) -> Path:
    """Copy everything under ``uri`` into ``dest`` (a local path is linked).

    ``private`` marks a ``gs://`` input that may sit in a private bucket (the
    submission): it is read through ``s5cmd`` with the HMAC key when one is
    set. Otherwise — and always for the public bundles — ``gs://`` is read
    anonymously over the JSON API, which needs no credentials at all.
    """
    scheme, bucket, prefix = split_uri(uri)
    if not scheme:
        src = Path(prefix).resolve()
        if not src.exists():
            msg = f"input not found: {src}"
            raise SystemExit(msg)
        return src
    dest.mkdir(parents=True, exist_ok=True)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    authenticated = scheme == "s3" or (private and gcs_hmac_env() is not None)
    log(f"fetching {uri} -> {dest}" + (" (anonymous)" if not authenticated else ""))
    if authenticated:
        cmd, env = s5cmd(scheme, "cp", f"s3://{bucket}/{prefix}*", f"{dest}/", workers=workers)
        subprocess.run(cmd, check=True, env=env)
        return dest
    items = [i for i in _gcs_list(bucket, prefix) if not i["name"].endswith("/")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_gcs_get, bucket, i["name"], dest / i["name"][len(prefix) :], int(i["size"]))
            for i in items
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    log(f"fetched {len(items)} objects from {uri}")
    if not items:
        hint = " (a private bucket needs the GCS HMAC key)" if private else ""
        msg = f"no objects under {uri}{hint}"
        raise SystemExit(msg)
    return dest


def upload(src: Path, uri: str) -> None:
    """Copy ``src`` to ``uri`` (gs://, s3:// or a local directory).

    ``gs://`` goes through GCS's S3-compatible XML API with the HMAC key of a
    service account that may write the results prefix (:func:`gcs_hmac_env`).
    """
    scheme, bucket, prefix = split_uri(uri)
    if not scheme:
        shutil.copytree(src, Path(prefix), dirs_exist_ok=True)
        return
    prefix = prefix.rstrip("/")
    cmd, env = s5cmd(scheme, "cp", f"{src}/", f"s3://{bucket}/{prefix}/")
    subprocess.run(cmd, check=True, env=env)


def verify_manifest(root: Path, *, workers: int = 8) -> int:
    """Check every file of a reference bundle against ``MANIFEST.json``."""
    manifest = json.loads((root / "MANIFEST.json").read_text())

    def check(entry: dict[str, Any]) -> str | None:
        path = root / entry["path"]
        if not path.is_file() or path.stat().st_size != int(entry["bytes"]):
            return f"missing or wrong size: {entry['path']}"
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(16 << 20), b""):
                digest.update(chunk)
        return None if digest.hexdigest() == entry["sha256"] else f"sha256 mismatch: {entry['path']}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        problems = [p for p in pool.map(check, manifest["files"]) if p]
    if problems:
        msg = "reference bundle failed verification:\n  " + "\n  ".join(problems)
        raise SystemExit(msg)
    return len(manifest["files"])


# -- the submission ----------------------------------------------------------


def discover(model_root: Path, profile: dict[str, Any]) -> tuple[dict[str, Path], dict[str, Path]]:
    """``(members, experiments)`` of a submission laid out as the profile says."""
    ensemble = model_root / profile["ensemble_dir"]
    if not ensemble.is_dir():
        msg = f"no {profile['ensemble_dir']}/ directory under the model URI"
        raise SystemExit(msg)
    members = {
        p.name: p for p in sorted(ensemble.iterdir(), key=_variant_key) if p.is_dir() and VARIANT_RE.match(p.name)
    }
    if not members:
        members = {"r1i1p1f1": ensemble}  # a flat single-member directory
    experiments = {
        key: model_root / name for name, key in profile["experiments"].items() if (model_root / name).is_dir()
    }
    return members, experiments


def _variant_key(path: Path) -> tuple[int, ...]:
    nums = re.findall(r"\d+", path.name)
    return tuple(int(n) for n in nums) if VARIANT_RE.match(path.name) else (1 << 30,)


# -- results -----------------------------------------------------------------


def unscored_rows(out_dir: Path) -> list[dict[str, Any]]:
    """Scored Tier II rows of the submission that ended without an ``E_ref``."""
    try:
        import duckdb
    except ImportError:
        return []
    rows: list[dict[str, Any]] = []
    for db in sorted(out_dir.glob("*.ddb")):
        con = duckdb.connect(str(db), read_only=True)
        try:
            tables = con.sql(
                "select schema_name from duckdb_columns() where table_name = 'metrics' and column_name = 'e_ref'",
            ).fetchall()
            for (schema,) in tables:
                for var_id, n in con.sql(
                    f'select var_id, count(*) from "{schema}".metrics '  # noqa: S608
                    "where scorer = 'climatebench2' and e_ref is null group by var_id",
                ).fetchall():
                    rows.append({"suite": db.stem, "diagnostic": schema, "var_id": var_id, "rows": n})
        except duckdb.Error as err:
            log(f"could not inspect {db.name}: {err}")
        finally:
            con.close()
    return rows


def software_versions() -> dict[str, str]:
    versions = {"climateeval_commit": Path("/opt/climateeval/COMMIT").read_text().strip()
                if Path("/opt/climateeval/COMMIT").exists() else "unknown"}
    try:
        from importlib.metadata import version

        versions["climatebench2"] = version("climatebench2")
    except Exception:  # noqa: BLE001
        versions["climatebench2"] = "unknown"
    versions["image"] = os.environ.get("CB2_IMAGE", "unknown")
    return versions


# -- main --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--model-uri",
        required=True,
        help="Submission root: gs://<private-bucket>/<model>/ (read with the GCS HMAC key), s3:// or a local path.",
    )
    p.add_argument("--name", required=True, help="Model name recorded in the results.")
    p.add_argument(
        "--output-uri",
        help="Where the result directory is uploaded (gs://, s3:// or local; default: the profile's results_uri).",
    )
    p.add_argument("--profile", default=os.environ.get("CB2_PROFILE", "v2"), help="Profile name or YAML path.")
    p.add_argument("--references-uri", help="Override the profile's reference bundle URI.")
    p.add_argument("--comparison-uri", help="Override the profile's frozen comparison bundle.")
    p.add_argument("--workdir", type=Path, default=Path(os.environ.get("CB2_WORKDIR", "/scratch")))
    p.add_argument("--suite", action="append", help="Run only these suites (repeatable; default: the profile's).")
    p.add_argument("--not-applicable", action="append", default=[], help="Tier I gate declared n/a (repeatable).")
    p.add_argument("--institute", default="")
    p.add_argument("--max-members", type=int, default=10, help="Score at most this many members (paper: 10).")
    p.add_argument("--no-verify", action="store_true", help="Skip the reference sha256 check.")
    return p


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915
    args = build_parser().parse_args(argv)
    profile = load_profile(args.profile)
    args.output_uri = args.output_uri or profile["results_uri"]
    started = dt.datetime.now(dt.UTC)
    work = args.workdir
    out_dir = work / "out" / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "name": args.name,
        "profile": {k: v for k, v in profile.items() if not k.startswith("_")},
        "inputs": {"model_uri": args.model_uri},
        "software": software_versions(),
        "started": started.isoformat(),
    }
    status = 1
    try:
        refs_uri = args.references_uri or profile["references_uri"]
        comp_uri = args.comparison_uri or profile["comparison_uri"]
        record["inputs"] |= {"references_uri": refs_uri, "comparison_uri": comp_uri}
        refs = fetch(refs_uri, work / "references")
        if profile.get("verify_references", True) and not args.no_verify:
            log(f"verified {verify_manifest(refs)} reference files")
        comparison = fetch(comp_uri, work / "comparison")
        model = fetch(args.model_uri, work / "model", private=True)

        members, experiments = discover(model, profile)
        if len(members) > args.max_members:
            log(f"{len(members)} members supplied; scoring the first {args.max_members}")
            members = dict(list(members.items())[: args.max_members])
        record["inputs"] |= {"members": list(members), "experiments": sorted(experiments)}
        log(f"members: {', '.join(members)}; experiments: {', '.join(sorted(experiments)) or 'none'}")

        cmd = [
            "climatebench2", "score", str(next(iter(members.values()))),
            "--name", args.name, "--institute", args.institute,
            "--data-root", str(refs),
            "--comparison", "frozen", "--frozen-comparison", str(comparison),
            "--timerange", profile["timerange"],
            "-o", str(out_dir),
        ]  # fmt: skip
        for label, path in members.items():
            cmd += ["--member", f"{label}={path}"]
        for key, path in sorted(experiments.items()):
            cmd += ["--experiment", f"{key}={path}"]
        for suite in args.suite or profile["suites"]:
            cmd += ["--suite", suite]
        for gate in args.not_applicable:
            cmd += ["--not-applicable", gate]
        if profile.get("allow_missing_comparison"):
            cmd.append("--allow-missing-comparison")
        record["command"] = cmd
        log("running: " + " ".join(cmd))
        with (out_dir / "score.log").open("w") as fh:
            score = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, check=False)
        record["score_exit_code"] = score.returncode

        dbs = sorted(str(p) for p in out_dir.glob("*.ddb"))
        if dbs:
            subprocess.run(
                ["climatebench2", "leaderboard", *dbs, "-o", str(out_dir / "leaderboard.html")], check=False,
            )
            subprocess.run(
                ["climatebench2", "leaderboard", *dbs, "--csv", str(out_dir / "scores.csv")], check=False,
            )
        record["unscored"] = unscored_rows(out_dir)
        status = score.returncode
    except BaseException as err:  # noqa: BLE001 - always record and upload what exists
        record["error"] = repr(err)
        log(f"FAILED: {err!r}")
    finally:
        record["finished"] = dt.datetime.now(dt.UTC).isoformat()
        record["status"] = "ok" if status == 0 else "failed"
        (out_dir / "run.json").write_text(json.dumps(record, indent=1, default=str))
        try:
            upload(out_dir, args.output_uri.rstrip("/") + f"/{args.name}/{started:%Y%m%dT%H%M%SZ}")
            log(f"results uploaded to {args.output_uri}")
        except Exception as err:  # noqa: BLE001
            log(f"upload failed: {err!r}")
            status = status or 2
    return status


if __name__ == "__main__":
    sys.exit(main())
