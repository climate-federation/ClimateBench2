# syntax=docker/dockerfile:1.7
#
# ClimateBench v2 scoring image: ClimateEval's locked pixi environment with
# ClimateBench2 installed into it, plus the `cb2-run` entrypoint that pulls a
# submission, the v2 reference bundle and the frozen CMIP6 comparison bundle
# from object storage, scores Tier I and Tier II, and uploads the results.
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t climatebench2:dev .
#
# CLIMATEEVAL_REF pins the ClimateEval commit (the same one as pyproject.toml's
# `climateeval @ git+...` pin); its pixi.lock fixes every library version.

ARG PIXI_VERSION=0.81.0

FROM ghcr.io/prefix-dev/pixi:${PIXI_VERSION}-noble AS build

ARG CLIMATEEVAL_REF=d2559b2fb1f70f37b84d41a281f81e79db754aba
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ClimateEval at the pinned commit, installed from its own lock file. pixi
# installs ClimateEval itself editable from this directory, so the final stage
# keeps it at the same path.
WORKDIR /opt/climateeval
RUN git clone https://github.com/climate-federation/ClimateEval.git . \
 && git checkout --detach "${CLIMATEEVAL_REF}" \
 && git rev-parse HEAD > /opt/climateeval/COMMIT \
 && rm -rf .git
RUN pixi install --frozen -e default \
 && rm -rf ~/.cache/rattler

# ClimateBench2 into the same environment, without dependencies: its
# pyproject pins ClimateEval by URL, which must not replace the checkout above.
COPY . /opt/src/climatebench2
RUN pixi run --frozen -e default python -m pip install --no-deps --no-cache-dir /opt/src/climatebench2 \
 && pixi shell-hook --frozen -e default -s bash > /opt/shell-hook.sh

FROM ubuntu:24.04

ARG S5CMD_VERSION=2.3.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && curl -fsSL "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-64bit.tar.gz" \
    | tar -xz -C /usr/local/bin s5cmd \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/climateeval /opt/climateeval
COPY --from=build /opt/shell-hook.sh /opt/shell-hook.sh
COPY deploy/profiles /opt/cb2/profiles
COPY deploy/esmvaltool /opt/cb2/esmvaltool
COPY deploy/cb2_run.py /opt/cb2/cb2_run.py
COPY deploy/entrypoint.sh /usr/local/bin/cb2-entrypoint

# ESMValCore must never search ESGF or a site rootpath from inside a pod:
# every input arrives through --data-root and the submission's directories.
ENV ESMVALTOOL_CONFIG_DIR=/opt/cb2/esmvaltool \
    CB2_PROFILE_DIR=/opt/cb2/profiles \
    MPLBACKEND=Agg \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

RUN useradd --create-home --uid 1000 cb2 && mkdir -p /scratch && chown cb2 /scratch
USER cb2
WORKDIR /scratch

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/cb2-entrypoint"]
CMD ["python", "/opt/cb2/cb2_run.py", "--help"]
