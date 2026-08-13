# Palintropos

[简体中文](README.zh-CN.md)

**A tiny executable model of reversible effects and reactive dependencies.**

Palintropos (Greek-derived: “turning back”) reduces the design behind DeepSeek Harness and Cordis to three atoms:

```text
effect(do -> undo)
provide / get
plugin(requires -> activate / deactivate)
```

It is a zero-dependency, single-core-file teaching project for Python 3.11+. Run it directly from a fresh clone:

```bash
python3 palintropos.py
```

Palintropos is an independent educational implementation. It is **not** an official DeepSeek project, does not copy upstream source code, and does not aim to be API-compatible with Cordis.

## Why the name

Every action in this runtime carries a route back. A plugin may change shared state while active, but its recorded inverses return that state to where it began. “Palintropos” names that turn back.

## The three atoms

### 1. `effect(do -> undo)`

`Context.effect(apply)` runs `apply()` immediately. `apply()` must return an undo callable. The runtime makes that undo idempotent and executes all tracked undos in LIFO order.

```python
def start(ctx):
    def apply():
        resource = acquire()
        return lambda: release(resource)

    ctx.effect(apply)
```

If activation fails after earlier effects succeeded, the completed effects are still rolled back. Cleanup continues after individual undo failures and reports all failures in an `ExceptionGroup`.

### 2. `provide / get`

`Context.provide(key, value)` is itself an effect. It publishes a capability and records the inverse that withdraws it.

Consumers do not import a provider. They declare keys in `requires` and read only those keys through `Context.get(key)`. Each activation receives an immutable dependency snapshot. That snapshot remains readable during teardown, even after the provider has stopped publishing the capability globally.

`Harness.get(key)` is different: it reads the live global service store and succeeds only while an active provider exists.

### 3. `plugin(requires -> activate / deactivate)`

```python
harness = Harness()

def start_consumer(ctx):
    begin(ctx.get("clock"))

def start_clock(ctx):
    ctx.provide("clock", monotonic)

unmount_consumer = harness.mount(
    "consumer",
    requires=("clock",),
    start=start_consumer,
)

unmount_provider = harness.mount(
    "clock-provider",
    requires=(),
    start=start_clock,
)
```

`Harness.mount(name, requires, start)` returns an idempotent unmount function. `start(ctx)` may return one cleanup callable or `None`. A plugin has exactly four visible states:

- `waiting`: at least one dependency is missing.
- `active`: activation completed and any services it provides are committed.
- `stopping`: consumers and effects are being withdrawn.
- `failed`: activation or teardown failed; details are in `status()`.

## Lifecycle trace

The default demo mounts the consumer before its provider, replaces that provider, restarts the consumer against a new snapshot, and closes the runtime:

```text
consumer: waiting for greeter
provider-v1: active -> consumer: active
withdraw provider-v1
  hide greeter from the live store
  consumer: stopping -> waiting
  provider-v1: undo second, undo first
provider-v2: active -> consumer: active again
close
  consumer stops before provider-v2 cleanup
  all effects unwind in LIFO order
status: {}
```

The ordering is deliberate. When a provider is unmounted, Palintropos first makes its services unavailable to new consumers, recursively stops existing consumers while their committed snapshots are still readable, and only then runs the provider's own undo stack.

Run the same trace explicitly with:

```bash
python3 palintropos.py demo
```

## Public core API

The intentionally small public surface is:

```python
Harness.mount(name, requires, start) -> unmount
Harness.get(key)
Harness.status()
Harness.close()

Context.effect(apply) -> undo
Context.provide(key, value) -> undo
Context.get(key)
```

Activation errors are retained as `failed` state instead of escaping `mount()`, so callers can inspect the whole composition. Cleanup errors escape `unmount()` or `close()` as `ExceptionGroup` only after every relevant cleanup has run.

Service keys are unique. A second provider fails activation without replacing the first. A plugin that calls `Context.get()` for an undeclared key also fails activation.

## Optional real tool-call loop

The `ask` command composes the same three atoms into three plugins:

1. an Anthropic-compatible Messages API client providing `llm`;
2. a pure `reverse` function providing `reverse`;
3. one bounded user-turn agent requiring both capabilities.

Set the token through the process environment as either `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, and set `ANTHROPIC_BASE_URL`. For GLM-5.2, the official compatible base URL is:

```bash
export ANTHROPIC_BASE_URL='https://open.bigmodel.cn/api/anthropic'
python3 palintropos.py ask --model glm-5.2 'Call reverse on stressed'
```

Expected shape:

```text
tool_use: reverse('stressed')
tool_result: 'desserts'
assistant: ...desserts...
```

The client uses only `urllib`. It never accepts an API-key command-line argument, reads Claude configuration files, prints request headers, or writes request logs. Tool execution is limited to the pure `reverse` function, and the loop has a finite step bound.

GLM compatibility is documented in the official [Zhipu Claude API compatibility guide](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction).

## Relationship to DeepSeek Harness and Cordis

This project is based on ideas, not copied implementation, from fixed public snapshots:

- [DeepSeek Harness `47f9438`](https://github.com/deepseek-ai/deepseek-harness/tree/47f943859bef60e4160492346772ded9b24f765a)
- [*A Programming Paradigm for Spatiotemporal Composability* `948a07b`](https://github.com/cordiverse/paper/tree/948a07b369c62adb3b12e102458be5c18dfb69b9)
- [Cordis `8cc9e33`](https://github.com/cordiverse/cordis/tree/8cc9e33fab69e2d0476d126baaf2acb24e6a6ab4)

| Palintropos atom | Upstream idea retained | Production machinery omitted |
| --- | --- | --- |
| `effect(do -> undo)` | Cordis revertible effects and plugin-owned disposal | async effects, nested fibers, events, diagnostics |
| `provide / get` | services by stable key and reactive coeffects | typing/reflection, isolation, interception, optional injection |
| `mount(requires, start)` | dependency-driven plugin activation and restart | YAML loader, config reconciliation, HMR, package graph |
| `ask` composition | DeepSeek Harness's “everything is a plugin” architecture | full agent loop, persistence, tool registries, UI and policy layers |

The paper distinguishes temporal composability (reversing a component's effects) from spatial composability (declaring and reactively resolving dependencies). Palintropos makes only that smallest executable intersection concrete.

## Trust boundary

`mount`, `unmount`, and `status` demonstrate creator-style runtime modification as a human-callable Python API. They are not exposed as model tools. Palintropos does not evaluate model-written Python, run shell commands, or claim to sandbox code.

That omission is intentional: DeepSeek Harness's own dynamic-package documentation says its VM is not a security boundary and asks operators to treat model-generated components like shell access ([fixed-source trust stance](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/extensions/cordis-host-runner/README.md#trust-stance)).

## Deliberately out of scope

There is no TUI or Web UI, shell/editor tool, persistence, async lifecycle, HMR, sandbox, multi-agent runtime, plugin discovery, package installer, model-authored runtime code, SDK, build system, PyPI package, or GitHub release in v1.

This small runtime also does not promise independence for arbitrary interleaved global mutations. Its guarantee is structural: effects owned by one activation are undone in LIFO order, and declared dependency edges order consumers before providers during teardown.

## Tests

Everything in CI is offline and uses the standard library:

```bash
python3 -m py_compile palintropos.py test_palintropos.py
python3 -m unittest -v
python3 palintropos.py demo
```

The tests cover effect rollback, idempotence, dependency waiting, consumer-before-provider teardown, teardown snapshots, provider replacement, failed-activation rollback, duplicate services, undeclared access, grouped cleanup errors, exact context recovery, and a fake-LLM tool-call loop.

## License

[MIT](LICENSE)
