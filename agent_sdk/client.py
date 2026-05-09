import asyncio
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, Callable, Optional


# Module-level ContextVar for the async SDK to track which task a handler
# coroutine is currently executing on behalf of.
current_task_id_var: ContextVar[Optional[str]] = ContextVar(
    "current_task_id", default=None
)


# === Exceptions (Phase 2 §2.2.6) ===


class ChildRejectedError(Exception):
    """Raised when the hub returns child_rejected for a submit_child call."""

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}" if message else reason)


class ChildWaitTimeout(Exception):
    """Raised when wait_timeout elapses before child_completed arrives."""


class ChildNotInTaskContext(Exception):
    """Raised when submit_child is called outside of an active task handler."""


class AgentHub:
    def __init__(
        self,
        hub_url: str,
        agent_id: str,
        capabilities: list[str],
        auth_token: Optional[str] = None,
        task_handler: Optional[Callable] = None,
        reconnect: bool = True,
        unregister_on_stop: bool = False,
    ):
        self.hub_url = hub_url.rstrip("/")
        self.ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.auth_token = auth_token
        self._task_handler = task_handler
        self.reconnect = reconnect
        self.unregister_on_stop = unregister_on_stop
        self._running = False
        self._ws = None

        # Phase 2 §2.2.6: reader-loop decoupling for sync SDK.
        self._send_lock = threading.Lock()
        workers = int(os.getenv("AGENT_HUB_HANDLER_WORKERS", "4"))
        self._executor = ThreadPoolExecutor(max_workers=workers)
        # request_id -> {"event": Event, "result": dict | None}
        # The "result" slot is filled with either the child_accepted payload
        # (carrying child_task_id) or the child_rejected payload.
        self._pending_requests: dict[str, dict[str, Any]] = {}
        # child_task_id -> {"event": Event, "result": dict | None}
        self._pending_children: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._current_task_id = threading.local()

    def task_handler(self, func: Callable):
        """Decorator for registering a task handler."""
        self._task_handler = func
        return func

    def _send_json(self, msg: dict):
        if self._ws:
            with self._send_lock:
                self._ws.send(json.dumps(msg))

    def _on_open(self, ws):
        # Use raw send here; ws is fresh and we haven't stored it yet.
        ws.send(json.dumps({
            "type": "register",
            "agent_id": self.agent_id,
            "capabilities": self.capabilities,
            "auth_token": self.auth_token,
        }))
        print(f"Agent {self.agent_id} sent registration")

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            print(f"Invalid JSON: {message}")
            return

        msg_type = msg.get("type")
        if msg_type == "registered":
            print(f"Agent {self.agent_id} registered successfully")
            return

        if msg_type == "task":
            # Phase 2 §2.2.6: route task execution to the worker pool so the
            # reader thread keeps draining child_*/ping while a handler awaits
            # a child future.
            self._executor.submit(self._handle_task, msg.get("task", {}))
            return

        if msg_type == "ping":
            self._send_json({"type": "pong"})
            return

        if msg_type == "child_accepted":
            self._resolve_pending_request(msg)
            return

        if msg_type == "child_rejected":
            self._resolve_pending_request(msg)
            return

        if msg_type == "child_completed":
            self._resolve_pending_child(msg)
            return

        if msg_type == "error":
            print(f"Hub error: {msg.get('message')}")

    def _resolve_pending_request(self, msg: dict):
        request_id = msg.get("request_id")
        if not request_id:
            return
        with self._pending_lock:
            slot = self._pending_requests.get(request_id)
            if slot is None:
                return
            slot["result"] = msg
            # If this is a child_accepted, pre-register a child wait slot so
            # we don't race a fast child_completed push.
            if msg.get("type") == "child_accepted":
                child_id = msg.get("child_task_id")
                if child_id and child_id not in self._pending_children:
                    self._pending_children[child_id] = {
                        "event": threading.Event(),
                        "result": None,
                    }
            slot["event"].set()

    def _resolve_pending_child(self, msg: dict):
        child_id = msg.get("child_task_id")
        if not child_id:
            return
        with self._pending_lock:
            slot = self._pending_children.get(child_id)
            if slot is None:
                # Late or unknown push — drop silently (parent may already
                # have raised ChildWaitTimeout).
                return
            slot["result"] = msg
            slot["event"].set()

    def _handle_task(self, task: dict):
        task_id = task.get("task_id")
        # Phase 2 §2.2.6: stash the current task id so submit_child can
        # discover its parent_task_id from the running handler.
        self._current_task_id.value = task_id
        try:
            print(f"Received task: {task_id}")
            self._send_json({"type": "status", "task_id": task_id, "status": "running"})

            if not self._task_handler:
                self._send_json({
                    "type": "result",
                    "task_id": task_id,
                    "status": "failed",
                    "result": {"error": "No task handler configured"},
                })
                return

            try:
                result = self._task_handler(task)
                self._send_json({
                    "type": "result",
                    "task_id": task_id,
                    "status": "completed",
                    "result": result,
                })
            except Exception as exc:
                self._send_json({
                    "type": "result",
                    "task_id": task_id,
                    "status": "failed",
                    "result": {"error": str(exc)},
                })
        finally:
            self._current_task_id.value = None

    def submit_child(
        self,
        *,
        target_agent: str,
        task: str = "",
        task_type: Optional[str] = None,
        payload: Optional[dict] = None,
        priority: int = 0,
        timeout: int = 300,
        wait_timeout: Optional[float] = None,
    ) -> dict:
        """Delegate work to another agent and block until the child finishes.

        Must be called from inside a task handler. Returns the child_completed
        payload dict (which carries `status` and `result`). Raises
        ChildNotInTaskContext, ChildRejectedError, or ChildWaitTimeout.
        """
        parent_task_id = getattr(self._current_task_id, "value", None)
        if not parent_task_id:
            raise ChildNotInTaskContext(
                "submit_child must be called from inside a task handler"
            )

        if wait_timeout is None:
            wait_timeout = float(timeout) + 30.0

        request_id = str(uuid.uuid4())
        request_event = threading.Event()
        with self._pending_lock:
            self._pending_requests[request_id] = {
                "event": request_event,
                "result": None,
            }

        try:
            msg: dict[str, Any] = {
                "type": "submit_child",
                "request_id": request_id,
                "parent_task_id": parent_task_id,
                "target_agent": target_agent,
                "task": task,
                "priority": priority,
                "timeout": timeout,
            }
            if task_type is not None:
                msg["task_type"] = task_type
            if payload is not None:
                msg["payload"] = payload
            self._send_json(msg)

            if not request_event.wait(timeout=wait_timeout):
                raise ChildWaitTimeout(
                    f"submit_child request {request_id} timed out before hub ack"
                )

            with self._pending_lock:
                ack = self._pending_requests.pop(request_id, {}).get("result")

            if not ack:
                raise ChildWaitTimeout("submit_child got no ack")

            if ack.get("type") == "child_rejected":
                # Clean up any pre-registered child slot (none in reject path).
                raise ChildRejectedError(
                    ack.get("reason", "unknown"),
                    ack.get("message", ""),
                )

            child_task_id = ack.get("child_task_id")
            if not child_task_id:
                raise ChildRejectedError("bad_ack", "child_accepted missing child_task_id")

            # Wait for child_completed (slot was pre-registered in
            # _resolve_pending_request).
            with self._pending_lock:
                child_slot = self._pending_children.get(child_task_id)
                if child_slot is None:
                    # Shouldn't happen — pre-registered in resolver — but be
                    # defensive against races.
                    child_slot = {"event": threading.Event(), "result": None}
                    self._pending_children[child_task_id] = child_slot

            if not child_slot["event"].wait(timeout=wait_timeout):
                with self._pending_lock:
                    self._pending_children.pop(child_task_id, None)
                raise ChildWaitTimeout(
                    f"child task {child_task_id} did not complete within {wait_timeout}s"
                )

            with self._pending_lock:
                completed = self._pending_children.pop(child_task_id, {}).get("result")

            return completed or {}
        finally:
            with self._pending_lock:
                self._pending_requests.pop(request_id, None)

    def _on_error(self, ws, error):
        print(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"WebSocket closed: {close_status_code} - {close_msg}")

    def _run_once(self):
        import websocket

        self._ws = websocket.WebSocketApp(
            f"{self.ws_url}/ws",
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._ws.run_forever(ping_interval=30)

    def start(self):
        """Start the agent and block until stopped."""
        self._running = True
        delay = 1
        while self._running:
            self._run_once()
            if not self.reconnect:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)

    def stop(self):
        self._running = False
        if self.unregister_on_stop:
            self._unregister_best_effort()
        if self._ws:
            self._ws.close()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python <3.9 fallback (no cancel_futures kwarg).
            self._executor.shutdown(wait=False)
        print(f"Agent {self.agent_id} stopped")

    def _unregister_best_effort(self):
        # REST POST /unregister?agent_id=<id>. Best-effort: a hub that's
        # already gone shouldn't prevent local shutdown.
        import httpx
        try:
            httpx.post(
                f"{self.hub_url}/unregister",
                params={"agent_id": self.agent_id},
                headers={"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {},
                timeout=2.0,
            )
        except Exception as exc:
            print(f"[agent_sdk] unregister failed (ignored): {exc}")

    def send_log(self, task_id: str, log: str):
        self._send_json({"type": "log", "task_id": task_id, "log": log})


class AsyncAgentHub:
    def __init__(
        self,
        hub_url: str,
        agent_id: str,
        capabilities: list[str],
        auth_token: Optional[str] = None,
        task_handler: Optional[Callable] = None,
        unregister_on_stop: bool = False,
    ):
        self.hub_url = hub_url.rstrip("/")
        self.ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.auth_token = auth_token
        self._task_handler = task_handler
        self.unregister_on_stop = unregister_on_stop
        self._ws = None

        # Phase 2 §2.2.6: send-side serialization + pending-wait maps.
        self._send_lock = asyncio.Lock()
        # request_id -> Future[dict] (resolved with child_accepted or child_rejected msg)
        self._pending_requests: dict[str, asyncio.Future] = {}
        # child_task_id -> Future[dict] (resolved with child_completed msg)
        self._pending_children: dict[str, asyncio.Future] = {}

    def task_handler(self, func: Callable):
        self._task_handler = func
        return func

    async def connect(self):
        import websockets

        self._ws = await websockets.connect(f"{self.ws_url}/ws")
        await self._ws.send(json.dumps({
            "type": "register",
            "agent_id": self.agent_id,
            "capabilities": self.capabilities,
            "auth_token": self.auth_token,
        }))
        return json.loads(await self._ws.recv())

    async def send(self, msg: dict):
        async with self._send_lock:
            await self._ws.send(json.dumps(msg))

    async def stop(self):
        if self.unregister_on_stop:
            await self._unregister_best_effort()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _unregister_best_effort(self):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=2.0) as http:
                await http.post(
                    f"{self.hub_url}/unregister",
                    params={"agent_id": self.agent_id},
                    headers={"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {},
                )
        except Exception as exc:
            print(f"[agent_sdk] unregister failed (ignored): {exc}")

    async def recv(self) -> dict:
        return json.loads(await self._ws.recv())

    async def run(self):
        await self.connect()
        while True:
            msg = await self.recv()
            t = msg.get("type")
            if t == "task":
                # Phase 2 §2.2.6: schedule handler concurrently so the reader
                # keeps draining child_*/ping while it awaits a child future.
                asyncio.create_task(self._handle_task(msg.get("task", {})))
            elif t == "ping":
                await self.send({"type": "pong"})
            elif t == "child_accepted" or t == "child_rejected":
                self._resolve_pending_request(msg)
            elif t == "child_completed":
                self._resolve_pending_child(msg)

    def _resolve_pending_request(self, msg: dict):
        request_id = msg.get("request_id")
        if not request_id:
            return
        fut = self._pending_requests.pop(request_id, None)
        if fut is None or fut.done():
            return
        # On child_accepted, pre-register the child future before completing
        # the request future, so a fast child_completed push can never lose.
        if msg.get("type") == "child_accepted":
            child_id = msg.get("child_task_id")
            if child_id and child_id not in self._pending_children:
                loop = asyncio.get_running_loop()
                self._pending_children[child_id] = loop.create_future()
        fut.set_result(msg)

    def _resolve_pending_child(self, msg: dict):
        child_id = msg.get("child_task_id")
        if not child_id:
            return
        fut = self._pending_children.get(child_id)
        if fut is None or fut.done():
            return
        fut.set_result(msg)

    async def _handle_task(self, task: dict):
        task_id = task.get("task_id")
        token = current_task_id_var.set(task_id)
        try:
            await self.send({"type": "status", "task_id": task_id, "status": "running"})
            try:
                if asyncio.iscoroutinefunction(self._task_handler):
                    result = await self._task_handler(task)
                elif self._task_handler:
                    result = self._task_handler(task)
                else:
                    raise RuntimeError("No task handler configured")
                await self.send({"type": "result", "task_id": task_id, "status": "completed", "result": result})
            except Exception as exc:
                await self.send({"type": "result", "task_id": task_id, "status": "failed", "result": {"error": str(exc)}})
        finally:
            current_task_id_var.reset(token)

    async def submit_child(
        self,
        *,
        target_agent: str,
        task: str = "",
        task_type: Optional[str] = None,
        payload: Optional[dict] = None,
        priority: int = 0,
        timeout: int = 300,
        wait_timeout: Optional[float] = None,
    ) -> dict:
        """Async equivalent of AgentHub.submit_child — see that docstring."""
        parent_task_id = current_task_id_var.get()
        if not parent_task_id:
            raise ChildNotInTaskContext(
                "submit_child must be called from inside a task handler"
            )

        if wait_timeout is None:
            wait_timeout = float(timeout) + 30.0

        loop = asyncio.get_running_loop()
        request_id = str(uuid.uuid4())
        request_fut: asyncio.Future = loop.create_future()
        self._pending_requests[request_id] = request_fut

        try:
            msg: dict[str, Any] = {
                "type": "submit_child",
                "request_id": request_id,
                "parent_task_id": parent_task_id,
                "target_agent": target_agent,
                "task": task,
                "priority": priority,
                "timeout": timeout,
            }
            if task_type is not None:
                msg["task_type"] = task_type
            if payload is not None:
                msg["payload"] = payload
            await self.send(msg)

            try:
                ack = await asyncio.wait_for(request_fut, timeout=wait_timeout)
            except asyncio.TimeoutError:
                self._pending_requests.pop(request_id, None)
                raise ChildWaitTimeout(
                    f"submit_child request {request_id} timed out before hub ack"
                )

            if ack.get("type") == "child_rejected":
                raise ChildRejectedError(
                    ack.get("reason", "unknown"),
                    ack.get("message", ""),
                )

            child_task_id = ack.get("child_task_id")
            if not child_task_id:
                raise ChildRejectedError("bad_ack", "child_accepted missing child_task_id")

            child_fut = self._pending_children.get(child_task_id)
            if child_fut is None:
                child_fut = loop.create_future()
                self._pending_children[child_task_id] = child_fut

            try:
                completed = await asyncio.wait_for(child_fut, timeout=wait_timeout)
            except asyncio.TimeoutError:
                self._pending_children.pop(child_task_id, None)
                raise ChildWaitTimeout(
                    f"child task {child_task_id} did not complete within {wait_timeout}s"
                )

            self._pending_children.pop(child_task_id, None)
            return completed
        finally:
            self._pending_requests.pop(request_id, None)
