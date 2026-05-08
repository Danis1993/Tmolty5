"""
WebSocket gameplay engine — wss://cdn.moltyroyale.com/ws/join.
Core loop: connect → process messages → decide → act → repeat.
"""

import json
import asyncio
import websockets

from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import (
    ActionSender,
    COOLDOWN_ACTIONS,
)
from bot.strategy.brain import (
    decide_action,
    reset_game_state,
    learn_from_map,
)
from bot.dashboard.state import dashboard_state
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


def _update_dz_knowledge(view: dict):
    """Track death zones continuously."""
    from bot.strategy.brain import _map_knowledge

    for region in view.get("visibleRegions", []):
        if isinstance(region, dict) and region.get("isDeathZone"):
            rid = region.get("id")
            if rid:
                _map_knowledge["death_zones"].add(rid)

    for conn in view.get("connectedRegions", []):
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            rid = conn.get("id")
            if rid:
                _map_knowledge["death_zones"].add(rid)

    cur = view.get("currentRegion", {})
    if isinstance(cur, dict) and cur.get("isDeathZone"):
        rid = cur.get("id")
        if rid:
            _map_knowledge["death_zones"].add(rid)

    for dz in view.get("pendingDeathzones", []):
        if isinstance(dz, dict):
            rid = dz.get("id")
            if rid:
                _map_knowledge["death_zones"].add(rid)
        elif isinstance(dz, str):
            _map_knowledge["death_zones"].add(dz)


class WebSocketEngine:
    """Main gameplay websocket engine."""

    def __init__(self, game_id: str, agent_id: str):
        self.game_id = game_id
        self.agent_id = agent_id

        self.ws = None
        self.game_result = None
        self.last_view = None

        self._running = False
        self._ping_task = None
        self._map_just_used = False

        self.action_sender = ActionSender()

        self.dashboard_key = agent_id
        self.dashboard_name = "Agent"

    async def run(self) -> dict:
        """Main websocket loop."""

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
                    ping_interval=None,
                    max_size=2**20,
                ) as ws:

                    self.ws = ws
                    retry_count = 0

                    log.info(
                        "✅ WebSocket connected for game=%s",
                        self.game_id
                    )

                    # hello packet
                    await ws.send(json.dumps({
                        "type": "hello",
                        "entryType": "free"
                    }))

                    # start ping loop
                    self._ping_task = asyncio.create_task(
                        self._ping_loop()
                    )

                    async for raw_msg in ws:

                        try:
                            msg = json.loads(raw_msg)

                            if not isinstance(msg, dict):
                                continue

                            msg_type = msg.get("type", "unknown")

                            log.debug(
                                "WS recv: type=%s",
                                msg_type
                            )

                            result = await self._handle_message(msg)

                            if result is not None:
                                self._running = False
                                return result

                        except json.JSONDecodeError:
                            log.warning(
                                "Invalid JSON message: %s",
                                raw_msg[:100]
                            )

            except websockets.exceptions.ConnectionClosed as e:

                retry_count += 1

                log.warning(
                    "WebSocket closed: %s | retry %d/%d",
                    e,
                    retry_count,
                    max_retries
                )

                if self._ping_task:
                    self._ping_task.cancel()

                await asyncio.sleep(
                    min(2 ** retry_count, 30)
                )

            except Exception as e:

                retry_count += 1

                log.error(
                    "WebSocket error: %s | retry %d/%d",
                    e,
                    retry_count,
                    max_retries
                )

                if self._ping_task:
                    self._ping_task.cancel()

                await asyncio.sleep(
                    min(2 ** retry_count, 30)
                )

        return self.game_result or {
            "status": "disconnected"
        }

    async def _handle_message(self, msg: dict):

        msg_type = msg.get("type", "")

        # ===============================
        # AGENT VIEW
        # ===============================
        if msg_type == "agent_view":

            view = msg.get("view") or {}

            if isinstance(view, dict):

                self.last_view = view

                await self._on_agent_view(view)

        # ===============================
        # TURN ADVANCED
        # ===============================
        elif msg_type == "turn_advanced":

            view = msg.get("view")

            if not view and isinstance(msg.get("data"), dict):
                view = msg["data"].get("view")

            if view and isinstance(view, dict):

                self.last_view = view

                await self._on_agent_view(view)

        # ===============================
        # ACTION RESULT
        # ===============================
        elif msg_type == "action_result":

            self.action_sender.can_act = msg.get(
                "canAct",
                self.action_sender.can_act
            )

            self.action_sender.cooldown_remaining_ms = msg.get(
                "cooldownRemainingMs",
                0
            )

            success = msg.get("success", False)

            if success:
                log.info("Action success")
            else:
                log.warning(
                    "Action failed: %s",
                    msg.get("error")
                )

        # ===============================
        # CAN ACT CHANGED
        # ===============================
        elif msg_type == "can_act_changed":

            self.action_sender.can_act = msg.get(
                "canAct",
                True
            )

            self.action_sender.cooldown_remaining_ms = msg.get(
                "cooldownRemainingMs",
                0
            )

            if self.last_view and msg.get("canAct"):
                await self._on_agent_view(
                    self.last_view
                )

        # ===============================
        # GAME ENDED
        # ===============================
        elif msg_type == "game_ended":

            log.info("═══ GAME ENDED ═══")

            reset_game_state()

            self.game_result = msg

            return msg

        # ===============================
        # WAITING
        # ===============================
        elif msg_type == "waiting":

            log.info("Waiting for players...")

        # ===============================
        # EVENT
        # ===============================
        elif msg_type == "event":

            log.debug(
                "Event: %s",
                msg.get("eventType")
            )

        # ===============================
        # PONG
        # ===============================
        elif msg_type == "pong":
            pass

        # ===============================
        # ERROR
        # ===============================
        elif msg_type == "error":

            log.error(
                "FULL SERVER ERROR: %s",
                json.dumps(msg, indent=2)
            )

        else:

            log.info(
                "Unknown WS message: %s",
                msg_type
            )

        return None

    async def _on_agent_view(self, view: dict):

        if not isinstance(view, dict):
            return

        self_data = view.get("self", {})

        if not isinstance(self_data, dict):
            return

        if not self_data.get("isAlive", True):

            log.info("☠️ Agent dead")

            return

        hp = self_data.get("hp", 0)
        ep = self_data.get("ep", 0)

        region = view.get("currentRegion", {})
        region_name = region.get("name", "?")

        log.info(
            "HP=%s EP=%s Region=%s",
            hp,
            ep,
            region_name
        )

        # Learn map
        if self._map_just_used:

            self._map_just_used = False

            learn_from_map(view)

            log.info("🗺️ Map knowledge updated")

        # Track DZ
        _update_dz_knowledge(view)

        # Brain decision
        can_act = self.action_sender.can_send_cooldown_action()

        decision = decide_action(view, can_act)

        if not decision:
            return

        action_type = decision["action"]
        action_data = decision.get("data", {})
        reason = decision.get("reason", "")

        if (
            action_type in COOLDOWN_ACTIONS
            and not can_act
        ):
            return

        payload = self.action_sender.build_action(
            action_type,
            action_data,
            reason,
            action_type,
        )

        await self._send(payload)

        log.info(
            "→ %s | %s",
            action_type.upper(),
            reason
        )

    async def _send(self, payload: dict):

        if not self.ws:
            return

        await ws_limiter.acquire()

        await self.ws.send(
            json.dumps(payload)
        )

    async def _ping_loop(self):

        try:
            while self._running:

                await asyncio.sleep(15)

                if self.ws:

                    await self._send({
                        "type": "ping"
                    })

        except asyncio.CancelledError:
            pass

        except Exception as e:

            log.debug(
                "Ping loop error: %s",
                e
                    )
