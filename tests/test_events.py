"""Tests for spec_runner.events module."""

import asyncio

from spec_runner.events import EventBus, TaskEvent


class TestTaskEvent:
    def test_creates_with_defaults(self):
        event = TaskEvent(task_id="TASK-001", event_type="output_line", data="hello")
        assert event.task_id == "TASK-001"
        assert event.event_type == "output_line"
        assert event.data == "hello"
        assert event.timestamp > 0


class TestEventBus:
    def test_publish_and_drain_recent(self):
        bus = EventBus()
        event = TaskEvent(task_id="T1", event_type="output_line", data="line 1")
        bus.publish(event)
        events = bus.drain_recent()
        assert len(events) == 1
        assert events[0].data == "line 1"

    def test_drain_clears_buffer(self):
        bus = EventBus()
        bus.publish(TaskEvent(task_id="T1", event_type="info", data="a"))
        bus.drain_recent()
        events = bus.drain_recent()
        assert events == []

    def test_max_recent_limits_buffer(self):
        bus = EventBus(max_recent=5)
        for i in range(10):
            bus.publish(TaskEvent(task_id="T1", event_type="output_line", data=f"line {i}"))
        events = bus.drain_recent()
        assert len(events) == 5
        assert events[0].data == "line 5"  # oldest kept

    def test_async_subscriber(self):
        bus = EventBus()
        queue = bus.subscribe()
        assert bus.subscriber_count == 1

        bus.publish(TaskEvent(task_id="T1", event_type="info", data="hello"))
        event = queue.get_nowait()
        assert event.data == "hello"

    def test_unsubscribe(self):
        bus = EventBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        assert bus.subscriber_count == 0

    def test_full_queue_drops_event(self):
        bus = EventBus(max_queue_size=1)
        queue = bus.subscribe()
        bus.publish(TaskEvent(task_id="T1", event_type="info", data="a"))
        bus.publish(TaskEvent(task_id="T1", event_type="info", data="b"))
        # Only first event should be in queue
        assert queue.qsize() == 1
        event = queue.get_nowait()
        assert event.data == "a"

    def test_thread_safety_drain(self):
        """drain_recent is thread-safe."""
        import threading

        bus = EventBus()

        def publisher():
            for i in range(100):
                bus.publish(TaskEvent(task_id="T1", event_type="info", data=str(i)))

        def consumer():
            total = 0
            for _ in range(50):
                events = bus.drain_recent()
                total += len(events)

        threads = [
            threading.Thread(target=publisher),
            threading.Thread(target=consumer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No assertion on count — just verifying no crash/deadlock


class TestEventBusWithRunClaudeAsync:
    """Integration test: verify run_claude_async streams to EventBus."""

    def test_streaming_captures_output_lines(self):
        """run_claude_async with event_bus publishes output_line events."""

        async def _run():
            bus = EventBus()
            from spec_runner.runner import run_claude_async

            stdout, stderr, rc = await run_claude_async(
                ["echo", "hello\nworld"],
                timeout=10,
                cwd=".",
                event_bus=bus,
                task_id="TASK-TEST",
            )
            events = bus.drain_recent()
            return stdout, events, rc

        stdout, events, rc = asyncio.run(_run())
        assert rc == 0
        assert "hello" in stdout
        # At least one event should be published
        assert len(events) >= 1
        assert events[0].task_id == "TASK-TEST"
        assert events[0].event_type == "output_line"

    def test_non_streaming_still_works(self):
        """run_claude_async without event_bus works as before."""

        async def _run():
            from spec_runner.runner import run_claude_async

            return await run_claude_async(
                ["echo", "hello"],
                timeout=10,
                cwd=".",
            )

        stdout, stderr, rc = asyncio.run(_run())
        assert rc == 0
        assert "hello" in stdout
