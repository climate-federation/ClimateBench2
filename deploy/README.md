# Scoring submissions in the cloud

`deploy/` packages ClimateBench v2 Tier I and Tier II scoring as a container
that runs one submission per Kubernetes Job, e.g. on AWS EKS.

A cloud run never touches the ~2 TB CMIP6 comparison ensemble. The ensemble's
rows were computed once, on the system that holds the data, and frozen into a
bundle (`climatebench2 freeze-comparison`). A run computes only the
submission and the observations, then merges the frozen rows before the
scoring pass (`climatebench2 score --comparison frozen`). Re-scoring the
paper's own submission this way reproduces every published skill score
exactly.

## What a run needs

| Input | Default location | Size |
|---|---|---|
| Reference bundle `v2` (`<source id>/<frequency>/<var>/*.nc`, `MANIFEST.json`) | `gs://climatebench/v2/references/` | ~34 GB |
| Frozen comparison bundle `v2` (`*.ddb`, `manifest.json`) | `gs://climatebench/v2/comparison/` | <0.1 GB |
| The submission | any `s3://` prefix | 10–300 GB |

`gs://climatebench` is public-read, so both bundles are fetched anonymously.
Results are written to `gs://climatebench/v2/results/<name>/<UTC timestamp>/`
through GCS's S3-compatible API. That needs an HMAC key for a service account
that may create objects there, stored as the Kubernetes Secret `cb2-gcs-hmac`.

## Submission layout

```
s3://bucket/MyModel/
├── historical-ssp245/       # Tier II ensemble, one directory per member (up to 10 are scored)
│   ├── r1i1p1f1/*.nc        # CMOR-like NetCDF, historical + SSP2-4.5 spliced, Amon/Omon/SImon/day
│   └── r2i1p1f1/*.nc
├── piControl/*.nc           # Tier I experiments, each optional:
├── abrupt-4xCO2/*.nc        #   a gate whose experiment is absent is skipped
├── hist-aer/*.nc
├── amip/*.nc
├── amip-4xCO2/*.nc
└── historical-day/*.nc      # daily historical fields (I.3b)
```

The profile (`profiles/v2.yml`) maps directory names to
`climatebench2 score --experiment` keys.

## Profiles

A profile pins everything two submissions must share for their scores to be
comparable:
- the reference and comparison bundle URIs;
- the Tier II window (`20150101/20251231`, clipped per variable to its reference's last complete year);
- the suites.

The CLI refuses a frozen bundle whose protocol fingerprint, CB2 version or
window differs from the run's. Updating an observational product, or the
window, therefore means a new comparison bundle and a new profile.

## Build and run

The image is built by `.github/workflows/image.yml` and pushed to
`ghcr.io/climate-federation/climatebench2` (`:main`, `:sha-…`, `:X.Y.Z`). To
build it locally:

```bash
docker build -f deploy/Dockerfile -t climatebench2:dev .
docker run --rm -v $PWD/scratch:/scratch climatebench2:dev \
  python /opt/cb2/cb2_run.py --model-uri /scratch/MyModel --name MyModel --output-uri /scratch/results
```

On EKS:

```bash
kubectl apply -f deploy/k8s/base.yaml          # namespace + IRSA service account (edit the role ARN)
kubectl -n climatebench create secret generic cb2-gcs-hmac \
    --from-literal=access_id=GOOG1E... --from-literal=secret=...
deploy/submit.sh MyModel s3://subs/MyModel/ main
```

Each run uploads `gs://climatebench/v2/results/<name>/<UTC timestamp>/` (or `--output-uri`), which contains:
- one `.ddb` per suite;
- `leaderboard.html` and `scores.csv`;
- `score.log`;
- `run.json`, with the profile, the software versions, the inputs, and the Tier II rows left without an `E_ref` (variables the bundle does not cover).

## Rebuilding the comparison bundle

Comparison rows can only be computed where the CMIP6 ensemble is on disk.
1. Run the Tier II suites live against the new references there.
2. Freeze them: `climatebench2 freeze-comparison ... -o BUNDLE`. Use `--extend` to add a variable group, or `--replace` to re-freeze one.
3. Upload the bundle under a new version prefix.
