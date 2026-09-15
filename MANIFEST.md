# The manifest: what every blessed image provides

**Version: v0.** The shape is settled; field names and example values may still change as the first images are
blessed, and each change bumps this document. The machine-readable contract is [`manifest.schema.json`](manifest.schema.json);
the controller validates against it, and a manifest that fails the schema is never blessed.

## The idea

The controller never knows what a workload is. Every blessed image ships a `manifest.yaml` that answers four
questions, and the controller treats every image the same using only those answers:

1. **What cards does it need?** (`placement`)
2. **How do we tell it is working?** (`health`)
3. **What does healthy look like?** (`profile`, optional)
4. **How is its output used?** (`front_door`)

Adding a new runtime means building an image and writing its manifest. No controller code changes. This is the
Kubernetes pattern (the app declares its own probes) and the Chutes one (each chute declares its interface).

**The manifest is yours; the deployment is ours.** `manifest.yaml` lives in your image's source repo and says
what the image **is**. Blessing signs the image digest and the manifest together into our registry, and the
controller reads manifests only from that registry, never from a miner box. **Deployment settings** (enabled,
replica count) live only in our registry entry and are operator-owned; they are not manifest fields.

**Scaling is the controller's job.** The manifest declares only per-instance limits (`front_door.concurrency`,
cards, VRAM). The controller derives replica counts and scaling thresholds itself. There are no per-image
autoscale fields.

## Fields

| Field | Required | What it is | The controller uses it to |
|---|---|---|---|
| `name`, `version` | yes | identity; no owner field (we bless everything) | key the registry |
| `description` | no | short text | show it in the operator panel and a future public catalog |
| `runtime` | yes | a label: `sparkinfer`, `vllm`, `sglang`, `custom`, … | group and filter images (no controller logic) |
| `image` | yes | the image reference **pinned by digest** (`repo[:tag]@sha256:…`); built from a normal Dockerfile | pin the run spec |
| `placement.gpu_types` | yes (default `include: [RTX5090]`) | GPU types to `include` / `exclude` | pick cards; 5090-only today, other types later |
| `placement.cards_per_instance` | yes (default 1) | cards one instance needs, on one box | pick cards; >1 means PCIe (no NVLink on 5090) |
| `placement.min_vram_gb` | yes | VRAM the instance needs per card | skip cards that can't fit it |
| `placement.max_load_s` | yes | load deadline from start to a passing health probe | fail a start that hangs (unpaid, standing drops); size the lease cycle |
| `run.env` / `run.volumes` | as needed | env and mounts (e.g. the model dir) | build the run spec |
| `network.egress` | no (default: nothing allowed) | a host allowlist for outbound traffic the workload itself needs at run time (an external API a tool-using workload calls). Weights and data never need it: the controller pre-stages `artifacts` | enforce egress in the run spec |
| `artifacts` | no | files with `path`, `source`, `revision` and `sha256` that must match before use | fetch from `source` at `revision` during pre-staging (controller-side, not the workload), then verify on disk at entry |
| `health` | yes | an HTTP check (`path`, `port`, `expect_status`) **or** a `command`; plus `interval_s`, `failure_threshold` | know the workload itself is up |
| `entry_canary` | no | a list of one or more canaries, each a `type` (`http`, `command`, `fixture`), one input and a `pass` rule; the controller picks **one at random** each start; sent at `front_door.concurrency`, so it doubles as warmup | sanity-gate a start (broken image, wrong weights, bad deploy) before use; not anti-cheat |
| `profile` | no | healthy numbers recorded at qualification | flag drift in-lease (**health and standing only, never pay**) |
| `front_door.type` | yes | `gateway-openai`, `http`, or the reserved `batch` | route work to the instance |
| `front_door.port`, `front_door.routes` | yes for HTTP types | the port; routes as `path`, `method`, `stream`, each with optional `request_schema` / `response_schema` | route (passthrough by default), validate at the gateway, generate docs, check canaries |
| `front_door.concurrency` | yes | max simultaneous requests per instance | route on it, run the canary at it, derive replica counts |
| `drain` | yes | `requests` (finish in-flight, within `max_s`), `checkpoint`, or `kill` | end a lease without breaking work; a drain past `max_s` is a failed drain (standing drops) |

### Not in the manifest

- **Deployment settings**: enabled, replica count. Registry entry, operator-owned.
- **Scaling fields**: no `max_instances`, `scaling_threshold`, `shutdown_after`. The controller derives them.
- **Owner / username.** We bless everything; the registry knows who submitted it.
- **Capabilities** (tools, vision, video, reasoning). They come from the runtime's `/v1/models`, see
  `gateway-openai` below.
- **A per-image public API path.** The gateway routes OpenAI-compatible work by model name.
- **Rejected on purpose:** `passthrough_headers`; required timing fields in inference responses (they stay
  welcome, but optional); a max hourly price; `encrypted_fs`, `tee`, `lock_modules`; in-container verification
  routes; job SSH.

## Lifecycle: how the controller uses each field

1. **Bless.** You build the image from a normal Dockerfile and write `manifest.yaml`. We qualify it once on our
   own 5090 (VRAM held, load time, the health probe passing, our evals, the profile numbers) and sign the image
   digest and manifest together into the registry.
2. **Toggle.** The operator enables it in the registry entry with a replica count.
3. **Place.** The controller picks verified idle cards that satisfy `placement` and deploys a run spec: the
   image digest, the pinned GPU UUIDs, `front_door.port`, the env and volumes from `run`, and the
   `network.egress` allowlist.
4. **Start (STARTING, unpaid).** The controller has already pre-staged the image and `artifacts`. It verifies
   the artifacts on disk; waits for `health` to pass within `placement.max_load_s`; sends one `entry_canary`,
   picked at random, if any are declared, at `front_door.concurrency`. Only then does the front door open.
   **Pay begins at the first passing `health` probe after the canary.**
5. **Serve (LEASED, paid only while healthy).** Every ~60 s: a generic heartbeat (same card, our container on
   its digest with the container ID we started, card ours alone) plus this image's `health` probe, plus
   `profile` checks if declared.
6. **Exit (DRAINING, then CHECKING, both unpaid).** The controller sends `SIGTERM` and pay ends. The front
   door closes, the instance drains as `drain` says, the controller undeploys, runs the full hardware check,
   and the card returns to idle.

### Failure rules

| What fails | Consequence |
|---|---|
| The generic heartbeat (card swapped, container restarted or recreated, foreign process on the card) | card benched, pay withheld |
| `health` failing `failure_threshold` times in a row | replica replaced, standing drops |
| `health` not passing by `placement.max_load_s` | failed start: undeploy, then the full check; no pay, standing drops, **no bench** (slow is not caught) |
| The entry canary | failed start, as above |
| Drain not done by `drain.max_s` | failed drain: undeploy, then the full check; standing drops |
| `profile` misses | standing drops only; never pay |

## Artifacts

The controller stages every `artifacts[]` entry into the volume whose `mount` holds its `path` **before** the
container starts (the workload has no egress and never downloads), then verifies it on disk against `sha256`.

| `source` | What the controller writes at `path` |
|---|---|
| `hf://<org>/<repo>` | the repo at `revision` (`hf download --revision`) as a directory, plus a `.revision` marker file holding `revision` |
| `https://…` | one file, fetched as is (pin the URL itself, e.g. a Hugging Face `resolve/<commit>/<file>` URL) |
| `data:,<text>` | one file whose content is `<text>`: a small marker or config your runtime expects beside its weights |

**How `sha256` is computed** (the controller, `entrypoint.sh` and `python3 scripts/_manifest.py <path>` agree):

- A **file**: the sha256 of its bytes.
- A **directory**: the sha256 of `<relpath>\0<file sha256 hex>\n` for every file, in sorted walk order,
  **skipping top-level dotfiles and dot-directories** (`.revision`, `.gitattributes`, `.cache`). Those are markers
  and source metadata, not content, so the marker the controller writes never changes the digest. A dotfile deeper
  in the tree is content and is hashed.

Mount weights `read_only: true`. A runtime that insists on writing a file next to its weights gets that file as its
own artifact instead (the controller writes it; the workload never does).

## Entry canary shapes

The controller supports a few generic shapes. Each canary in the manifest picks one and fills it in:

| `type` | Input | Passes when |
|---|---|---|
| `http` | an HTTP request to the instance: `http: {method, path, port}` plus a JSON `body` | `pass.status` matches, and optionally `pass.contains` (substring), `pass.json_field` + `pass.equals`, or `pass.regex` |
| `command` | a command run inside the container: `command: [...]` | `pass.exit_code` (default 0), plus an optional `pass.contains` / `pass.regex` over its output |
| `fixture` | a tiny job on a fixture input: `input: <path in the image>` | `pass.output` exists and matches `pass.sha256` or `pass.schema` |

- **Shared across runtimes.** Any OpenAI-compatible runtime uses the same `http` canary. An image generator
  could check for a 512×512 PNG; an eval harness could run a `command` on a tiny fixture.
- **No determinism.** Answers must be unambiguous: arithmetic, exact recall, schema-valid JSON, output
  dimensions, a tool call's name and arguments. Do not assert on exact prose.
- **Written by you.** The manifest is yours. Guidance only: an image that serves tools makes a canary a tool
  call, because the failure it catches is silent otherwise (tools dropped before the chat template, the model
  never calls one, and the request still looks like a clean 200).
- **Optional; several allowed.** A manifest may list several canaries and the controller picks one at random
  each start. Without the field, the health probe alone carries the start.
- **A per-box smoke test, not anti-cheat.** Its value is catching faults the other checks miss before we route or
  pay: a card or driver that passes the hardware proof but faults on real work, a bad load, or a wrong
  runtime/driver combination on that specific box. Keep it cheap.

## Front door types

### `gateway-openai`

The instance serves `POST /v1/chat/completions` (streaming supported) and `GET /v1/models` on the declared port.

- **Reserve or 429.** When the instance has no free slot it returns **429 at once and never queues**, because the
  gateway promises "reserve or 429" to callers.
- **Timing fields** in `usage` (`ttft_ms`, `decode_tps`) are welcome but optional, never required.
- **Not required:** determinism, logprobs, a `/v1/score` route, a reference runtime.
- **Tool calling passes through.** The gateway forwards the OpenAI request body unchanged: `tools`,
  `tool_choice`, `parallel_tool_calls`, assistant `tool_calls` history, `role: "tool"` results, content parts.
  It enforces limits (the `max_tokens` cap, `n`, quota, rate) and never rebuilds the body or checks message
  shapes. **The runtime owns tool semantics**: it renders `tools` into the chat template and returns structured
  `tool_calls` with `finish_reason: "tool_calls"`. Any `request_schema` you attach to a route must stay loose
  enough not to reject these fields.
- **Capabilities come from the runtime, not the manifest.** Tools, vision, video, reasoning and the like vary
  per model, but there is no capabilities field. The gateway republishes each model's `/v1/models` from a
  healthy instance (with an operator override in the registry entry for anything a runtime gets wrong). A
  request a model can't serve is the runtime's 400 to return; the gateway doesn't gate on capabilities.
- **Remote media.** Workloads run with `network.egress: []`, so a runtime can't fetch an `image_url` or
  `video_url`. The gateway downloads http(s) media and inlines it as base64 before forwarding, or rejects the
  URL. Your runtime only ever sees inline data.
- **Output format** is between the caller and the runtime. Chat completions return whatever the runtime
  produces; a caller that wants the model's native markup renders its own prompt and calls `/v1/completions`.
  The gateway relays either unchanged.

### `http`

The instance serves the declared `routes` on `front_door.port`; the gateway passes requests through unchanged,
validating against a route's `request_schema` if it has one. There is no per-image public path field yet; that
is revisited with the first non-inference HTTP workload.

### `batch` (reserved)

Not built. The controller will hand the instance its input and output locations via env, and the job writes
results there. Its shape is defined with the first batch workload (our own eval-style jobs). A manifest may
declare `front_door: {type: batch}` today so the shape validates, but it will not be placed.

## Drain types

| `type` | What happens after `SIGTERM` |
|---|---|
| `requests` | the front door closes, in-flight requests finish, then the process exits; must complete within `max_s` |
| `checkpoint` | the job writes its state and exits; must complete within `max_s` |
| `kill` | no grace: the container is removed at once |

Size `max_s` from your worst case. For an inference image that is the longest single completion at the slowest
in-band decode speed (e.g. 4096 tokens at ~90 tok/s ≈ 45 s).

## The `profile` block

Healthy numbers recorded at qualification: for an inference image, decode tok/s (single and aggregate), a TTFT
band, VRAM held. In-lease, the controller compares what it observes against these to lower **standing** on
drift (the wrong card class, an overloaded or shared card, the serving port redirected). **It never affects
pay.** Pay is a flat rate per GPU type while the instance is healthy. Agent-shaped traffic is normal traffic:
`finish_reason: tool_calls`, short completions and large prompts must not read as misses, so TTFT bands scale
with prompt length.

## Example: the 27B inference image

```yaml
name: qwen3.8-27b-nvfp4
version: 1
description: Qwen3.8-27B (NVFP4) chat, one RTX 5090 per instance
runtime: sparkinfer
image: entrius/sparkinfer:19ef39ec2@sha256:<digest>
placement:
  gpu_types: {include: [RTX5090]}
  cards_per_instance: 1
  min_vram_gb: 30            # model ≈ 25 GB + KV headroom; confirm at qualification
  max_load_s: <measured>     # 27B cold load on a 5090
run:
  volumes: [{name: models, mount: /models, read_only: true}]
network:
  egress: []                 # none: the controller pre-stages the weights, so the workload never downloads
artifacts:
  - {path: /models/qwen3.8-27b-nvfp4, source: "hf://<org>/<repo>", revision: <commit>, sha256: <MODEL_SHA256>}
health:
  http: {path: /v1/models, port: 8080, expect_status: 200}
  interval_s: 60
  failure_threshold: 3
entry_canary:                # one picked at random each start; sent at front_door.concurrency (warmup)
  # Every canary here is a tool call: one call proves the weights decode AND that `tools` reach the chat
  # template on this box, so no random pick skips tool calling.
  - type: http
    http: {method: POST, path: /v1/chat/completions, port: 8080}
    body: {messages: [{role: user, content: "What is 17 × 23? Use the multiply tool."}], max_tokens: 64,
           enable_thinking: false,
           tools: [{type: function, function: {name: multiply, parameters: {type: object,
             properties: {a: {type: number}, b: {type: number}}, required: [a, b]}}}]}
    # one lookahead per fact: runtimes order tool_call keys differently (sparkinfer puts "arguments" before "name")
    pass: {status: 200, regex: '(?=[\s\S]*"finish_reason":\s*"tool_calls")(?=[\s\S]*"name":\s*"multiply")(?=[\s\S]*\\"[ab]\\":\s*17\b)(?=[\s\S]*\\"[ab]\\":\s*23\b)'}
  - type: http
    http: {method: POST, path: /v1/chat/completions, port: 8080}
    body: {messages: [{role: user, content: "What's the weather in Paris right now? Use the tool."}], max_tokens: 64,
           enable_thinking: false,
           tools: [{type: function, function: {name: get_weather, parameters: {type: object,
             properties: {city: {type: string}}, required: [city]}}}]}
    pass: {status: 200, regex: '(?=[\s\S]*"finish_reason":\s*"tool_calls")(?=[\s\S]*"name":\s*"get_weather")(?=[\s\S]*\\"city\\":\s*\\"Paris)'}
profile:                     # health and standing only, never pay
  decode_tps_single: 99
  decode_tps_aggregate: 338
  ttft_ms_band: <measured>   # scaled by prompt length: agent turns carry 2k+ tokens of tool schema plus history
  vram_gb: 25
front_door:
  type: gateway-openai
  port: 8080
  concurrency: <measured>    # measured at agent-sized prompts (30–60k tokens incl. a tool schema), not the canary
  routes:
    - {path: /v1/chat/completions, method: POST, stream: true}
    - {path: /v1/models, method: GET}
drain:
  type: requests
  max_s: 60                  # worst case 4096 tokens at ~90 tok/s ≈ 45 s
```

The runnable version of this example, with placeholders filled so it passes the schema, is this repo's
[`manifest.yaml`](manifest.yaml).

## Example: a batch job (hypothetical, for shape only)

```yaml
name: eval-harness
version: 1
description: Runs an eval suite on a fixture set (shape only; batch front door not defined yet)
runtime: custom
image: entrius/eval-harness@sha256:<digest>
placement: {gpu_types: {include: [RTX5090]}, cards_per_instance: 1, min_vram_gb: 28, max_load_s: 300}
health:
  command: ["test", "-f", "/work/alive"]
  interval_s: 60
  failure_threshold: 5
entry_canary:
  - type: fixture
    input: /fixtures/tiny-eval.jsonl
    pass: {output: /work/out/tiny-eval.json, schema: eval-result-v1}
front_door:
  type: batch                # reserved; defined with the first batch workload
drain:
  type: checkpoint
  max_s: 120
```

## What the controller guarantees back to every image

- Only its pinned cards are visible to it, and nothing else runs on them.
- The ports, env and volumes its manifest asked for.
- Warning before a lease ends: `SIGTERM`, then the declared drain window, then removal.
