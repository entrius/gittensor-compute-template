# gittensor-compute-template

The base repo you fork to build a **workload image** for the Gittensor compute pool (Bittensor SN74): a global
pool of verified RTX 5090s that runs blessed images and pays the cards that host them.

The controller never knows what your workload is. It reads one file, **`manifest.yaml`**, and treats every image
the same using only what that file says: which cards it needs, how to tell it is working, what healthy looks
like, and how its output is used. Adding a new runtime means building an image and writing its manifest. No
controller change.

**The spec is [`MANIFEST.md`](MANIFEST.md); the machine-readable contract is [`manifest.schema.json`](manifest.schema.json).**
The controller validates against that schema. Where this repo and any other document differ, this repo wins.

## What is here

| Path | What it is |
|---|---|
| `manifest.yaml` | This template's own example: a Qwen3.8-27B (NVFP4) chat image on sparkinfer, one 5090 per instance |
| `manifests/` | Ready manifests per runtime (`sparkinfer`, `vllm`, `sglang`); copy one over `manifest.yaml` and edit |
| `Dockerfile` | A skeleton image: pinned CUDA base, no weights baked in, a placeholder server you replace |
| `entrypoint.sh` | Verifies the manifest's `artifacts` on disk, starts the server, forwards SIGTERM so it can drain |
| `scripts/validate` | No GPU: manifest against the schema, consistency checks, optionally build + smoke the image |
| `scripts/qualify` | On your own 5090: health, canaries at concurrency, VRAM, load time; prints pass/fail, writes a report |

## Flow

1. **Fork** this repo.
2. **Edit `Dockerfile`** so the image runs your runtime, and **`manifest.yaml`** so it describes it. Weights are
   never baked in: list them under `artifacts` and the controller pre-stages them onto the card before start.
3. **`scripts/validate`** (no GPU). Fix everything it lists.
4. **`scripts/qualify`** on a 5090 you have. It measures what the manifest claims (`max_load_s`, `min_vram_gb`,
   the canaries at `front_door.concurrency`) and prints a pass/fail table.
5. **Submit.** We re-qualify on our own card, record the profile numbers, and sign the image digest and the
   manifest together into the registry (Docker Hub, `entrius/`). Only then can the controller place it.

Blessing is curation, not a queue: we bless images we want as workloads, and accept outside forks when we choose
to curate them. Nothing about a submission is blocked on a verification problem; it is a decision.

```sh
uv venv && uv pip install -e '.[dev]'      # or: pip install jsonschema pyyaml
uv run scripts/validate                     # manifest.yaml against the schema
uv run scripts/validate --build             # also docker build + start + health probe (no GPU needed)
uv run scripts/qualify --models-dir /path/to/weights    # needs an RTX 5090 and the NVIDIA container toolkit
```

## What the controller guarantees back to every image

- Only its pinned cards are visible to it, and nothing else runs on them.
- The ports, env and volumes its manifest asked for.
- Warning before a lease ends: `SIGTERM`, then the declared `drain` window, then removal.

## What the controller does with your image

Bless → the operator enables it with a replica count → the controller places replicas on verified idle cards →
start (health probe within `placement.max_load_s`, one entry canary at random) → serve, watched every ~60 s →
drain and undeploy when the lease ends. Pay to the card runs only while the instance is healthy. The full
lifecycle, every field, and the failure rules are in [`MANIFEST.md`](MANIFEST.md).

## License

MIT, © 2025 Entrius.
