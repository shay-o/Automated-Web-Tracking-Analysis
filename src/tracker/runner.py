from __future__ import annotations

import base64
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Page,
    Request,
    Response,
    Route,
    WebSocket,
    sync_playwright,
)

from tracker.action_script import ActionScript, Locator, Settle, Step, load_script

WEBDRIVER_HIDE_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> float:
    return time.time()


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self._fp = open(path, "w", buffering=1)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._fp.write(line + "\n")

    def close(self) -> None:
        self._fp.close()


class StepTracker:
    """Holds the currently active step id so request/WS handlers can tag captures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: str | None = None

    def set(self, step_id: str | None) -> None:
        with self._lock:
            self._current = step_id

    def get(self) -> str | None:
        with self._lock:
            return self._current


def _build_locator(page: Page, loc: Locator):
    if loc.css:
        el = page.locator(loc.css)
    elif loc.role:
        kwargs: dict[str, Any] = {}
        if loc.name:
            kwargs["name"] = loc.name
        elif loc.name_contains:
            import re
            kwargs["name"] = re.compile(re.escape(loc.name_contains))
        el = page.get_by_role(loc.role, **kwargs)  # type: ignore[arg-type]
    elif loc.text:
        el = page.get_by_text(loc.text)
    else:
        raise ValueError(f"Locator must specify css, role, or text: {loc!r}")
    return el.first if loc.first else el


def _settle(page: Page, settle: Settle | None) -> None:
    if not settle:
        return
    if settle.wait_for_selector:
        page.wait_for_selector(settle.wait_for_selector)
    if settle.wait_for_request:
        page.wait_for_request(settle.wait_for_request)
    if settle.network_idle_ms is not None:
        try:
            page.wait_for_load_state("networkidle", timeout=settle.network_idle_ms)
        except Exception:
            pass
    if settle.wait_ms is not None:
        page.wait_for_timeout(settle.wait_ms)


def _execute_step(page: Page, step: Step) -> None:
    if step.action == "goto":
        if not step.url:
            raise ValueError(f"goto step '{step.id}' requires url")
        page.goto(step.url, wait_until=step.wait_for or "load")
    elif step.action == "click":
        if not step.locator:
            raise ValueError(f"click step '{step.id}' requires locator")
        _build_locator(page, step.locator).click()
    elif step.action == "fill":
        if not step.locator or step.value is None:
            raise ValueError(f"fill step '{step.id}' requires locator and value")
        _build_locator(page, step.locator).fill(step.value)
    elif step.action == "select":
        if not step.locator or step.value is None:
            raise ValueError(f"select step '{step.id}' requires locator and value")
        _build_locator(page, step.locator).select_option(step.value)
    elif step.action == "press":
        if not step.locator or not step.key:
            raise ValueError(f"press step '{step.id}' requires locator and key")
        _build_locator(page, step.locator).press(step.key)
    elif step.action == "wait":
        if step.settle is None:
            raise ValueError(f"wait step '{step.id}' requires settle")
    else:
        raise ValueError(f"Unknown action: {step.action}")

    _settle(page, step.settle)


def _make_request_handler(
    writer: JsonlWriter, tracker: StepTracker
) -> tuple[Any, Any, Any]:
    request_starts: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def on_request(request: Request) -> None:
        try:
            post_data: str | None = None
            try:
                post_data = request.post_data
            except Exception:
                post_data = None

            record = {
                "kind": "request_started",
                "ts": _now_iso(),
                "step_id": tracker.get(),
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "headers": dict(request.headers),
                "post_data": post_data,
            }
            with lock:
                request_starts[id(request)] = record
            writer.write(record)
        except Exception as e:
            writer.write({"kind": "request_error", "phase": "start", "error": str(e)})

    def on_response(response: Response) -> None:
        try:
            req = response.request
            record: dict[str, Any] = {
                "kind": "response",
                "ts": _now_iso(),
                "step_id": tracker.get(),
                "url": req.url,
                "method": req.method,
                "status": response.status,
                "status_text": response.status_text,
                "headers": dict(response.headers),
            }
            ctype = response.headers.get("content-type", "")
            if any(s in ctype for s in ("json", "javascript", "text", "xml")):
                try:
                    record["body_text"] = response.text()
                except Exception:
                    pass
            writer.write(record)
        except Exception as e:
            writer.write({"kind": "request_error", "phase": "response", "error": str(e)})

    def on_request_failed(request: Request) -> None:
        try:
            writer.write(
                {
                    "kind": "request_failed",
                    "ts": _now_iso(),
                    "step_id": tracker.get(),
                    "url": request.url,
                    "method": request.method,
                    "failure": request.failure,
                }
            )
        except Exception as e:
            writer.write({"kind": "request_error", "phase": "failed", "error": str(e)})

    return on_request, on_response, on_request_failed


def _make_websocket_handler(writer: JsonlWriter, tracker: StepTracker):
    def on_websocket(ws: WebSocket) -> None:
        ws_url = ws.url
        writer.write(
            {
                "kind": "ws_open",
                "ts": _now_iso(),
                "step_id": tracker.get(),
                "ws_url": ws_url,
            }
        )

        def on_framesent(payload: str | bytes) -> None:
            data_text: str | None = None
            data_b64: str | None = None
            if isinstance(payload, bytes):
                data_b64 = base64.b64encode(payload).decode("ascii")
                length = len(payload)
            else:
                data_text = payload
                length = len(payload.encode("utf-8"))
            writer.write(
                {
                    "kind": "ws_frame",
                    "ts": _now_iso(),
                    "step_id": tracker.get(),
                    "ws_url": ws_url,
                    "direction": "send",
                    "length": length,
                    "data_text": data_text,
                    "data_b64": data_b64,
                }
            )

        def on_framereceived(payload: str | bytes) -> None:
            data_text: str | None = None
            data_b64: str | None = None
            if isinstance(payload, bytes):
                data_b64 = base64.b64encode(payload).decode("ascii")
                length = len(payload)
            else:
                data_text = payload
                length = len(payload.encode("utf-8"))
            writer.write(
                {
                    "kind": "ws_frame",
                    "ts": _now_iso(),
                    "step_id": tracker.get(),
                    "ws_url": ws_url,
                    "direction": "receive",
                    "length": length,
                    "data_text": data_text,
                    "data_b64": data_b64,
                }
            )

        def on_close() -> None:
            writer.write(
                {
                    "kind": "ws_close",
                    "ts": _now_iso(),
                    "step_id": tracker.get(),
                    "ws_url": ws_url,
                }
            )

        ws.on("framesent", on_framesent)
        ws.on("framereceived", on_framereceived)
        ws.on("close", on_close)

    return on_websocket


def _make_console_handler(writer: JsonlWriter, tracker: StepTracker):
    def on_console(msg) -> None:
        try:
            writer.write(
                {
                    "kind": "console",
                    "ts": _now_iso(),
                    "step_id": tracker.get(),
                    "type": msg.type,
                    "text": msg.text,
                }
            )
        except Exception:
            pass

    return on_console


def _snapshot_datalayer(page: Page) -> Any:
    try:
        return page.evaluate("() => (window.dataLayer ? JSON.parse(JSON.stringify(window.dataLayer)) : null)")
    except Exception:
        return None


def run(script_path: Path, out_dir: Path | None = None, headless: bool = False) -> Path:
    script = load_script(script_path)

    if out_dir is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_dir = Path("runs") / f"{stamp}_{script.session}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "screenshots").mkdir(exist_ok=True)

    har_path = out_dir / "session.har"
    requests_writer = JsonlWriter(out_dir / "requests.jsonl")
    ws_writer = JsonlWriter(out_dir / "websocket_frames.jsonl")
    console_writer = JsonlWriter(out_dir / "console.jsonl")
    datalayer_writer = JsonlWriter(out_dir / "datalayer.jsonl")
    steps_writer = JsonlWriter(out_dir / "steps.jsonl")

    tracker = StepTracker()

    print(f"[tracker] Running session '{script.session}' → {out_dir}")
    print(f"[tracker] Headless: {headless}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": script.viewport.width, "height": script.viewport.height},
                record_har_path=str(har_path),
                record_har_content="embed",
            )
            context.add_init_script(WEBDRIVER_HIDE_SCRIPT)

            page = context.new_page()

            on_req, on_resp, on_req_failed = _make_request_handler(requests_writer, tracker)
            page.on("request", on_req)
            page.on("response", on_resp)
            page.on("requestfailed", on_req_failed)
            page.on("websocket", _make_websocket_handler(ws_writer, tracker))
            page.on("console", _make_console_handler(console_writer, tracker))

            # Pre-flight navigation to start_url if first step is not already a goto.
            if not (script.steps and script.steps[0].action == "goto"):
                page.goto(script.start_url, wait_until="load")

            for step in script.steps:
                step_record: dict[str, Any] = {
                    "id": step.id,
                    "action": step.action,
                    "start_ts": _now_iso(),
                    "start_epoch": _ts(),
                }
                tracker.set(step.id)
                print(f"[tracker] step {step.id}: {step.action}")

                before_path = out_dir / "screenshots" / f"{step.id}_before.png"
                try:
                    page.screenshot(path=str(before_path), full_page=False)
                    step_record["screenshot_before"] = str(before_path.relative_to(out_dir))
                except Exception as e:
                    step_record["screenshot_before_error"] = str(e)

                error: str | None = None
                try:
                    _execute_step(page, step)
                except Exception as e:
                    error = repr(e)
                    print(f"[tracker]   ERROR: {error}")

                after_path = out_dir / "screenshots" / f"{step.id}_after.png"
                try:
                    page.screenshot(path=str(after_path), full_page=False)
                    step_record["screenshot_after"] = str(after_path.relative_to(out_dir))
                except Exception as e:
                    step_record["screenshot_after_error"] = str(e)

                dl = _snapshot_datalayer(page)
                datalayer_writer.write(
                    {"step_id": step.id, "ts": _now_iso(), "datalayer": dl}
                )

                step_record["end_ts"] = _now_iso()
                step_record["end_epoch"] = _ts()
                step_record["duration_ms"] = int(
                    (step_record["end_epoch"] - step_record["start_epoch"]) * 1000
                )
                if error:
                    step_record["error"] = error
                steps_writer.write(step_record)

            tracker.set(None)
            context.close()
            browser.close()
    finally:
        requests_writer.close()
        ws_writer.close()
        console_writer.close()
        datalayer_writer.close()
        steps_writer.close()

    print(f"[tracker] Done. Artifact: {out_dir}")
    return out_dir
