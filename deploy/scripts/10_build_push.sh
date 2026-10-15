#!/usr/bin/env bash
# 10_build_push.sh — build the page service image and push it to this test's ECR repo.
#
# Creates the repo (tagged) if it is missing. Builds only when the local image is
# missing or --rebuild is given. Safe to re-run.
source "$(dirname "$0")/lib.sh"

LOCAL_IMAGE=doc-parser-page:$IMAGE_TAG

if ! aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
  log "creating ECR repo $ECR_REPO"
  # shellcheck disable=SC2046
  aws ecr create-repository --repository-name "$ECR_REPO" \
    --image-scanning-configuration scanOnPush=true \
    --tags $(ecr_tags) >/dev/null
fi

if [[ ${1:-} == --rebuild ]] || ! sudo docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
  log "building $LOCAL_IMAGE"
  sudo docker build -f "$REPO_ROOT/deploy/docker/Dockerfile" -t "$LOCAL_IMAGE" "$REPO_ROOT"
fi

log "pushing $IMAGE_URI"
aws ecr get-login-password | sudo docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null
sudo docker tag "$LOCAL_IMAGE" "$IMAGE_URI"
sudo docker push "$IMAGE_URI" | tail -1

aws ecr describe-images --repository-name "$ECR_REPO" --image-ids imageTag="$IMAGE_TAG" \
  --query 'imageDetails[0].[imageDigest,imageSizeInBytes,imagePushedAt]' --output text \
  | tee "$EVIDENCE/image.txt"
