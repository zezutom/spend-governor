"""The autonomous governor — drives the cockpit's agent loop, pushes SSE events.

The agent reasons over live cost (optimizer.agent), AUTO-APPLIES safe levers on
its own clock, ESCALATES risky (answer-affecting) ones for a human decision, and
re-reasons instantly when the human acts on the canvas (veto / re-enable /
accept / reject). Every figure comes from `accountant.service`; the agent's prose
carries none. Blocking work (the LLM call, activate_policy) runs in a thread so
the event loop and SSE stay responsive.

Events pushed to subscribers: {seq, ts, narration:{text,kind}|null, state}, where
`state` is the full cockpit snapshot (burn rate, levers with active/safe/vetoed/
escalated, roadmap, pushback) the canvas renders.
"""

import asyncio
import time

from accountant import service
from accountant.optimizer import agent

_MIN_PER_MONTH = 30 * 24 * 60
TICK_SECONDS = 5.0
_NODE_FOR = {"cache_tool:web_search": "tools", "cache_tool:kb_lookup": "tools",
             "route_model:simple": "model"}
# A correction worth pushing back on: vetoing a lever that's giving up this much
# measured monthly saving. Grounded, not nagging.
_PUSHBACK_MIN_USD = 1000.0


class Governor:
    def __init__(self, volume: int = 4_000_000):
        self.subscribers: set[asyncio.Queue] = set()
        self.vetoed: set[str] = set()
        self.escalated: set[str] = set()
        self.plan: list[dict] = []
        self.observations: list[str] = []
        self.pushback: dict | None = None
        self.volume = volume
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._held = False  # gates the one-time "holding" line; loop never dies

    # --- subscriptions -----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    # --- snapshot (everything the canvas + counters render) ----------------
    def _ctx(self):
        rates = service.default_tool_rates()
        live = service.live_state()
        recs = service.recommendations()
        _, totals = service.cost_breakdown(live, recs, rates)
        return rates, totals

    def _monthly(self, lever, rates, totals) -> float:
        return service.policy_monthly_saving(lever["issue"], rates, self.volume, totals["total_n"])

    def snapshot(self) -> dict:
        rates, totals = self._ctx()
        gross = totals["cost_per_ticket"] * self.volume
        levers, saved = [], 0.0
        for l in service.levers():
            if not l["enactable"]:
                continue
            m = self._monthly(l, rates, totals)
            if l["active"]:
                saved += m
            levers.append({
                "sig": l["signature"], "title": l["title"], "type": l["policy_type"],
                "node": _NODE_FOR.get(l["signature"]), "active": l["active"],
                "safe": l["safe"], "vetoed": l["signature"] in self.vetoed,
                "escalated": l["signature"] in self.escalated,
                "monthly": round(m, 2),
            })
        return {
            "burn_rate": round(max(gross - saved, 0.0) / _MIN_PER_MONTH, 5),
            "gross_burn": round(gross / _MIN_PER_MONTH, 5),
            "volume": self.volume,
            "active_count": service.policies_active_count(),
            "realized_savings": round(service.realized_savings().get("total_savings_usd", 0) or 0, 4),
            "levers": levers,
            "roadmap": service.roadmap_capabilities(),
            "pushback": self.pushback,
            "holding": self._holding(),
        }

    def _holding(self) -> bool:
        active = {p["signature"] for p in service.active_policies()}
        for l in service.levers():
            if l["enactable"] and l["signature"] not in active \
                    and l["signature"] not in self.vetoed and l["signature"] not in self.escalated:
                return False
        return True

    async def _emit(self, kind: str | None, text: str | None = None) -> None:
        self._seq += 1
        ev = {"seq": self._seq, "ts": time.time(),
              "narration": ({"text": text, "kind": kind} if text else None),
              "state": self.snapshot()}
        for q in list(self.subscribers):
            await q.put(ev)

    # --- reasoning (LLM, off-thread) ---------------------------------------
    async def _decide(self) -> bool:
        """Reason (LLM, off-thread). Resilient: a transient model error never
        crashes the loop — the agent just retries on the next beat."""
        try:
            dec = await asyncio.to_thread(agent.decide, list(self.vetoed))
            self.plan = [{"lever": s.lever, "reason": s.reason} for s in dec.plan]
            self.observations = list(dec.observations)
            return True
        except Exception:
            if not self.observations:
                self.observations = ["Still waking up — the model's busy, I'll try again in a moment."]
            return False

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.reset()
        self._task = asyncio.create_task(self._loop())

    async def reset(self) -> None:
        async with self._lock:
            for p in service.active_policies():
                await asyncio.to_thread(service.deactivate_policy, p["signature"])
            self.vetoed.clear(); self.escalated.clear(); self.pushback = None
            self.plan, self.observations = [], []
            self._held = False
            await self._emit("thinking", "Reading the live traffic to see where the money's going…")
            await self._decide()
            for o in self.observations:
                await self._emit("thinking", o)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            async with self._lock:
                if not self.plan:            # reasoning failed earlier — retry
                    await self._decide()
                await self._advance()

    async def _advance(self) -> None:
        """One autonomous beat: auto-apply the next SAFE lever, or escalate the
        next RISKY one. Roadmap is never enacted; then it holds."""
        active = {p["signature"] for p in service.active_policies()}
        by = {l["signature"]: l for l in service.levers()}
        for step in self.plan:
            sig = step["lever"]; lv = by.get(sig)
            if not lv or not lv["enactable"] or sig in active or sig in self.vetoed:
                continue
            if lv["safe"]:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
                self._held = False
                await self._emit("applied", step["reason"])
                return
            if sig not in self.escalated:
                self.escalated.add(sig)
                self._held = False
                await self._emit("escalate", step["reason"]
                                 + " This one can change the answer, so I'll leave the call to you.")
                return
        # Nothing left to auto-apply. Say so ONCE, then idle — the loop keeps
        # running so it resumes the moment the human reopens work (veto/re-enable).
        if not self._held:
            self._held = True
            await self._emit("holding", "That's the safe limit — cutting further would start to "
                             "risk answer quality, so I'm holding here.")

    # --- human turns (canvas actions) -------------------------------------
    # DECOUPLED: the facts of the reaction (recomputed burn, lever, $ cost) emit
    # INSTANTLY from the service layer; the LLM wording lands as a second beat on
    # an already-updated inbox. The reaction never waits on the model.
    def _burn_now(self) -> float:
        rates, totals = self._ctx()
        gross = totals["cost_per_ticket"] * self.volume
        saved = sum(self._monthly(l, rates, totals) for l in service.levers()
                    if l["active"] and l["enactable"])
        return max(gross - saved, 0.0) / _MIN_PER_MONTH

    @staticmethod
    def _fmt(burn: float) -> str:
        return f"${burn:.2f}/min" if burn >= 0.1 else f"${burn:.4f}/min"

    async def _reason_async(self) -> None:
        """Second beat: the agent's voice, after the facts already rendered."""
        async with self._lock:
            if await self._decide() and self.observations:
                await self._emit("reasoned", self.observations[0])

    async def veto(self, sig: str) -> None:
        async with self._lock:
            rates, totals = self._ctx()
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            was_active = lv and lv["active"]
            monthly = self._monthly(lv, rates, totals) if lv else 0.0
            await asyncio.to_thread(service.deactivate_policy, sig)
            self.vetoed.add(sig); self.escalated.discard(sig)
            if was_active and monthly >= _PUSHBACK_MIN_USD and lv:
                self.pushback = {"sig": sig, "title": lv["title"], "monthly": round(monthly, 0)}
            facts = f"{lv['title']} off — burn back to {self._fmt(self._burn_now())}" if lv else "lever off"
            if monthly:
                facts += f"; ~${monthly:,.0f}/mo of waste returns"
            await self._emit("reaction", facts + ".")  # INSTANT — facts + recomputed state
        asyncio.create_task(self._reason_async())

    async def enable(self, sig: str) -> None:
        async with self._lock:
            self.vetoed.discard(sig)
            if self.pushback and self.pushback.get("sig") == sig:
                self.pushback = None
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            if lv and lv["safe"]:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            tail = f"{lv['title']} back on — burn down to {self._fmt(self._burn_now())}." if lv else "back on."
            await self._emit("reaction", tail)
        asyncio.create_task(self._reason_async())

    async def accept(self, sig: str) -> None:
        async with self._lock:
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            if lv:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            self.escalated.discard(sig)
            await self._emit("reaction", f"Okayed — routing live, burn down to {self._fmt(self._burn_now())}.")
        asyncio.create_task(self._reason_async())

    async def reject(self, sig: str) -> None:
        async with self._lock:
            self.escalated.discard(sig); self.vetoed.add(sig)
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            await self._emit("reaction", f"Leaving {lv['title'].lower() if lv else 'that'} off.")
        asyncio.create_task(self._reason_async())


governor = Governor()
