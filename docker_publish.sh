#!/usr/bin/env bash
set -euo pipefail

local_tag="postings-api:latest"
remote_tag="harbor.nb.no/sprakbanken/postings-api:latest"

echo "Building Docker image: ${local_tag}"
docker build -t "${local_tag}" .

echo "Tagging image: ${local_tag} -> ${remote_tag}"
docker tag "${local_tag}" "${remote_tag}"

echo "Pushing image: ${remote_tag}"
docker push "${remote_tag}"
