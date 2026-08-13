from __future__ import annotations

import copy
import unittest

import palintropos


class HarnessTests(unittest.TestCase):
    def test_effects_apply_lifo_and_are_idempotent(self) -> None:
        events: list[str] = []
        harness = palintropos.Harness()

        def start(ctx: palintropos.Context):
            first = ctx.effect(
                lambda: (events.append("apply:first"), lambda: events.append("undo:first"))[1]
            )
            second = ctx.effect(
                lambda: (events.append("apply:second"), lambda: events.append("undo:second"))[1]
            )
            second()
            second()
            self.assertTrue(callable(first))

        unmount = harness.mount("effects", (), start)
        unmount()
        unmount()
        self.assertEqual(
            events,
            ["apply:first", "apply:second", "undo:second", "undo:first"],
        )

    def test_consumer_waits_then_activates(self) -> None:
        harness = palintropos.Harness()
        seen: list[str] = []

        harness.mount(
            "consumer",
            ("message",),
            lambda ctx: seen.append(ctx.get("message")),
        )
        self.assertEqual(harness.status()["consumer"]["state"], "waiting")
        self.assertEqual(harness.status()["consumer"]["missing"], ("message",))

        harness.mount(
            "provider",
            (),
            lambda ctx: ctx.provide("message", "ready") and None,
        )
        self.assertEqual(harness.status()["consumer"]["state"], "active")
        self.assertEqual(seen, ["ready"])

    def test_provider_unmount_stops_consumer_first(self) -> None:
        harness = palintropos.Harness()
        events: list[str] = []

        def consume(ctx: palintropos.Context):
            ctx.get("service")
            events.append("consumer:start")
            return lambda: events.append("consumer:stop")

        def provide(ctx: palintropos.Context):
            events.append("provider:start")
            ctx.provide("service", object())
            return lambda: events.append("provider:stop")

        harness.mount("consumer", ("service",), consume)
        unmount_provider = harness.mount("provider", (), provide)
        unmount_provider()
        self.assertEqual(
            events,
            [
                "provider:start",
                "consumer:start",
                "consumer:stop",
                "provider:stop",
            ],
        )
        self.assertEqual(harness.status()["consumer"]["state"], "waiting")

    def test_teardown_can_read_committed_dependency_snapshot(self) -> None:
        harness = palintropos.Harness()
        seen: list[str] = []

        def consume(ctx: palintropos.Context):
            self.assertEqual(ctx.get("name"), "snapshot")
            return lambda: seen.append(ctx.get("name"))

        harness.mount("consumer", ("name",), consume)
        unmount_provider = harness.mount(
            "provider",
            (),
            lambda ctx: ctx.provide("name", "snapshot") and None,
        )
        unmount_provider()
        self.assertEqual(seen, ["snapshot"])
        with self.assertRaises(palintropos.MissingServiceError):
            harness.get("name")

    def test_provider_replacement_reactivates_consumer(self) -> None:
        harness = palintropos.Harness()
        events: list[str] = []

        def consume(ctx: palintropos.Context):
            version = ctx.get("version")
            events.append(f"start:{version}")
            return lambda: events.append(f"stop:{ctx.get('version')}")

        harness.mount("consumer", ("version",), consume)
        unmount_v1 = harness.mount(
            "provider-v1",
            (),
            lambda ctx: ctx.provide("version", "v1") and None,
        )
        unmount_v1()
        harness.mount(
            "provider-v2",
            (),
            lambda ctx: ctx.provide("version", "v2") and None,
        )
        self.assertEqual(events, ["start:v1", "stop:v1", "start:v2"])
        self.assertEqual(harness.get("version"), "v2")

    def test_activation_failure_rolls_back_partial_effects(self) -> None:
        harness = palintropos.Harness()
        events: list[str] = []

        def broken(ctx: palintropos.Context):
            ctx.effect(
                lambda: (events.append("apply"), lambda: events.append("undo"))[1]
            )
            ctx.provide("partial", 1)
            raise RuntimeError("boom")

        harness.mount("broken", (), broken)
        info = harness.status()["broken"]
        self.assertEqual(info["state"], "failed")
        self.assertIn("boom", info["error"])
        self.assertEqual(events, ["apply", "undo"])
        with self.assertRaises(palintropos.MissingServiceError):
            harness.get("partial")

    def test_duplicate_service_fails_new_provider_without_replacement(self) -> None:
        harness = palintropos.Harness()
        original = object()
        harness.mount(
            "original",
            (),
            lambda ctx: ctx.provide("singleton", original) and None,
        )
        harness.mount(
            "duplicate",
            (),
            lambda ctx: ctx.provide("singleton", object()) and None,
        )
        self.assertIs(harness.get("singleton"), original)
        info = harness.status()["duplicate"]
        self.assertEqual(info["state"], "failed")
        self.assertIn("DuplicateServiceError", info["error"])

    def test_undeclared_access_fails_activation(self) -> None:
        harness = palintropos.Harness()
        harness.mount("bad-reader", (), lambda ctx: ctx.get("secret"))
        info = harness.status()["bad-reader"]
        self.assertEqual(info["state"], "failed")
        self.assertIn("UndeclaredDependencyError", info["error"])

    def test_all_cleanup_errors_are_grouped(self) -> None:
        harness = palintropos.Harness()
        events: list[str] = []

        def bad_undo(label: str):
            def undo() -> None:
                events.append(label)
                raise RuntimeError(label)

            return undo

        def start(ctx: palintropos.Context) -> None:
            ctx.effect(lambda: bad_undo("first"))
            ctx.effect(lambda: bad_undo("second"))

        unmount = harness.mount("cleanup-errors", (), start)
        with self.assertRaises(ExceptionGroup) as caught:
            unmount()
        self.assertEqual(events, ["second", "first"])
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertEqual(harness.status(), {})

    def test_close_is_idempotent_and_restores_empty_context(self) -> None:
        harness = palintropos.Harness()
        harness.mount(
            "provider", (), lambda ctx: ctx.provide("value", 42) and None
        )
        harness.close()
        harness.close()
        self.assertEqual(harness.status(), {})
        with self.assertRaises(palintropos.MissingServiceError):
            harness.get("value")
        with self.assertRaises(palintropos.LifecycleError):
            harness.mount("late", (), lambda _ctx: None)


class AgentLoopTests(unittest.TestCase):
    def test_fake_llm_drives_offline_tool_call_loop(self) -> None:
        requests: list[tuple[list[dict], list[dict]]] = []

        def fake_llm(messages, tools):
            requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
            if len(requests) == 1:
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "reverse",
                            "input": {"text": "stressed"},
                        }
                    ]
                }
            return {"content": [{"type": "text", "text": "desserts"}]}

        result = palintropos.run_agent(
            fake_llm,
            lambda text: text[::-1],
            "Call reverse on stressed",
        )
        self.assertEqual(result.text, "desserts")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.tool_calls[0]["result"], "desserts")
        self.assertEqual(requests[1][0][-1]["role"], "user")
        tool_result = requests[1][0][-1]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "call-1")
        self.assertEqual(tool_result["content"], "desserts")
        self.assertEqual(requests[0][1][0]["name"], "reverse")


if __name__ == "__main__":
    unittest.main()
