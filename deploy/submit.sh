#!/bin/bash
# Submit one ClimateBench v2 scoring Job to the current kubectl context.
#
#   deploy/submit.sh MODEL_NAME MODEL_URI OUTPUT_URI [IMAGE_TAG]
#
# e.g. deploy/submit.sh MyModel s3://my-bucket/MyModel/ s3://cb2-results/ v2.0.0
set -euo pipefail
NAME=${1:?model name}
MODEL_URI=${2:?model URI (s3://...)}
OUTPUT_URI=${3:?output URI (s3://...)}
TAG=${4:-latest}
HERE=$(cd "$(dirname "$0")" && pwd)
sed -e "s#<MODEL_NAME>#${NAME}#g" \
    -e "s#<MODEL_URI>#${MODEL_URI}#g" \
    -e "s#<OUTPUT_URI>#${OUTPUT_URI}#g" \
    -e "s#<IMAGE_TAG>#${TAG}#g" \
    "$HERE/k8s/score-job.yaml" | kubectl create -f -
