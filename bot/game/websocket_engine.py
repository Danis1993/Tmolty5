"""
WebSocket gameplay engine — wss://cdn.moltyroyale.com/ws/join.
Core loop: connect → process messages → decide → act → repeat.

Per game-loop.md:
- agent_view uses 'view' key (NOT 'data')
- turn_advanced includes full 'view' snapshot — MUST be processed
- action envelope: { type: "action", data: { type: "ACTION_TYPE", ... }, thought: {...} }
- action_result: includes canAct + cooldownRemainingMs at TOP LEVEL
- can_act_changed: canAct at TOP LEVEL (not nested in data)
- Only one WS session per API key
"""
import json
import asyncio
import websockets
from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import ActionSender, COOLDOWN_ACTIONS, FREE_ACTIONS
from bot.strategy.brain import decide_action, reset_game_state, learn_from_map
from bot.dashboard.state import dashboard_state
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


def _update_dz_knowledge(view: dict):
    """Continuously track death zones from every agent_view.
    Updates brain._map_knowledge with any new DZ regions observed.
    v1.5.2: pendingDeathzones entries are {id, name} objects.
    """
    from bot.strategy.brain import _map_knowledge
    # Track DZ from visible regions
    for region in view.get("visibleRegions", []):
        if isinstance(region, dict) and region.get("isDeathZone"):
            rid = region.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
    # Track from connected regions (type-safe: may be string IDs or objects)
    for conn in view.get("connectedRegions", []):
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            rid = conn.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
        # Bare string IDs — we don't know if it's DZ, skip
    # Track current region
    cur = view.get("currentRegion", {})
    if isinstance(cur, dict) and cur.get("isDeathZone"):
        rid = cur.get("id", "")
        if rid:
            _map_knowledge["death_zones"].add(rid)
    # Track pending DZ — v1.5.2: entries are {id, name} objects
    for dz in view.get("pendingDeathzones", []):
        if isinstance(dz, dict):
            rid = dz.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
        elif isinstance(dz, str):
            _map_knowledge["death_zones"].add(dz)  # Legacy fallback


class WebSocketEngine:
    """Manages the gameplay WebSocket session."""

    def __init__(self, game_id: str, agent_id: str):
        self.game_id = game_id
        self.agent_id = agent_id
        self.action_sender = ActionSender()
        self.ws = None
        self.game_result = None
        self.last_view = None
        self._ping_task = None
        self._running = False
        self._map_just_used = False  # Track if Map was used for learning
        # Dashboard key/name — set by heartbeat before .run()
        self.dashboard_key = agent_id  # fallback to agent_id
        self.dashboard_name = "Agent"

    async def run(self) -> dict:
        """
        Main gameplay loop. Returns game result dict.
        Per gotchas.md: connect with X-API-Key only, no gameId/agentId params.
        """
        api_key = get_api_key()

headers = {
    "X-API-Key": api_key,
    "X-Version": SKILL_VERSION,
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://www.moltyroyale.com",
}

self._running = True
retry_count = 0
max_retries = 5

        while self._running and retry_count < max_retries:
            try:
                log.info("Connecting WebSocket to %s...", WS_URL)
                async with websockets.connect(
                    WS_URL,
                    additional_headers=headers,
                    ping_interval=None,  # We handle our own pings
                    max_size=2**20,  # 1MB max message
                ) as ws:
                    self.ws = ws
                    retry_count = 0  # Reset on successful connect
                    log.info("✅ WebSocket connected for game=%s", self.game_id)

                    # Start ping keepalive
                    self._ping_task = asyncio.create_task(self._ping_loop())

                    # Message processing loop
                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            if not isinstance(msg, dict):
                                log.warning("Non-dict WS message: %s", type(msg).__name__)
                                continue
                            msg_type = msg.get("type", "unknown")
                            log.debug("WS recv: type=%s", msg_type)
                            result = await self._handle_message(msg)
                            if result is not None:
                                self._running = False
                                return result
                        except json.JSONDecodeError:
                            log.warning("Non-JSON message: %s", raw_msg[:100])

            except websockets.exceptions.ConnectionClosed as e:
                retry_count += 1
                log.warning("WebSocket closed: code=%s reason=%s (retry %d/%d)",
                            e.code, e.reason, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

            except Exception as e:
                retry_count += 1
                log.error("WebSocket error: %s (retry %d/%d)", e, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

        return self.game_result or {"status": "disconnected"}

    async def _handle_message(self, msg: dict) -> dict | None:
        """Process a single WebSocket message. Returns game result or None."""
        msg_type = msg.get("type", "")

        # ── agent_view ────────────────────────────────────────────────
        # Per game-loop.md: uses 'view' key for state data
        # Sent on: initial connect, game start, reconnect, vision change
        if msg_type == "agent_view":
            view = msg.get("view") or msg.get("data") or {}
            if isinstance(view, dict) and view:
                self.last_view = view
                reason = msg.get("reason", "initial")
                alive = view.get("self", {}).get("isAlive", "?")
                hp = view.get("self", {}).get("hp", "?")
                ep = view.get("self", {}).get("ep", "?")
                log.info("agent_view (reason=%s) alive=%s HP=%s EP=%s", reason, alive, hp, ep)
                await self._on_agent_view(view)
            else:
                log.warning("agent_view with empty/invalid view: %s", str(view)[:100])

        # ── action_result ─────────────────────────────────────────────
        # Per actions.md: canAct and cooldownRemainingMs are at TOP LEVEL
        elif msg_type == "action_result":
            success = msg.get("success", False)
            # canAct is at TOP LEVEL per actions.md, NOT inside data
            self.action_sender.can_act = msg.get("canAct", self.action_sender.can_act)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)

            if success:
                data = msg.get("data", {})
                action_msg = data.get("message", "") if isinstance(data, dict) else str(data)
                log.info("Action OK: %s (canAct=%s)", action_msg, msg.get("canAct"))
                # Track map usage for learning on next view
                if isinstance(data, dict) and "map" in str(action_msg).lower():
                    self._map_just_used = True
            else:
                err = msg.get("error", {})
                err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                err_msg = err.get("message", "") if isinstance(err, dict) else ""
                log.warning("Action FAILED: %s — %s (canAct=%s)", err_code, err_msg, msg.get("canAct"))

        # ── can_act_changed ───────────────────────────────────────────
        # Per actions.md: canAct is at TOP LEVEL
        elif msg_type == "can_act_changed":
            self.action_sender.can_act = msg.get("canAct", True)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)
            log.info("can_act_changed: canAct=%s", msg.get("canAct"))
            # Re-evaluate actions with current view
            if self.last_view and msg.get("canAct"):
                await self._on_agent_view(self.last_view)

        # ── turn_advanced ─────────────────────────────────────────────
        # Per game-loop.md: "turn_advanced is a pure state snapshot for a new turn"
        # It INCLUDES full 'view' data — MUST be processed like agent_view
        elif msg_type == "turn_advanced":
            # view can be at msg.view or msg.data.view or inside msg directly
            turn_num = msg.get("turn", "?")
            view = msg.get("view")
            if not view and isinstance(msg.get("data"), dict):
                view = msg["data"].get("view")
                turn_num = msg["data"].get("turn", turn_num)

            log.info("Turn %s — processing view...", turn_num)
            if view and isinstance(view, dict):
                self.last_view = view
                await self._on_agent_view(view)
            elif self.last_view:
                # No view in message — re-evaluate with last known state
                await self._on_agent_view(self.last_view)
            else:
                log.warning("Turn advanced but no view data available")

        # ── game_ended ────────────────────────────────────────────────
        elif msg_type == "game_ended":
            log.info("═══ GAME ENDED ═══")
            reset_game_state()  # Clear curse tracking for next game
            self.game_result = msg
            return msg

        # ── event ─────────────────────────────────────────────────────
        elif msg_type == "event":
            event_type = msg.get("eventType", msg.get("data", {}).get("eventType", ""))
            log.debug("Event: %s", event_type)

        # ── waiting ───────────────────────────────────────────────────
        elif msg_type == "waiting":
            log.info("Game is waiting for players...")

        # ── pong ──────────────────────────────────────────────────────
        elif msg_type == "pong":
            pass

        # ── error ─────────────────────────────────────────────────────
        elif msg_type == "error":
    log.error("FULL SERVER ERROR: %s", json.dumps(msg, indent=2))

        # ── unknown ───────────────────────────────────────────────────
        else:
            log.info("Unknown WS message type=%s keys=%s",
                     msg_type, list(msg.keys()))

        return None

    async def _on_agent_view(self, view: dict):
        """Process agent_view → decide action → send if appropriate."""
        if not isinstance(view, dict):
            return

        self_data = view.get("self", {})
        if not isinstance(self_data, dict):
            return

        alive_count = view.get("aliveCount", "?")

        if not self_data.get("isAlive", True):
            log.info("☠️ Agent DEAD — Alive remaining: %s. Waiting for game_ended...", alive_count)
            # Update dashboard with dead state (don't just return silently!)
            dk = self.dashboard_key
            dashboard_state.update_agent(dk, {
                "name": self.dashboard_name,
                "status": "dead",
                "hp": 0,
                "ep": 0,
                "maxHp": self_data.get("maxHp", 100),
                "maxEp": self_data.get("maxEp", 10),
                "alive_count": alive_count,
                "last_action": "☠️ DEAD — waiting for game to end",
                "enemies": [],
                "region_items": [],
            })
            dashboard_state.add_log(
                f"☠️ Agent DEAD — Alive remaining: {alive_count}",
                "warning", dk
            )
            return

        # Log status
        hp = self_data.get("hp", "?")
        ep = self_data.get("ep", "?")
        region = view.get("currentRegion", {})
        region_name = region.get("name", "?") if isinstance(region, dict) else "?"
        log.info("Status: HP=%s EP=%s Region=%s | Alive: %s", hp, ep, region_name, alive_count)
        dashboard_state.add_log(
            f"HP={hp} EP={ep} Region={region_name} | Alive: {alive_count}",
            "info", self.dashboard_key
        )

        # Feed dashboard with live game data
        inv = self_data.get("inventory", [])
        enemies = [a for a in view.get("visibleAgents", [])
                   if isinstance(a, dict) and a.get("isAlive") and a.get("id") != self_data.get("id")]

        # Region items: visibleItems entries are WRAPPED: { regionId, item: {id, name, ...} }
        # We must unwrap the .item sub-object and attach regionId to it.
        region_id = region.get("id", "") if isinstance(region, dict) else ""

        def _unwrap_items(raw_items):
            """Unwrap visibleItems: each entry is { regionId, item: {...} }.
            Returns flat list of item dicts with regionId attached."""
            result = []
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item")
                if isinstance(inner, dict):
                    # Attach regionId from wrapper to the inner item
                    inner["regionId"] = entry.get("regionId", "")
                    result.append(inner)
                elif entry.get("id"):
                    # Already a flat item (legacy format)
                    result.append(entry)
            return result

        region_items = []

        # Strategy 1: currentRegion.items (some game versions embed items here)
        if isinstance(region, dict) and region.get("items"):
            region_items = _unwrap_items(region["items"])

        # Strategy 2: filter visibleItems by regionId
        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            region_items = [i for i in all_visible
                            if i.get("regionId") == region_id]

        # Strategy 3: if regionId filter returns nothing, show ALL visible items
        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            if all_visible:
                region_items = all_visible

        equipped = self_data.get("equippedWeapon")
        weapon_name = "fist"
        weapon_bonus = 0
        if equipped and isinstance(equipped, dict):
            weapon_name = equipped.get("typeId", "fist")
            from bot.strategy.brain import WEAPONS
            weapon_bonus = WEAPONS.get(weapon_name.lower(), {}).get("bonus", 0)


        def _item_label(i):
            """Get best display label for an item.
            Try all possible field names the API might use.
            """
            return (i.get("name")
                    or i.get("typeId")
                    or i.get("type")
                    or i.get("itemType")
                    or i.get("itemName")
                    or i.get("label")
                    or i.get("kind")
                    or str(i.get("id", "?"))[:12])

        def _item_cat(i):
            """Get item category from any available field."""
            return (i.get("category")
                    or i.get("cat")
                    or i.get("itemCategory")
                    or i.get("type")
                    or "")

        dk = self.dashboard_key
        dashboard_state.update_agent(dk, {
            "name": self.dashboard_name,
            "hp": hp, "ep": ep,
            "status": "playing",
            "maxHp": self_data.get("maxHp", 100),
            "maxEp": self_data.get("maxEp", 10),
            "atk": self_data.get("atk", 0),
            "def": self_data.get("def", 0),
            "weapon": weapon_name,
            "weapon_bonus": weapon_bonus,
            "kills": self_data.get("kills", 0),
            "region": region_name,
            "alive_count": alive_count,
            "inventory": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                          for i in inv if isinstance(i, dict)],
            "enemies": [{"name": e.get("name","?"), "hp": e.get("hp","?"), "id": e.get("id","")}
                        for e in enemies[:8]],
            "region_items": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                             for i in region_items[:10]],
        })

        # Map learning: after Map item used, learn from the expanded vision
        if self._map_just_used:
            self._map_just_used = False
            learn_from_map(view)
            log.info("🗺️ Map knowledge updated — DZ tracking active")

        # Continuous DZ tracking from every view
        _update_dz_knowledge(view)

        # Run strategy brain
        can_act = self.action_sender.can_send_cooldown_action()
        decision = decide_action(view, can_act)

        if decision is None:
            return  # No action needed now

        action_type = decision["action"]
        action_data = decision.get("data", {})
        reason = decision.get("reason", "")

        # Check if cooldown action is allowed
        if action_type in COOLDOWN_ACTIONS and not can_act:
            log.debug("Cooldown active — skipping %s", action_type)
            return

        # Build and send per actions.md envelope spec
        payload = self.action_sender.build_action(
            action_type, action_data, reason, action_type,
        )

        await self._send(payload)
        log.info("→ %s | %s", action_type.upper(), reason)

        # Feed dashboard with action
        dashboard_state.update_agent(self.dashboard_key, {"last_action": f"{action_type}: {reason[:60]}"})
        dashboard_state.add_log(f"{action_type}: {reason[:80]}", "info", self.dashboard_key)

    async def _send(self, payload: dict):
        """Send a message through WebSocket with rate limiting."""
        if self.ws is None:
            return
        await ws_limiter.acquire()
        await self.ws.send(json.dumps(payload))

    async def _ping_loop(self):
        """Send ping every 15s to keep connection alive per api-summary.md."""
        try:
            while self._running:
                await asyncio.sleep(15)
                if self.ws:
                    await self._send({"type": "ping"})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Ping loop error: %s", e)
"""
Per game-loop.md §9 Message Types:
| Type              | Key Fields                                           |
|-------------------|------------------------------------------------------|
| agent_view        | gameId, agentId, status, view, reason?               |
| turn_advanced     | turn, view                                           |
| action_result     | success, data?, error?, canAct, cooldownRemainingMs  |
| can_act_changed   | canAct: true, cooldownRemainingMs: 0                 |
| event             | eventType, ...payload                                |
| game_ended        | gameId, agentId                                      |
| waiting           | gameId, agentId, message                             |
| pong              | —                                                    |
"""
