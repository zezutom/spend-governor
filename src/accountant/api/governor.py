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
PHASE_DWELL = 1.2  # hold a real phase briefly so the loop indicator is legible

# The visible mind: one cognitive loop the agent runs, current step lit in zone 1.
# OBSERVE (read live Phoenix traffic) → DIAGNOSE (where money leaks) → DECIDE
# (choose a lever, judge safe vs answer-affecting) → ACT (enact / escalate) →
# VERIFY (re-measure from Phoenix, prove the delta with quality held) → back to
# OBSERVE. The step rides on every snapshot so the loop reflects the live position.
STEPS = ["OBSERVE", "DIAGNOSE", "DECIDE", "ACT", "VERIFY"]
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
        self.verify: dict | None = None  # last VERIFY result (Phoenix-measured delta)
        self.step = "OBSERVE"            # current position in the mind loop
        self.rate_overrides: dict[str, float] = {}  # operator tool-rate edits (debugger)
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

    def rates(self) -> dict:
        """The tool rates in force = operator defaults (TOOL_PRICES) with any
        debugger edits applied. Used everywhere cost is computed, so an edited
        rate recomputes $/message globally — not just in the debugger."""
        return {**service.default_tool_rates(), **self.rate_overrides}

    async def set_tool_rate(self, tool: str, rate: float) -> None:
        async with self._lock:
            self.rate_overrides[tool] = max(0.0, float(rate))
            await self._emit("user", f"You set the {tool} rate to ${float(rate):.4f}/call.")

    # --- snapshot (everything the canvas + counters render) ----------------
    def _ctx(self):
        rates = self.rates()
        live = service.live_state()
        recs = service.recommendations()
        _, totals = service.cost_breakdown(live, recs, rates)
        return rates, totals

    def _monthly(self, lever, rates, totals) -> float:
        return service.policy_monthly_saving(lever["issue"], rates, self.volume, totals["total_n"])

    def snapshot(self) -> dict:
        rates = self.rates()
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
            "step": self.step,
            "steps": STEPS,
            "verify": self.verify,
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

    async def _emit(self, kind: str | None, text: str | None = None,
                    step: str | None = None) -> None:
        if step:
            self.step = step
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
            self.verify = None
            self._held = False
            await self._emit("thinking", "Reading the live traffic to see where the money's going…",
                             step="OBSERVE")
            await self._decide()
            for i, o in enumerate(self.observations):
                await self._emit("thinking", o, step="DIAGNOSE" if i == 0 else None)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            async with self._lock:
                if not self.plan:            # reasoning failed earlier — retry
                    await self._decide()
                await self._advance()

    async def _advance(self) -> None:
        """One autonomous beat: DECIDE the next lever, then ACT — auto-apply if
        SAFE, escalate if answer-affecting — then VERIFY the delta from Phoenix.
        Roadmap is never enacted; then it holds."""
        active = {p["signature"] for p in service.active_policies()}
        by = {l["signature"]: l for l in service.levers()}

        def _ready(step):
            lv = by.get(step["lever"])
            if not lv or not lv["enactable"] or step["lever"] in active or step["lever"] in self.vetoed:
                return None
            return lv

        # SAFE-FIRST: apply every safe (output-preserving) lever before deferring
        # any answer-affecting one — so the arc plays cache, cache, then the single
        # pivot, never interleaved. The phase steps are REAL — DECIDE while judging,
        # ACT while activating, VERIFY while re-measuring — held briefly apart only
        # so each is legible in the loop indicator (not a timer cycling phases).
        for step in self.plan:
            lv = _ready(step)
            if lv and lv["safe"]:
                await self._emit("thinking", None, step="DECIDE")          # deciding: judged safe
                await asyncio.sleep(PHASE_DWELL)
                await self._emit("thinking", None, step="ACT")             # acting: enact it
                await asyncio.to_thread(service.activate_policy, step["lever"], lv["policy_type"], lv["params"])
                self._held = False
                await self._emit("applied", step["reason"], step="ACT")
                await asyncio.sleep(PHASE_DWELL)
                await self._verify(step["lever"], lv)                      # verifying: re-measure
                return
        # then the pivot: escalate the first answer-affecting lever for a human call
        for step in self.plan:
            lv = _ready(step)
            if lv and not lv["safe"] and step["lever"] not in self.escalated:
                self.escalated.add(step["lever"])
                self._held = False
                await self._emit("escalate", step["reason"]
                                 + " This one can change the answer, so I'll leave the call to you.",
                                 step="DECIDE")
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
            await self._emit("holding", msg, step="OBSERVE")  # back to watching the traffic

    # --- VERIFY: re-measure the delta from Phoenix after an enact ----------
    async def _verify(self, sig: str, lv: dict) -> None:
        """The loop's last step. After enacting, re-measure $/message from the
        live data (a real recompute, not a scripted line) and attach the
        trace-level before/after evidence. Caching levers carry the captured
        Phoenix pair; the same-answer claim is shown ONLY when genuinely true."""
        rates, totals = self._ctx()
        baseline_dpm = totals["cost_per_ticket"]
        saved = sum(self._monthly(l, rates, totals) for l in service.levers()
                    if l["active"] and l["enactable"])
        governed_dpm = max(baseline_dpm - (saved / self.volume if self.volume else 0), 0.0)
        is_cache = lv["policy_type"] == "cache_tool"
        pair = service.captured_trace_pair() if is_cache else None
        same_answer = bool(pair and pair.get("same_answer"))
        self.verify = {
            "sig": sig, "title": lv["title"],
            "kind": "cache" if is_cache else "route",
            "baseline_dollars_per_message": round(baseline_dpm, 6),
            "dollars_per_message": round(governed_dpm, 6),
            "monthly_saving": round(self._monthly(lv, rates, totals), 2),
            "same_answer": same_answer,            # claimed only when truly equal
            "pair": pair,                           # real captured before/after (cache)
            "phoenix_url": (pair or {}).get("governed", {}).get("phoenix_url")
            if pair else service.span_deeplink(service.project_gid(), None, None),
            "measured_in_phoenix": True,
        }
        # Honest narration: state the measured $/message move; assert answer-equal
        # only when the captured pair proves it.
        delta = baseline_dpm - governed_dpm
        line = (f"Re-measured from the same traffic in Phoenix: $/message "
                f"${baseline_dpm:.4f} → ${governed_dpm:.4f}")
        if same_answer:
            line += ", and the answer comes back identical — quality held."
        else:
            line += ". Watching the next traces to confirm quality holds."
        await self._emit("verified", line, step="VERIFY")

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

    # --- promote a debug-session config to PRODUCTION ----------------------
    async def apply_from_debug(self, use_case: str, cache: bool, economy: bool,
                               evidence: dict | None = None) -> dict:
        """The debugger's one sanctioned crossing into production: activate the
        chosen real levers for this use case (deactivate the ones turned off),
        then write the decision to the inbox — with the agent's grounded advisory
        if a risky (answer-affecting) lever is included. Non-blocking: applied at
        the operator's direction; the agent then watches live (trip lifecycle)."""
        async with self._lock:
            applied, removed = [], []
            for sig in _CLASS_LEVERS.get(use_case, []):
                lv = next((l for l in service.levers() if l["signature"] == sig), None)
                if not lv or not lv["enactable"]:
                    continue
                want = cache if lv["policy_type"] == "cache_tool" else economy
                if want and not lv["active"]:
                    await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
                    self.vetoed.discard(sig); self.escalated.discard(sig)
                    applied.append(lv["title"])
                elif want and lv["active"]:
                    applied.append(lv["title"])
                elif (not want) and lv["active"]:
                    await asyncio.to_thread(service.deactivate_policy, sig)
                    removed.append(lv["title"])
            label = _CLASS_LABEL.get(use_case, use_case)
            await self._emit("user", f"You applied a debug-session config to {label}.")
            parts = []
            if applied:
                parts.append("now running " + " + ".join(t.lower() for t in applied))
            if removed:
                parts.append("turned off " + " + ".join(t.lower() for t in removed))
            ack = f"Applied from your debug session on {label.lower()}: {('; '.join(parts)) or 'no change'}."
            ev = evidence or {}
            if economy and ev.get("held_pct") is not None:
                ack += (f" Load test held {round(ev['held_pct'] * 100)}% on {ev.get('n', '?')} replays — "
                        f"I'd keep premium, but it's your call. Applied; I'm watching live and I'll flag "
                        f"the moment quality slips.")
            elif applied:
                ack += " Output-preserving — safe; I'll keep watching the traffic."
            await self._emit("applied", ack)
        asyncio.create_task(self._reason_async())
        return {"applied": applied, "removed": removed}


governor = Governor()
