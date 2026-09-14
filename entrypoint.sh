#!/usr/bin/env bash
# Entrypoint of a Gittensor compute-pool workload image. Spec: MANIFEST.md.
#
# 1. Verify every `artifacts[]` entry of /manifest.yaml on disk: the file (or every file under the directory) must
#    hash to the manifest's sha256. The controller pre-staged them before start; a mismatch means the wrong weights,
#    a partial stage, or tampering, and the only right answer is to refuse to serve (exit 1). Never re-download:
#    egress is closed and the controller owns staging.
# 2. Exec the server (CMD). The server becomes PID 1's child; SIGTERM is forwarded to it.
#
# The drain contract (manifest `drain`): when the lease ends the controller sends SIGTERM, waits `drain.max_s`,
# then removes the container. Your server must, on SIGTERM, stop accepting requests, finish in-flight work
# (`requests`) or write its state (`checkpoint`), and exit within that window. `kill` means no grace.
set -euo pipefail

MANIFEST="${MANIFEST_PATH:-/manifest.yaml}"

verify_artifacts() {
    python3 - "$MANIFEST" <<'PY'
import hashlib, os, sys, yaml

manifest = yaml.safe_load(open(sys.argv[1])) or {}
failed = False
for art in manifest.get('artifacts') or []:
    path, want = art['path'], art['sha256'].lower()
    if os.path.isdir(path):
        # A directory artifact: sha256 over its files in sorted relative order, each as "<relpath>\0<sha256>\n".
        h = hashlib.sha256()
        for root, _dirs, files in sorted(os.walk(path)):
            for name in sorted(files):
                full = os.path.join(root, name)
                fh = hashlib.sha256()
                with open(full, 'rb') as f:
                    for chunk in iter(lambda: f.read(1 << 20), b''):
                        fh.update(chunk)
                h.update(os.path.relpath(full, path).encode() + b'\0' + fh.hexdigest().encode() + b'\n')
        got = h.hexdigest()
    elif os.path.isfile(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        got = h.hexdigest()
    else:
        print(f'>> FATAL: artifact {path} is missing (the controller pre-stages it; never download here)', file=sys.stderr)
        failed = True
        continue
    if got != want:
        print(f'>> FATAL: artifact {path} sha256 MISMATCH (got {got}, want {want}); refusing to serve', file=sys.stderr)
        failed = True
    else:
        print(f'>> artifact {path} sha256 OK', file=sys.stderr)
sys.exit(1 if failed else 0)
PY
}

if [ "${SKIP_ARTIFACT_CHECK:-0}" = "1" ]; then
    # Only for `scripts/validate --build` smoke runs on a machine with no weights. Never set in a blessed deploy.
    echo '>> SKIP_ARTIFACT_CHECK=1: not verifying artifacts (smoke run)' >&2
else
    verify_artifacts
fi

# exec so the server is the process the controller's SIGTERM reaches directly.
exec "$@"
