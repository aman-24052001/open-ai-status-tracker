#!/usr/bin/env python3
"""
OpenAI Status Tracker
=====================
Automatically tracks and logs service updates from the OpenAI Status Page.
Detects new incidents, outages, degradations, and component status changes.

Architecture:
    Event-driven with asyncio.Queue as the central event bus.
    - Producer tasks (RSS watcher, JSON watcher) independently monitor sources
      and push StatusEvent objects onto the queue when changes are detected.
    - A single consumer task reads from the queue and prints to console.
    - Adding a new provider = spawning 2 more producer tasks on the same queue.
    - Adding a push-based source (SSE/webhook) = another producer, zero consumer changes.

    Transport efficiency:
    - Conditional HTTP (ETag / If-Modified-Since) — 304 responses skip all processing.
    - Seed-then-watch — first fetch records baseline silently, subsequent fetches emit only on change.

Usage:
    python openai_status_main.py
    POLL_INTERVAL=15 python openai_status_main.py
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum

import feedparser
import httpx
from html.parser import HTMLParser
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_RETRIES = 3

# Add more providers to scale — each gets its own pair of producer tasks.
STATUS_PAGES: list[dict[str, str]] = [
    {
        "name": "OpenAI",
        "rss_url": "https://status.openai.com/feed.rss",
        "json_url": "https://status.openai.com/api/v2/summary.json",
    },
    # {
    #     "name": "GitHub",
    #     "rss_url": "https://www.githubstatus.com/history.rss",
    #     "json_url": "https://www.githubstatus.com/api/v2/summary.json",
    # },
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("status-tracker")


# ═══════════════════════════════════════════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class ComponentStatus(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded_performance"
    PARTIAL_OUTAGE = "partial_outage"
    MAJOR_OUTAGE = "major_outage"
    UNKNOWN = "unknown"


COMPONENT_STATUS_LABELS: dict[ComponentStatus, str] = {
    ComponentStatus.OPERATIONAL: "Operational",
    ComponentStatus.DEGRADED: "Degraded performance",
    ComponentStatus.PARTIAL_OUTAGE: "Partial outage",
    ComponentStatus.MAJOR_OUTAGE: "Major outage",
    ComponentStatus.UNKNOWN: "Unknown",
}


class IncidentStatus(str, Enum):
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    POSTMORTEM = "postmortem"
    UNKNOWN = "unknown"


class Component(BaseModel):
    id: str
    name: str
    status: ComponentStatus = ComponentStatus.OPERATIONAL


class Incident(BaseModel):
    id: str
    title: str
    status: IncidentStatus = IncidentStatus.UNKNOWN
    message: str = ""
    affected_components: list[str] = Field(default_factory=list)
    timestamp: datetime | None = None
    provider: str = ""


class StatusEvent(BaseModel):
    """The unit of data that flows through the event bus (asyncio.Queue)."""

    class EventType(str, Enum):
        NEW_INCIDENT = "new_incident"
        INCIDENT_UPDATE = "incident_update"
        COMPONENT_STATUS_CHANGE = "component_status_change"

    event_type: EventType
    provider: str
    timestamp: datetime
    title: str
    message: str = ""
    affected_components: list[str] = Field(default_factory=list)
    old_status: str = ""
    new_status: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
#  RSS PARSING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_INCIDENT_STATUS_MAP: dict[str, IncidentStatus] = {
    "resolved": IncidentStatus.RESOLVED,
    "investigating": IncidentStatus.INVESTIGATING,
    "identified": IncidentStatus.IDENTIFIED,
    "monitoring": IncidentStatus.MONITORING,
    "postmortem": IncidentStatus.POSTMORTEM,
}

_COMP_STATUS_MAP: dict[str, ComponentStatus] = {
    "operational": ComponentStatus.OPERATIONAL,
    "degraded_performance": ComponentStatus.DEGRADED,
    "partial_outage": ComponentStatus.PARTIAL_OUTAGE,
    "major_outage": ComponentStatus.MAJOR_OUTAGE,
}


class _IncidentHTMLParser(HTMLParser):
    """
    Walks the Statuspage.io RSS description HTML using the stdlib html.parser.

    Why not regex?
    - Regex on HTML is order/whitespace-sensitive and silently fails on any
      structural variation. html.parser is tag-aware and handles entities,
      self-closing <br/>, and attribute variations correctly.
    - The old _COMPONENT_RE used \\w[\\w\\s]* which does NOT match underscores
      mid-token, so "degraded_performance" and "partial_outage" were broken.

    State machine:
        We track a simple 'section' flag as we walk tags top-to-bottom.
        The feed structure is:
            <b>Status: Investigating</b>
            <br/><br/>
            <p>...message text...</p>   (or bare text after <br/><br/>)
            <b>Affected components:</b>
            <ul><li>Component Name (status_string)</li>...</ul>
    """

    def __init__(self) -> None:
        super().__init__()
        self.status: IncidentStatus = IncidentStatus.UNKNOWN
        self.message: str = ""
        self.components: list[str] = []

        self._in_bold = False
        self._in_li = False
        self._section = "pre_status"   # pre_status → status → message → components
        self._buf = ""

    # ── tag callbacks ──────────────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "b":
            self._in_bold = True
            self._buf = ""
        elif tag == "li":
            self._in_li = True
            self._buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "b":
            self._in_bold = False
            text = self._buf.strip()
            if text.lower().startswith("status:"):
                raw = text[len("status:"):].strip().lower()
                self.status = _INCIDENT_STATUS_MAP.get(raw, IncidentStatus.UNKNOWN)
                self._section = "message"
            elif "affected" in text.lower():
                self._section = "components"
            self._buf = ""

        elif tag == "li":
            self._in_li = False
            text = self._buf.strip()
            # Component lines look like "Component Name (status_string)"
            # Use rfind so component names containing parens still work.
            paren_open = text.rfind("(")
            paren_close = text.rfind(")")
            if paren_open != -1 and paren_close > paren_open:
                name = text[:paren_open].strip()
                if name:
                    self.components.append(name)
            self._buf = ""

    # ── data callback ──────────────────────────────────────────────────────────

    def handle_data(self, data: str) -> None:
        if self._in_bold or self._in_li:
            self._buf += data
        elif self._section == "message":
            chunk = data.strip()
            if chunk:
                self.message += (" " if self.message else "") + chunk


def _parse_rss_entry_html(html: str) -> tuple[IncidentStatus, str, list[str]]:
    """Parse the HTML description of a single RSS entry. Returns (status, message, components)."""
    parser = _IncidentHTMLParser()
    parser.feed(html)
    return parser.status, parser.message.strip(), parser.components


def _parse_timestamp(entry: dict) -> datetime | None:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_rss_incidents(text: str, provider: str) -> list[Incident]:
    """Parse RSS feed text into Incident list."""
    feed = feedparser.parse(text)
    incidents: list[Incident] = []
    for entry in feed.entries:
        html = entry.get("content", [{}])[0].get("value", "") or entry.get("summary", "")
        status, message, components = _parse_rss_entry_html(html)
        incidents.append(Incident(
            id=entry.get("id") or entry.get("link", ""),
            title=entry.get("title", "Unknown Incident"),
            status=status,
            message=message,
            affected_components=components,
            timestamp=_parse_timestamp(entry),
            provider=provider,
        ))
    return incidents


def _parse_json_components(data: dict) -> list[Component]:
    """Parse JSON API response into Component list."""
    return [
        Component(
            id=c["id"], name=c["name"],
            status=_COMP_STATUS_MAP.get(c.get("status", "operational"), ComponentStatus.UNKNOWN),
        )
        for c in data.get("components", [])
    ]


# ═══════════════════════════════════════════════════════════════════════════════
#  CHANGE DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class ChangeDetector:
    """Stateful diff engine — tracks incidents by content hash, components by last status."""

    def __init__(self) -> None:
        self._incident_hashes: dict[str, str] = {}
        self._component_statuses: dict[str, ComponentStatus] = {}
        self._seeded = False

    @staticmethod
    def _hash_incident(inc: Incident) -> str:
        # Normalize components (important fix)
        components = sorted(c.strip() for c in inc.affected_components)

        # Normalize text fields
        status = inc.status.strip()
        message = inc.message.strip()

        payload = f"{status}|{message}|{','.join(components)}"
        return hashlib.md5(payload.encode()).hexdigest()

    def detect_incident_changes(self, incidents: list[Incident], provider: str) -> list[StatusEvent]:
        events: list[StatusEvent] = []
        current: dict[str, str] = {}

        for inc in incidents:
            h = self._hash_incident(inc)
            current[inc.id] = h

            if not self._seeded:
                continue

            ts = inc.timestamp or datetime.now(timezone.utc)
            if inc.id not in self._incident_hashes:
                events.append(StatusEvent(
                    event_type=StatusEvent.EventType.NEW_INCIDENT, provider=provider,
                    timestamp=ts, title=inc.title, message=inc.message,
                    affected_components=inc.affected_components, new_status=inc.status.value,
                ))
            elif self._incident_hashes[inc.id] != h:
                events.append(StatusEvent(
                    event_type=StatusEvent.EventType.INCIDENT_UPDATE, provider=provider,
                    timestamp=ts, title=inc.title, message=inc.message,
                    affected_components=inc.affected_components, new_status=inc.status.value,
                ))

        self._incident_hashes = current
        return events

    def detect_component_changes(self, components: list[Component], provider: str) -> list[StatusEvent]:
        events: list[StatusEvent] = []
        for comp in components:
            prev = self._component_statuses.get(comp.id)
            if prev is not None and prev != comp.status and self._seeded:
                events.append(StatusEvent(
                    event_type=StatusEvent.EventType.COMPONENT_STATUS_CHANGE, provider=provider,
                    timestamp=datetime.now(timezone.utc), title=comp.name,
                    old_status=prev.value, new_status=comp.status.value,
                ))
            self._component_statuses[comp.id] = comp.status
        return events

    def mark_seeded(self) -> None:
        self._seeded = True


# ═══════════════════════════════════════════════════════════════════════════════
#  CONDITIONAL HTTP
# ═══════════════════════════════════════════════════════════════════════════════

class ConditionalHTTP:
    """Wraps httpx GET with ETag/If-Modified-Since + retry. One instance per URL."""

    def __init__(self) -> None:
        self._etag: str | None = None
        self._last_modified: str | None = None

    async def get(self, client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        """Returns None on 304 (no change). Retries transient failures."""
        headers: dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 304:
                    return None
                resp.raise_for_status()
                if "etag" in resp.headers:
                    self._etag = resp.headers["etag"]
                if "last-modified" in resp.headers:
                    self._last_modified = resp.headers["last-modified"]
                return resp
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    wait = 2 ** (attempt - 1)
                    logger.warning("Retry %d/%d for %s: %s", attempt, MAX_RETRIES, url, exc)
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════════
#  EVENT FORMATTING
# ═══════════════════════════════════════════════════════════════════════════════

def format_event(event: StatusEvent, provider_name: str) -> str:
    """
    Format matching the assignment example:
        [2025-11-03 14:32:00] Product: OpenAI API - Chat Completions
        Status: Degraded performance due to upstream issue
    """
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")

    if event.event_type == StatusEvent.EventType.COMPONENT_STATUS_CHANGE:
        new_label = COMPONENT_STATUS_LABELS.get(
            ComponentStatus(event.new_status), event.new_status
        )
        return (
            f"[{ts}] Product: {provider_name} API - {event.title}\n"
            f"Status: {new_label}"
        )

    products = event.affected_components if event.affected_components else [event.title]
    product_str = ", ".join(products)
    status_label = event.new_status.capitalize()
    status_line = f"{status_label} — {event.message}" if event.message else status_label

    return (
        f"[{ts}] Product: {provider_name} API - {product_str}\n"
        f"Status: {status_line}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  SEED — plain awaited function, runs before any tasks are spawned
# ═══════════════════════════════════════════════════════════════════════════════

async def seed_provider(
    client: httpx.AsyncClient,
    cfg: dict[str, str],
    detector: ChangeDetector,
    rss_http: ConditionalHTTP,
    json_http: ConditionalHTTP,
) -> None:
    """
    Fetch both sources once and record baseline state. No events emitted.
    Called with plain await — no tasks, no races, no timing issues.
    """
    name = cfg["name"]

    try:
        resp = await rss_http.get(client, cfg["rss_url"])
        if resp is not None:
            incidents = _parse_rss_incidents(resp.text, name)
            detector.detect_incident_changes(incidents, name)
    except Exception as exc:
        logger.warning("[%s] RSS seed failed: %s", name, exc)

    try:
        resp = await json_http.get(client, cfg["json_url"])
        if resp is not None:
            components = _parse_json_components(resp.json())
            detector.detect_component_changes(components, name)
    except Exception as exc:
        logger.warning("[%s] JSON seed failed: %s", name, exc)

    detector.mark_seeded()
    logger.info(
        "[%s] Seeded — %d incidents, %d components",
        name, len(detector._incident_hashes), len(detector._component_statuses),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TASK SUPERVISOR
# ═══════════════════════════════════════════════════════════════════════════════

async def supervised(coro_fn, *args, name: str = "task", restart_delay: float = 5.0):
    """
    Run an async producer function forever, restarting it if it raises.

    Why this matters:
        asyncio.gather() propagates the *first* unhandled exception and cancels
        every other task. Without a supervisor, a single bad HTTP response or
        unexpected parse edge-case that slips past inner try/except kills the
        entire monitor process — including all other providers.

    Design:
        - CancelledError is NOT caught; it lets normal shutdown (Ctrl+C /
          task.cancel()) propagate cleanly.
        - Everything else is logged and the coroutine is restarted after a
          fixed backoff (default 5 s) so we don't tight-loop on persistent errors.
    """
    while True:
        try:
            await coro_fn(*args)
        except asyncio.CancelledError:
            raise  # honour cancellation — do not restart
        except Exception as exc:
            logger.error("[%s] crashed: %s — restarting in %.0fs", name, exc, restart_delay)
            await asyncio.sleep(restart_delay)


# ═══════════════════════════════════════════════════════════════════════════════
#  PRODUCERS — only spawned AFTER seed is complete
# ═══════════════════════════════════════════════════════════════════════════════

async def rss_producer(
    queue: asyncio.Queue[StatusEvent],
    client: httpx.AsyncClient,
    cfg: dict[str, str],
    detector: ChangeDetector,
    http: ConditionalHTTP,
) -> None:
    """
    Producer task: polls RSS feed, detects incident changes, pushes events to queue.
    Sleep-first: waits one interval before first fetch (seed already has baseline).
    """
    name = cfg["name"]
    url = cfg["rss_url"]

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            resp = await http.get(client, url)
            if resp is not None:
                incidents = _parse_rss_incidents(resp.text, name)
                for event in detector.detect_incident_changes(incidents, name):
                    await queue.put(event)
        except Exception as exc:
            logger.warning("[%s] RSS watcher error: %s", name, exc)


async def json_producer(
    queue: asyncio.Queue[StatusEvent],
    client: httpx.AsyncClient,
    cfg: dict[str, str],
    detector: ChangeDetector,
    http: ConditionalHTTP,
) -> None:
    """
    Producer task: polls JSON API, detects component changes, pushes events to queue.
    Sleep-first: waits one interval before first fetch (seed already has baseline).
    """
    name = cfg["name"]
    url = cfg["json_url"]

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            resp = await http.get(client, url)
            if resp is not None:
                for event in detector.detect_component_changes(
                    _parse_json_components(resp.json()), name
                ):
                    await queue.put(event)
        except Exception as exc:
            logger.warning("[%s] JSON watcher error: %s", name, exc)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSUMER
# ═══════════════════════════════════════════════════════════════════════════════

async def event_consumer(queue: asyncio.Queue[StatusEvent]) -> None:
    """
    Consumer task: reads events from the queue and prints to console.
    Fully decoupled from producers — doesn't know or care where events come from.
    """
    while True:
        event = await queue.get()
        print(format_event(event, event.provider))
        print()
        queue.task_done()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    queue: asyncio.Queue[StatusEvent] = asyncio.Queue(maxsize=1000)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT), follow_redirects=True, http2=True,
    ) as client:

        # ── Phase 1: Seed (plain await, no tasks, no race) ───────────────────
        providers: list[tuple[dict, ChangeDetector, ConditionalHTTP, ConditionalHTTP]] = []

        for cfg in STATUS_PAGES:
            detector = ChangeDetector()
            rss_http = ConditionalHTTP()
            json_http = ConditionalHTTP()
            await seed_provider(client, cfg, detector, rss_http, json_http)
            providers.append((cfg, detector, rss_http, json_http))

        # ── Phase 2: Launch event bus ────────────────────────────────────────
        print(
            f"\n{'─' * 60}\n"
            f"  Monitoring {len(STATUS_PAGES)} status page(s)\n"
            f"  Poll interval: {POLL_INTERVAL}s\n"
            f"  Press Ctrl+C to stop\n"
            f"{'─' * 60}\n"
        )

        tasks: list[asyncio.Task] = []
        tasks.append(asyncio.create_task(event_consumer(queue), name="consumer"))

        for cfg, detector, rss_http, json_http in providers:
            tasks.append(asyncio.create_task(
                supervised(rss_producer, queue, client, cfg, detector, rss_http,
                           name=f"{cfg['name']}-rss"),
                name=f"{cfg['name']}-rss",
            ))
            tasks.append(asyncio.create_task(
                supervised(json_producer, queue, client, cfg, detector, json_http,
                           name=f"{cfg['name']}-json"),
                name=f"{cfg['name']}-json",
            ))

        logger.info(
            "Watching %d provider(s) — %d tasks (poll every %ds)",
            len(STATUS_PAGES), len(tasks), POLL_INTERVAL,
        )

        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")