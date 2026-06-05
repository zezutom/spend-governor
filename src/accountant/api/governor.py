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
_SEC_PER_MONTH = 30 * 24 * 60 * 60
TICK_SECONDS = 5.0
_NODE_FOR = {"cache_tool:web_search": "tools", "cache_tool:kb_lookup": "tools",
             "route_model:simple": "model"}
# Which levers govern each task class (so the class boxes light up when governed).
_CLASS_LEVERS = {
    "refund_handling": ["cache_tool:web_search"],
    "account_question": ["cache_tool:kb_lookup", "route_model:simple"],
    "password_reset": ["route_model:simple"],
    "plan_change": [],
}
_CLASS_LABEL = {"refund_handling": "Refund tickets", "account_question": "Account questions",
                "password_reset": "Password resets", "plan_change": "Plan changes"}
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
        rates = service.default_tool_rates()
        live = service.live_state()
        recs = service.recommendations()
        rows, totals = service.cost_breakdown(live, recs, rates)
        gross = totals["cost_per_ticket"] * self.volume
        active_sigs = {p["signature"] for p in service.active_policies()}

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

        # The workload lanes: each conversation type with its real cost + the
        # operations it runs (which tools, how often, and the lever on each).
        by = live.get("by_task_class") or {}
        _TOOL_LEVER = {"web_search": "cache_tool:web_search", "kb_lookup": "cache_tool:kb_lookup"}
        _SIMPLE = {"password_reset", "account_question"}
        classes = []
        for r in rows:
            tc = r["tc"]
            if tc == "unknown":
                continue
            counts = (by.get(tc) or {}).get("avg_tool_counts") or {}
            ops = []
            for tool, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
                if tool in ("task_classifier", "(merged tools)") or cnt < 0.5:
                    continue
                lever = _TOOL_LEVER.get(tool)
                ops.append({"op": tool, "count": round(cnt, 1), "kind": "tool",
                            "lever": lever, "governed": bool(lever and lever in active_sigs)})
            model_lever = "route_model:simple" if tc in _SIMPLE else None
            ops.append({"op": "model", "count": None, "kind": "model", "lever": model_lever,
                        "governed": bool(model_lever and model_lever in active_sigs)})
            govs = _CLASS_LEVERS.get(tc, [])
            classes.append({
                "tc": tc, "label": _CLASS_LABEL.get(tc, tc), "cost_per_ticket": round(r["cost"], 5),
                "share": round(r["share"], 3), "mult": round(r["mult"], 1), "baseline": r["is_base"],
                "governed": (any(s in active_sigs for s in govs) if govs else None), "ops": ops,
            })
        classes.sort(key=lambda c: -c["share"])

        # FOCAL PAIR — throughput (messages/sec) + $/message. $/message is what
        # governance actually moves: measured baseline cost per ticket, minus the
        # per-ticket saving from the levers governing now. Throughput is the
        # operator's volume as a rate (the same projection basis as burn; becomes
        # the real live rate once traffic streams). Burn is demoted to a small
        # confirming readout.
        msgs_per_sec = self.volume / _SEC_PER_MONTH
        baseline_dpm = totals["cost_per_ticket"]
        saved_per_msg = (saved / self.volume) if self.volume else 0.0
        governed_dpm = max(baseline_dpm - saved_per_msg, 0.0)

        return {
            "throughput_per_sec": round(msgs_per_sec, 3),
            "dollars_per_message": round(governed_dpm, 6),
            "baseline_dollars_per_message": round(baseline_dpm, 6),
            "burn_per_min": round(max(gross - saved, 0.0) / _MIN_PER_MONTH, 4),  # small confirming readout
            "burn_rate": round(max(gross - saved, 0.0) / _MIN_PER_MONTH, 5),     # kept for compat
            "gross_burn": round(gross / _MIN_PER_MONTH, 5),
            "volume": self.volume,
            "active_count": service.policies_active_count(),
            "realized_savings": round(service.realized_savings().get("total_savings_usd", 0) or 0, 4),
            "levers": levers,
            "classes": classes,
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
        self._hb = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        """Push the live snapshot on a steady beat so the cockpit reflects the
        underlying data continuously — burn rate, class costs, savings — even
        between agent actions and as new traffic streams in."""
        while True:
            await asyncio.sleep(3.0)
            await self._emit(None)  # state only, no narration

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
        # Nothing left to auto-apply. Say so ONCE (with context — what's done and
        # what's left), then idle; the loop keeps running and resumes the moment
        # the human reopens work (veto / re-enable).
        if not self._held:
            self._held = True
            done = [l["title"].lower() for l in service.levers() if l["active"] and l["enactable"]]
            esc = [l["title"].lower() for l in service.levers()
                   if l["signature"] in self.escalated and not l["active"]]
            done_txt = (" and ".join(done) if len(done) <= 2 else
                        ", ".join(done[:-1]) + f", and {done[-1]}") if done else "nothing yet"
            if esc:
                msg = (f"I've handled the safe wins ({done_txt}). The one thing left — "
                       f"{' and '.join(esc)} — can change the answer, so I've flagged it on the "
                       f"canvas for your call. Holding otherwise.")
            else:
                msg = (f"I've cached {done_txt}. That's the safe limit — going further would start "
                       f"to risk answer quality, so I'm holding here.")
            await self._emit("holding", msg)

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
            title = lv["title"] if lv else "that lever"
            was_active = lv and lv["active"]
            monthly = self._monthly(lv, rates, totals) if lv else 0.0
            await asyncio.to_thread(service.deactivate_policy, sig)
            self.vetoed.add(sig); self.escalated.discard(sig)
            if was_active and monthly >= _PUSHBACK_MIN_USD and lv:
                self.pushback = {"sig": sig, "title": title, "monthly": round(monthly, 0)}
            await self._emit("user", f"You vetoed {title}.")          # your turn
            facts = f"Burn back to {self._fmt(self._burn_now())}"
            if monthly:
                facts += f" — that's ~${monthly:,.0f}/mo of waste again"
            await self._emit("reaction", facts + ".")                 # instant facts
        asyncio.create_task(self._reason_async())                     # next-step reasoning

    async def enable(self, sig: str) -> None:
        async with self._lock:
            self.vetoed.discard(sig)
            if self.pushback and self.pushback.get("sig") == sig:
                self.pushback = None
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            title = lv["title"] if lv else "that lever"
            if lv and lv["safe"]:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            await self._emit("user", f"You re-enabled {title}.")
            await self._emit("reaction", f"Back on — burn down to {self._fmt(self._burn_now())}.")
        asyncio.create_task(self._reason_async())

    async def accept(self, sig: str) -> None:
        async with self._lock:
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            title = lv["title"] if lv else "that lever"
            if lv:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            self.escalated.discard(sig)
            await self._emit("user", f"You accepted {title}.")
            await self._emit("reaction", f"Routing live — burn down to {self._fmt(self._burn_now())}.")
        asyncio.create_task(self._reason_async())

    async def reject(self, sig: str) -> None:
        async with self._lock:
            self.escalated.discard(sig); self.vetoed.add(sig)
            lv = next((l for l in service.levers() if l["signature"] == sig), None)
            title = lv["title"] if lv else "that lever"
            await self._emit("user", f"You rejected {title}.")
            await self._emit("reaction", "Leaving that one off, then.")
        asyncio.create_task(self._reason_async())


governor = Governor()
