#!/usr/bin/env bash
# The automatic checks of the Docker image for one CPU type (spec 53, B4 part 1).
#
#   scripts/docker-checks.sh arm64
#   scripts/docker-checks.sh amd64     # on an ARM machine, Docker runs it with emulation (slow)
#
# Cost: 0 Gemini calls, 0 BigQuery queries. Each container runs with --network none, with a fake
# key or no key, and without the gcloud folder. The script does not read .env or data/.
# The builder: the current `docker buildx` builder. Set BUILDX_BUILDER to use another one.
set -uo pipefail

platform="${1:-}"
case "$platform" in
  amd64 | arm64) ;;
  *)
    echo "Usage: scripts/docker-checks.sh amd64|arm64" >&2
    exit 2
    ;;
esac

cd "$(dirname "$0")/.."
image="opsfleet-agent:$platform"
test_image="opsfleet-agent-test:$platform"
run=(docker run --rm --platform "linux/$platform" --network none)
fake_env=(-e GOOGLE_API_KEY=fake-key-for-checks -e GOOGLE_CLOUD_PROJECT=check-project)
failures=0

pass() { echo "PASS  $1"; }
fail() {
  echo "FAIL  $1"
  failures=$((failures + 1))
}

# check NAME EXPECTED_EXIT_CODE EXPECTED_TEXT -- COMMAND...
# Runs the command, then compares the exit code and looks for the text in the output.
check() {
  local name="$1" want_code="$2" want_text="$3"
  shift 4
  local out code
  out="$("$@" 2>&1)"
  code=$?
  if [[ "$code" == "$want_code" && "$out" == *"$want_text"* ]]; then
    pass "$name (exit $code)"
  else
    fail "$name: exit $code, expected $want_code and the text '$want_text'"
    printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
  fi
}

build() {
  local tag="$1" target="$2" start end
  start=$(date +%s)
  if docker buildx build --platform "linux/$platform" --target "$target" -t "$tag" --load \
    --quiet . >/dev/null; then
    end=$(date +%s)
    pass "build $tag (target $target) in $((end - start)) s"
  else
    fail "build $tag (target $target)"
    exit 1
  fi
}

echo "== Build (linux/$platform)"
build "$image" runtime
build "$test_image" test

echo "== Unit tests in the test image (no network)"
out="$("${run[@]}" "$test_image" 2>&1)"
if [[ $? == 0 ]]; then
  pass "unit tests: $(printf '%s\n' "$out" | tail -1)"
else
  fail "unit tests"
  printf '%s\n' "$out" | tail -20 | sed 's/^/      /'
fi

echo "== The image"
uid="$("${run[@]}" --entrypoint id "$image" -u)"
if [[ "$uid" =~ ^[0-9]+$ && "$uid" != 0 ]]; then pass "user without root rights (uid $uid)"; else fail "uid is '$uid'"; fi

found="$("${run[@]}" --entrypoint sh "$image" -c '
  find / -xdev \( -name .env -o -name application_default_credentials.json \
    -o -name "*-key.json" -o -name "service-account*.json" -o -name CLAUDE.local.md \
    -o -name .claude -o -name .git \) -print 2>/dev/null
  ls -A /app/data
  ls -d /app/docs /app/tests /app/.env 2>/dev/null
  true')"
if [[ -z "$found" ]]; then
  pass "no .env, key, ADC file, CLAUDE.local.md, .claude/, .git/, docs/, tests/; data/ is empty"
else
  fail "files that must not be in the image:"
  printf '%s\n' "$found" | sed 's/^/      /'
fi

echo "== Commands without network, key or credentials"
check "opsfleet-agent --help" 0 "Usage: opsfleet-agent" -- "${run[@]}" "$image" --help
check "opsfleet-eval --help" 0 "Usage: opsfleet-eval" -- \
  "${run[@]}" --entrypoint opsfleet-eval "$image" --help
check "chat without -it" 2 "The chat needs an interactive terminal. Run docker with -it." -- \
  "${run[@]}" "${fake_env[@]}" "$image" chat --user maya

# With -i and piped input, as in a scripted session.
check "unknown user" 2 "User 'nobody' is not in config/users.yaml." -- \
  sh -c "printf '/exit\n' | $(printf '%q ' "${run[@]}") -i ${fake_env[*]} $image chat --user nobody"
check "no GOOGLE_API_KEY" 2 "GOOGLE_API_KEY is not set." -- \
  sh -c "printf '/exit\n' | $(printf '%q ' "${run[@]}") -i -e GOOGLE_CLOUD_PROJECT=check-project $image chat --user maya"
check "no gcloud folder (ADC)" 3 "gcloud auth application-default login" -- \
  sh -c "printf '/exit\n' | $(printf '%q ' "${run[@]}") -i ${fake_env[*]} $image chat --user maya"

echo "== Size"
size="$(docker image inspect "$image" --format '{{.Size}}')"
echo "INFO  $image: $((size / 1000000)) MB ($(docker image ls "$image" --format '{{.Size}}' | head -1) in docker image ls)"

echo
if [[ $failures == 0 ]]; then
  echo "All checks passed for linux/$platform."
else
  echo "$failures check(s) failed for linux/$platform."
  exit 1
fi
