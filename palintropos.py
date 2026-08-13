#!/usr/bin/env python3
"""Palintropos: reversible effects and reactive dependencies in one file.

Python 3.11+ only.  The core has no third-party dependencies; the optional
``ask`` command uses urllib to call an Anthropic-compatible Messages API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


Undo = Callable[[], Any]
Start = Callable[["Context"], Undo | None]
LLM = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]


class PalintroposError(RuntimeError):
    """Base class for errors raised by the tiny runtime."""


class DuplicatePluginError(PalintroposError):
    """A mounted plugin already uses this name."""


class DuplicateServiceError(PalintroposError):
    """A service key already has a provider."""


class MissingServiceError(PalintroposError):
    """No active provider currently supplies this service."""


class UndeclaredDependencyError(PalintroposError):
    """A plugin tried to read a service it did not declare."""


class LifecycleError(PalintroposError):
    """An operation is invalid in the current lifecycle phase."""


@dataclass(slots=True)
class _Service:
    provider: "_Plugin"
    value: Any


@dataclass(slots=True, eq=False)
class _Plugin:
    name: str
    requires: tuple[str, ...]
    start: Start
    state: str = "waiting"
    effects: list[Undo] = field(default_factory=list)
    snapshot: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    snapshot_providers: Mapping[str, "_Plugin"] = field(
        default_factory=lambda: MappingProxyType({})
    )
    provides: set[str] = field(default_factory=set)
    context: "Context | None" = None
    error: Exception | None = None


def _once(undo: Undo) -> Undo:
    called = False

    def idempotent_undo() -> Any:
        nonlocal called
        if called:
            return None
        called = True
        return undo()

    return idempotent_undo


class Context:
    """The capability view and effect accumulator for one plugin activation."""

    __slots__ = ("_harness", "_plugin")

    def __init__(self, harness: "Harness", plugin: _Plugin) -> None:
        self._harness = harness
        self._plugin = plugin

    def _assert_registering(self) -> None:
        plugin = self._plugin
        if plugin.context is not self:
            raise LifecycleError("plugin context is no longer live")
        if plugin.state != "active" and plugin not in self._harness._starting:
            raise LifecycleError("effects can only be registered while starting or active")

    def effect(self, apply: Callable[[], Undo]) -> Undo:
        """Apply a side effect now and track its idempotent inverse."""

        self._assert_registering()
        undo = apply()
        if not callable(undo):
            raise TypeError("effect apply() must return an undo callable")
        wrapped = _once(undo)
        self._plugin.effects.append(wrapped)
        return wrapped

    def provide(self, key: str, value: Any) -> Undo:
        """Provide a capability as a reversible effect."""

        if not isinstance(key, str) or not key:
            raise ValueError("service key must be a non-empty string")

        def apply() -> Undo:
            self._harness._install_service(self._plugin, key, value)
            return lambda: self._harness._remove_service(self._plugin, key)

        undo = self.effect(apply)
        self._harness._reconcile()
        return undo

    def get(self, key: str) -> Any:
        """Read only a declared dependency from this activation's snapshot."""

        if key not in self._plugin.requires:
            raise UndeclaredDependencyError(
                f"plugin {self._plugin.name!r} did not declare dependency {key!r}"
            )
        if key not in self._plugin.snapshot:
            raise MissingServiceError(
                f"dependency {key!r} is not committed for this activation"
            )
        return self._plugin.snapshot[key]


class Harness:
    """Synchronous plugin runtime with reversible effects and reactive services."""

    def __init__(self) -> None:
        self._plugins: dict[str, _Plugin] = {}
        self._services: dict[str, _Service] = {}
        self._starting: set[_Plugin] = set()
        self._reconciling = False
        self._reconcile_requested = False
        self._closing = False
        self._closed = False

    def mount(
        self,
        name: str,
        requires: Iterable[str],
        start: Start,
    ) -> Undo:
        """Declare a plugin and return an idempotent unmount function."""

        if self._closed or self._closing:
            raise LifecycleError("harness is closed")
        if not isinstance(name, str) or not name:
            raise ValueError("plugin name must be a non-empty string")
        if name in self._plugins:
            raise DuplicatePluginError(f"plugin {name!r} is already mounted")
        if not callable(start):
            raise TypeError("start must be callable")

        required = tuple(requires)
        if any(not isinstance(key, str) or not key for key in required):
            raise ValueError("dependencies must be non-empty strings")
        if len(set(required)) != len(required):
            raise ValueError("dependencies must not contain duplicates")

        plugin = _Plugin(name=name, requires=required, start=start)
        self._plugins[name] = plugin
        self._reconcile()

        done = False

        def unmount() -> None:
            nonlocal done
            if done:
                return
            done = True
            self._unmount(plugin)

        return unmount

    def get(self, key: str) -> Any:
        """Read a capability supplied by an active provider."""

        service = self._services.get(key)
        if service is None or service.provider.state != "active":
            raise MissingServiceError(f"no active provider for service {key!r}")
        return service.value

    def status(self) -> dict[str, dict[str, Any]]:
        """Return a JSON-friendly snapshot of all mounted plugins."""

        result: dict[str, dict[str, Any]] = {}
        for plugin in self._plugins.values():
            missing = tuple(
                key for key in plugin.requires if not self._service_available(key)
            )
            result[plugin.name] = {
                "state": plugin.state,
                "requires": plugin.requires,
                "provides": tuple(sorted(plugin.provides)),
                "missing": missing,
                "error": (
                    None
                    if plugin.error is None
                    else f"{type(plugin.error).__name__}: {plugin.error}"
                ),
            }
        return result

    def close(self) -> None:
        """Unmount everything in reverse mount order and aggregate cleanup errors."""

        if self._closed:
            return
        self._closing = True
        errors: list[Exception] = []
        try:
            for plugin in reversed(list(self._plugins.values())):
                if self._plugins.get(plugin.name) is not plugin:
                    continue
                self._stop(plugin, next_state="waiting", errors=errors)
                self._plugins.pop(plugin.name, None)
            self._services.clear()
            self._closed = True
        finally:
            self._closing = False
        if errors:
            raise ExceptionGroup("errors while closing harness", errors)

    def _service_available(self, key: str) -> bool:
        service = self._services.get(key)
        return service is not None and service.provider.state == "active"

    def _dependencies_ready(self, plugin: _Plugin) -> bool:
        return all(self._service_available(key) for key in plugin.requires)

    def _snapshot_dependencies(
        self, plugin: _Plugin
    ) -> tuple[Mapping[str, Any], Mapping[str, _Plugin]]:
        values: dict[str, Any] = {}
        providers: dict[str, _Plugin] = {}
        for key in plugin.requires:
            service = self._services[key]
            values[key] = service.value
            providers[key] = service.provider
        return MappingProxyType(values), MappingProxyType(providers)

    def _reconcile(self) -> None:
        if self._closed or self._closing:
            return
        if self._reconciling:
            self._reconcile_requested = True
            return

        self._reconciling = True
        try:
            while True:
                self._reconcile_requested = False
                progressed = False
                for plugin in list(self._plugins.values()):
                    if plugin.state != "waiting" or plugin in self._starting:
                        continue
                    if not self._dependencies_ready(plugin):
                        continue
                    self._activate(plugin)
                    progressed = True
                if not progressed and not self._reconcile_requested:
                    break
        finally:
            self._reconciling = False

    def _activate(self, plugin: _Plugin) -> None:
        plugin.snapshot, plugin.snapshot_providers = self._snapshot_dependencies(plugin)
        plugin.effects.clear()
        plugin.error = None
        context = Context(self, plugin)
        plugin.context = context
        self._starting.add(plugin)
        try:
            cleanup = plugin.start(context)
            if cleanup is not None:
                if not callable(cleanup):
                    raise TypeError("plugin start() must return a cleanup callable or None")
                plugin.effects.append(_once(cleanup))
        except Exception as error:
            self._starting.remove(plugin)
            self._fail_activation(plugin, error)
            return
        self._starting.remove(plugin)

        if not self._snapshot_is_current(plugin):
            self._pause_after_race(plugin)
            return

        plugin.state = "active"
        self._reconcile_requested = True

    def _snapshot_is_current(self, plugin: _Plugin) -> bool:
        for key, provider in plugin.snapshot_providers.items():
            service = self._services.get(key)
            if (
                service is None
                or service.provider is not provider
                or provider.state != "active"
            ):
                return False
        return True

    def _pause_after_race(self, plugin: _Plugin) -> None:
        plugin.state = "stopping"
        self._withdraw_all(plugin)
        errors: list[Exception] = []
        self._run_effects(plugin, errors)
        self._clear_activation(plugin)
        if errors:
            plugin.error = ExceptionGroup(
                f"cleanup failed for plugin {plugin.name!r}", errors
            )
            plugin.state = "failed"
        else:
            plugin.state = "waiting"

    def _fail_activation(self, plugin: _Plugin, error: Exception) -> None:
        plugin.state = "stopping"
        self._withdraw_all(plugin)
        cleanup_errors: list[Exception] = []
        self._run_effects(plugin, cleanup_errors)
        self._clear_activation(plugin)
        if cleanup_errors:
            plugin.error = ExceptionGroup(
                f"activation and rollback failed for plugin {plugin.name!r}",
                [error, *cleanup_errors],
            )
        else:
            plugin.error = error
        plugin.state = "failed"

    def _clear_activation(self, plugin: _Plugin) -> None:
        plugin.effects.clear()
        plugin.snapshot = MappingProxyType({})
        plugin.snapshot_providers = MappingProxyType({})
        plugin.context = None

    def _install_service(self, plugin: _Plugin, key: str, value: Any) -> None:
        current = self._services.get(key)
        if current is not None:
            raise DuplicateServiceError(
                f"service {key!r} is already provided by {current.provider.name!r}"
            )
        self._services[key] = _Service(provider=plugin, value=value)
        plugin.provides.add(key)

    def _remove_service(self, plugin: _Plugin, key: str) -> None:
        current = self._services.get(key)
        if current is None or current.provider is not plugin:
            plugin.provides.discard(key)
            return

        del self._services[key]
        plugin.provides.discard(key)
        errors: list[Exception] = []
        if plugin.state == "active":
            for consumer in self._consumers_of(plugin, keys={key}):
                self._stop(consumer, next_state="waiting", errors=errors)
        self._reconcile()
        if errors:
            raise ExceptionGroup(
                f"errors while withdrawing service {key!r}", errors
            )

    def _withdraw_all(self, plugin: _Plugin) -> set[str]:
        removed: set[str] = set()
        for key in tuple(plugin.provides):
            current = self._services.get(key)
            if current is not None and current.provider is plugin:
                del self._services[key]
                removed.add(key)
            plugin.provides.discard(key)
        return removed

    def _consumers_of(
        self, provider: _Plugin, keys: set[str] | None = None
    ) -> list[_Plugin]:
        consumers: list[_Plugin] = []
        for plugin in reversed(list(self._plugins.values())):
            if plugin.state != "active":
                continue
            if any(
                dependency_provider is provider and (keys is None or key in keys)
                for key, dependency_provider in plugin.snapshot_providers.items()
            ):
                consumers.append(plugin)
        return consumers

    def _stop(
        self,
        plugin: _Plugin,
        *,
        next_state: str,
        errors: list[Exception],
    ) -> None:
        if plugin.state != "active":
            return

        plugin.state = "stopping"
        removed = self._withdraw_all(plugin)
        for consumer in self._consumers_of(plugin, keys=removed or None):
            self._stop(consumer, next_state="waiting", errors=errors)

        before = len(errors)
        self._run_effects(plugin, errors)
        own_errors = errors[before:]
        self._clear_activation(plugin)
        if own_errors:
            plugin.error = ExceptionGroup(
                f"cleanup failed for plugin {plugin.name!r}", list(own_errors)
            )
            plugin.state = "failed"
        else:
            plugin.error = None
            plugin.state = next_state

    @staticmethod
    def _run_effects(plugin: _Plugin, errors: list[Exception]) -> None:
        for undo in reversed(plugin.effects):
            try:
                undo()
            except Exception as error:
                errors.append(error)

    def _unmount(self, plugin: _Plugin) -> None:
        if self._plugins.get(plugin.name) is not plugin:
            return
        errors: list[Exception] = []
        self._stop(plugin, next_state="waiting", errors=errors)
        self._plugins.pop(plugin.name, None)
        if not self._closing:
            self._reconcile()
        if errors:
            raise ExceptionGroup(
                f"errors while unmounting plugin {plugin.name!r}", errors
            )


@dataclass(frozen=True, slots=True)
class AgentResult:
    text: str
    tool_calls: tuple[dict[str, Any], ...]
    steps: int


REVERSE_TOOL = {
    "name": "reverse",
    "description": "Reverse the characters in a string.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The exact string to reverse.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


def run_agent(
    llm: LLM,
    reverse: Callable[[str], str],
    prompt: str,
    *,
    max_steps: int = 4,
    emit: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run one user turn with a bounded Anthropic-style tool loop."""

    if max_steps < 1:
        raise ValueError("max_steps must be at least 1")
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    calls: list[dict[str, Any]] = []

    for step in range(1, max_steps + 1):
        response = llm(messages, [REVERSE_TOOL])
        content = response.get("content")
        if not isinstance(content, list):
            raise RuntimeError("LLM response content must be a list")

        tool_uses = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not tool_uses:
            text = "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if emit is not None:
                emit(f"assistant: {text}")
            return AgentResult(text=text, tool_calls=tuple(calls), steps=step)

        messages.append({"role": "assistant", "content": content})
        results: list[dict[str, Any]] = []
        for block in tool_uses:
            call_id = block.get("id")
            name = block.get("name")
            arguments = block.get("input")
            if not isinstance(call_id, str) or not call_id:
                raise RuntimeError("tool_use block is missing a string id")
            if name != "reverse":
                raise RuntimeError(f"unsupported tool requested: {name!r}")
            if not isinstance(arguments, dict) or not isinstance(
                arguments.get("text"), str
            ):
                raise RuntimeError("reverse tool requires a string 'text' argument")
            source = arguments["text"]
            value = reverse(source)
            call = {
                "id": call_id,
                "name": name,
                "input": {"text": source},
                "result": value,
            }
            calls.append(call)
            if emit is not None:
                emit(f"tool_use: reverse({source!r})")
                emit(f"tool_result: {value!r}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": value,
                }
            )
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"agent exceeded the {max_steps}-step tool-call limit")


def make_anthropic_client(
    model: str,
    *,
    max_tokens: int = 1024,
    timeout: float = 120.0,
) -> LLM:
    """Create a tiny stdlib client without reading config files or logging secrets."""

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "ANTHROPIC_AUTH_TOKEN"
    )
    if not api_key:
        raise RuntimeError(
            "set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN in the process environment"
        )
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if not base_url:
        raise RuntimeError("set ANTHROPIC_BASE_URL in the process environment")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1/messages"):
        messages_url = base_url
    elif base_url.endswith("/v1"):
        messages_url = base_url + "/messages"
    else:
        messages_url = base_url + "/v1/messages"

    system = (
        "You are a tool-using assistant. When asked to reverse text, you must "
        "call the reverse tool and then report its result."
    )

    def call(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages,
                "tools": tools,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            messages_url,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "user-agent": "palintropos/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Messages API returned HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Messages API request failed: {error.reason}") from error
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError("Messages API returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise RuntimeError("Messages API returned a non-object response")
        return decoded

    return call


def _compact_status(harness: Harness) -> str:
    states = {
        name: {
            "state": info["state"],
            "provides": info["provides"],
            "missing": info["missing"],
        }
        for name, info in harness.status().items()
    }
    return json.dumps(states, ensure_ascii=False)


def demo() -> None:
    """Show waiting, activation, replacement, restart, and exact recovery."""

    harness = Harness()
    print("Palintropos demo: effect + provide/get + plugin")

    def start_consumer(ctx: Context) -> Undo:
        greet = ctx.get("greeter")
        print(f"  activate consumer -> {greet('world')}")

        def stop() -> None:
            still_available = ctx.get("greeter")("teardown")
            print(f"  deactivate consumer -> snapshot says {still_available}")

        return stop

    harness.mount("consumer", ("greeter",), start_consumer)
    print(f"1 waiting: {_compact_status(harness)}")

    def provider(version: str) -> Start:
        def start(ctx: Context) -> Undo:
            print(f"  activate provider-{version}")

            def acquire(label: str) -> Undo:
                print(f"    effect +{label}")
                return lambda: print(f"    undo   -{label}")

            ctx.effect(lambda: acquire(f"{version}:first"))
            ctx.effect(lambda: acquire(f"{version}:second"))
            ctx.provide("greeter", lambda who: f"{version} hello, {who}")
            return lambda: print(f"  deactivate provider-{version}")

        return start

    unmount_v1 = harness.mount("provider-v1", (), provider("v1"))
    print(f"2 active:  {_compact_status(harness)}")

    print("3 replace v1 -> v2:")
    unmount_v1()
    print(f"  after withdraw: {_compact_status(harness)}")
    harness.mount("provider-v2", (), provider("v2"))
    print(f"  after replace:  {_compact_status(harness)}")

    print("4 close (consumer stops before provider cleanup; effects undo LIFO):")
    harness.close()
    print(f"5 restored: {_compact_status(harness)}")


def ask(prompt: str, *, model: str, max_steps: int) -> AgentResult:
    """Compose LLM, reverse tool, and one-turn agent as three plugins."""

    harness = Harness()
    outcome: dict[str, Any] = {}

    def start_llm(ctx: Context) -> None:
        ctx.provide("llm", make_anthropic_client(model))
        return None

    def start_reverse(ctx: Context) -> None:
        ctx.provide("reverse", lambda text: text[::-1])
        return None

    def start_agent(ctx: Context) -> None:
        try:
            outcome["result"] = run_agent(
                ctx.get("llm"),
                ctx.get("reverse"),
                prompt,
                max_steps=max_steps,
                emit=print,
            )
        except Exception as error:
            outcome["error"] = error
            raise
        return None

    try:
        harness.mount("llm.anthropic", (), start_llm)
        harness.mount("tool.reverse", (), start_reverse)
        harness.mount("agent.one-turn", ("llm", "reverse"), start_agent)
        if "error" in outcome:
            raise outcome["error"]
        result = outcome.get("result")
        if not isinstance(result, AgentResult):
            raise RuntimeError(f"agent did not complete: {harness.status()}")
        return result
    finally:
        harness.close()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A tiny executable model of reversible effects and reactive dependencies."
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("demo", help="run the offline lifecycle demo")
    ask_parser = subparsers.add_parser(
        "ask", help="run one real Anthropic-compatible tool-call turn"
    )
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--model", default="glm-5.2")
    ask_parser.add_argument("--max-steps", type=_positive_int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in (None, "demo"):
            demo()
        elif args.command == "ask":
            result = ask(args.prompt, model=args.model, max_steps=args.max_steps)
            if not result.tool_calls:
                print("error: model completed without calling reverse", file=sys.stderr)
                return 2
        else:  # pragma: no cover - argparse owns command validation.
            raise AssertionError(args.command)
    except (PalintroposError, RuntimeError, ValueError, ExceptionGroup) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
