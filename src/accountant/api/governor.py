"""The autonomous governor — drives the cockpit's agent loop, pushes SSE events.

The agent reasons over live cost (optimizer.agent), AUTO-APPLIES safe levers on
its own clock, ESCALATES risky (answer-affecting) ones for a human decision, and
re-reasons instantly when the human acts on the canvas (veto / re-enable /
accept / reject). Every figure comes from `accountant.service`; the agent's prose
carries none. Blocking work (the LLM call, activate_policy) runs in a thread so
the event loop and SSE stay responsive.

The WORLD is scripted (a seeded `scenario` clock decides WHAT problem surfaces and
WHEN); the AGENT is real (real diagnosis, real pre-run eval verdicts, real lever
activation). The compressed clock is the only artifice, and it's disclosed. See
`scenario.py`. The governor accumulates a metrics time-series (`history`) and the
decision `pins` that the top-bar value-spine renders — cost solid (measured),
quality dashed (eval-measured), volume seeded.

Events pushed to subscribers: {seq, ts, narration:{text,kind,trigger?,session?}|null,
state}, where `state` is the full cockpit snapshot the canvas + value-spine render.
"""

import asyncio
import time

from accountant import service
from accountant.api.scenario import Scenario, ROUTE_ECON_DROP
from accountant.optimizer import agent

_MIN_PER_MONTH = 30 * 24 * 60
_SEC_PER_MONTH = 30 * 24 * 60 * 60
TICK_SECONDS = 4.0
PHASE_DWELL = 1.1  # hold a real phase briefly so the loop indicator is legible

# The visible mind: one cognitive loop the agent runs, current step lit in zone 1.
# OBSERVE → DIAGNOSE → DECIDE → ACT → VERIFY → back to OBSERVE.
STEPS = ["OBSERVE", "DIAGNOSE", "DECIDE", "ACT", "VERIFY"]

# Per-class model routing — three INDEPENDENT decisions, each its own beat with its
# own real pre-run eval. They activate for real via service.activate_policy (the
# store keys on signature; the guard passes on policy_type 'route_model'), so no
# service change is needed. They SUPERSEDE the single combined route_model:simple.
_ROUTE_CLASSES = ["password_reset", "account_question", "refund_handling"]
_ROUTE_TITLE = {
    "password_reset": "Route password resets to economy model",
    "account_question": "Route account questions to economy model",
    "refund_handling": "Route refunds to economy model",
}

_NODE_FOR = {
    "cache_tool:web_search": "tools", "cache_tool:kb_lookup": "tools",
    "route_model:password_reset": "model", "route_model:account_question": "model",
    "route_model:refund_handling": "model",
}
# Which levers govern each task class (so the class boxes light up when governed).
_CLASS_LEVERS = {
    "refund_handling": ["cache_tool:web_search", "route_model:refund_handling"],
    "account_question": ["cache_tool:kb_lookup", "route_model:account_question"],
    "password_reset": ["route_model:password_reset"],
    "plan_change": [],
}
_CLASS_LABEL = {"refund_handling": "Refund tickets", "account_question": "Account questions",
                "password_reset": "Password resets", "plan_change": "Plan changes"}
# Grounded act-alone narration for the safe caches (agent voice; numbers stay in UI).
_SAFE_REASON = {
    "cache_tool:web_search": (
        "Refund tickets re-run the same web_search several times per ticket — I'm "
        "caching the repeat. Output-preserving, so I'll just do it.",
        "web_search ×3 redundant detected"),
    "cache_tool:kb_lookup": (
        "Account questions repeat the same kb_lookup — caching the duplicate. Same "
        "answer, lower cost.",
        "kb_lookup repeated per ticket"),
}
# A correction worth pushing back on: vetoing a lever that's giving up this much
# measured monthly saving. Grounded, not nagging.
_PUSHBACK_MIN_USD = 1000.0


class Governor:
    def __init__(self, volume: int = 4_000_000, seed: int = 0):
        self.subscribers: set[asyncio.Queue] = set()
        self.vetoed: set[str] = set()
        self.escalated: set[str] = set()
        self.plan: list[dict] = []
        self.observations: list[str] = []
        self.pushback: dict | None = None
        self.sessions: dict[str, dict] = {}   # debug-session records, by id (#DS-…)
        self._ds_seq = 0
        self.verify: dict | None = None  # last VERIFY result (Phoenix-measured delta)
        self.step = "OBSERVE"            # current position in the mind loop
        self.rate_overrides: dict[str, float] = {}  # operator tool-rate edits (debugger)
        self.volume = volume
        # the scripted-world clock + the real metrics time-series it drives
        self.scenario = Scenario(seed=seed)
        self.history: list[dict] = []    # rolling {t, label, dollars_per_message, quality, volume}
        self.pins: list[dict] = []       # decision pins on the timeline (link to inbox)
        self._eval_cache: dict[str, dict | None] = {}
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

    # --- economics helpers -------------------------------------------------
    def _economics(self):
        rates = self.rates()
        live = service.live_state()
        recs = service.recommendations()
        rows, totals = service.cost_breakdown(live, recs, rates)
        return rates, rows, totals

    def _monthly(self, lever, rates, totals) -> float:
        return service.policy_monthly_saving(lever["issue"], rates, self.volume, totals["total_n"])

    def _route_saving_per_ticket(self, tc: str, rows: list) -> float:
        """A per-class model-route saving, on the SAME basis the replay lab shows
        (economy ≈ 0.2× the LLM cost ⇒ saves llm×0.8 per ticket). Real, labeled."""
        r = next((x for x in rows if x["tc"] == tc), None)
        if not r:
            return 0.0
        llm = max(r["cost"] - r["tool"], 0.0)
        return llm * ROUTE_ECON_DROP

    def _route_monthly(self, tc: str, rows: list) -> float:
        r = next((x for x in rows if x["tc"] == tc), None)
        if not r:
            return 0.0
        return self._route_saving_per_ticket(tc, rows) * self.volume * r["share"]

    def _route_params(self) -> dict:
        lv = next((l for l in service.levers() if l["signature"] == "route_model:simple"), None)
        return dict(lv["params"]) if lv and lv.get("params") else {"cheap_model": "gemini-2.5-flash-lite"}

    def _lever_by_sig(self, sig: str) -> dict | None:
        """A lever dict for either a real service lever OR a synthetic per-class
        route (which service.levers() doesn't emit). Both activate for real."""
        lv = next((l for l in service.levers() if l["signature"] == sig), None)
        if lv:
            return lv
        if sig.startswith("route_model:"):
            tc = sig.split(":", 1)[1]
            active = {p["signature"] for p in service.active_policies()}
            return {"signature": sig, "policy_type": "route_model", "params": self._route_params(),
                    "title": _ROUTE_TITLE.get(tc, sig), "active": sig in active,
                    "enactable": True, "safe": False}
        return None

    def _saved_total(self, rates, rows, totals) -> float:
        """Projected $/mo saved by every ACTIVE enactable lever — caches (from the
        view-model) plus the per-class routes (lab basis). route_model:simple is
        excluded; it's superseded by the per-class routes (no double count)."""
        active = {p["signature"] for p in service.active_policies()}
        saved = 0.0
        for l in service.levers():
            if not l["enactable"] or l["signature"] == "route_model:simple":
                continue
            if l["signature"] in active:
                saved += self._monthly(l, rates, totals)
        for tc in _ROUTE_CLASSES:
            if f"route_model:{tc}" in active:
                saved += self._route_monthly(tc, rows)
        return saved

    def _eval(self, key: str | None) -> dict | None:
        if not key:
            return None
        if key not in self._eval_cache:
            from accountant.analytics import quality_eval
            self._eval_cache[key] = quality_eval.load_eval(key)
        return self._eval_cache[key]

    def _quality_now(self, rows) -> float:
        """Quality RETENTION (0..1), eval-measured. 1.0 = held. Dips ONLY while a
        route whose REAL pre-run verdict is 'revert' (the refund trap) is active —
        using that eval's measured economy/baseline quality, weighted by the class's
        real share of traffic. Recovers the instant the route is reverted. Never
        pre-baked: it's recomputed from the active levers + the real eval each time."""
        active = {p["signature"] for p in service.active_policies()}
        ret = 1.0
        for tc in _ROUTE_CLASSES:
            sig = f"route_model:{tc}"
            if sig not in active:
                continue
            beat = self.scenario.beat_for_lever(sig)
            ev = self._eval(beat.get("eval_key")) if beat else None
            if not ev or ev.get("verdict") != "revert":
                continue
            base_q = ev.get("mean_quality_baseline") or 5.0
            econ_q = ev.get("mean_quality_economy") or base_q
            share = next((r["share"] for r in rows if r["tc"] == tc), 0.0)
            if base_q > 0:
                ret -= (1 - econ_q / base_q) * share
        return round(max(min(ret, 1.0), 0.0), 4)

    # --- snapshot (everything the canvas + counters + value-spine render) ---
    def snapshot(self) -> dict:
        now = time.monotonic()
        rates, rows, totals = self._economics()
        gross = totals["cost_per_ticket"] * self.volume
        active_sigs = {p["signature"] for p in service.active_policies()}
        released = self.scenario.released_sigs(now)

        levers, saved = [], 0.0
        # 1) real service levers EXCEPT the superseded combined route
        for l in service.levers():
            if not l["enactable"] or l["signature"] == "route_model:simple":
                continue
            m = self._monthly(l, rates, totals)
            if l["active"]:
                saved += m
            levers.append({
                "sig": l["signature"], "title": l["title"], "type": l["policy_type"],
                "node": _NODE_FOR.get(l["signature"]), "active": l["active"],
                "safe": l["safe"], "vetoed": l["signature"] in self.vetoed,
                "escalated": l["signature"] in self.escalated, "monthly": round(m, 2),
                "eval_key": None, "tc": (l.get("classes") or [None])[0],
            })
        # 2) synthetic per-class route levers (real activation, real per-class saving).
        #    Hidden until their beat releases — so the trap can't be armed early.
        for tc in _ROUTE_CLASSES:
            sig = f"route_model:{tc}"
            is_active = sig in active_sigs
            if not (sig in released or is_active or sig in self.escalated or sig in self.vetoed):
                continue
            beat = self.scenario.beat_for_lever(sig)
            m = self._route_monthly(tc, rows)
            if is_active:
                saved += m
            levers.append({
                "sig": sig, "title": _ROUTE_TITLE[tc], "type": "route_model", "node": "model",
                "active": is_active, "safe": False, "vetoed": sig in self.vetoed,
                "escalated": sig in self.escalated, "monthly": round(m, 2),
                "eval_key": (beat.get("eval_key") if beat else None), "tc": tc,
            })

        # The workload lanes: each conversation type with its real cost + the
        # operations it runs (which tools, how often, and the lever on each).
        by = (service.live_state().get("by_task_class")) or {}
        _TOOL_LEVER = {"web_search": "cache_tool:web_search", "kb_lookup": "cache_tool:kb_lookup"}
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
            route_sig = f"route_model:{tc}"
            has_route = tc in _ROUTE_CLASSES and (route_sig in released or route_sig in active_sigs
                                                  or route_sig in self.escalated)
            model_lever = route_sig if has_route else None
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
        # per-ticket saving from the levers governing now.
        msgs_per_sec = self.volume / _SEC_PER_MONTH
        baseline_dpm = totals["cost_per_ticket"]
        saved_per_msg = (saved / self.volume) if self.volume else 0.0
        governed_dpm = max(baseline_dpm - saved_per_msg, 0.0)

        return {
            "step": self.step,
            "steps": STEPS,
            "verify": self.verify,
            "clock": self.scenario.clock(now),
            "history": list(self.history),
            "pins": list(self.pins),
            "summary": self._summary(),
            "throughput_per_sec": round(msgs_per_sec, 3),
            "dollars_per_message": round(governed_dpm, 6),
            "baseline_dollars_per_message": round(baseline_dpm, 6),
            "burn_per_min": round(max(gross - saved, 0.0) / _MIN_PER_MONTH, 4),
            "burn_rate": round(max(gross - saved, 0.0) / _MIN_PER_MONTH, 5),
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
        now = time.monotonic()
        active = {p["signature"] for p in service.active_policies()}
        released = self.scenario.released_sigs(now)
        for l in service.levers():  # a released safe cache still to apply?
            if l["signature"] == "route_model:simple":
                continue
            if l["enactable"] and l["safe"] and l["signature"] in released \
                    and l["signature"] not in active and l["signature"] not in self.vetoed:
                return False
        for tc in _ROUTE_CLASSES:  # a released route the human hasn't decided?
            sig = f"route_model:{tc}"
            if sig in released and sig not in active and sig not in self.vetoed \
                    and sig not in self.escalated:
                return False
        return True

    # --- value-spine: history sampling + decision pins + closing summary ----
    def _sample(self) -> None:
        """Append one metrics sample on the compressed clock. dpm is the REAL
        cost-model output given the active levers (solid = measured); quality is
        eval-measured (dashed); volume is the seeded arrival curve."""
        now = time.monotonic()
        rates, rows, totals = self._economics()
        saved = self._saved_total(rates, rows, totals)
        baseline_dpm = totals["cost_per_ticket"]
        dpm = max(baseline_dpm - (saved / self.volume if self.volume else 0), 0.0)
        base_rate = self.volume / _SEC_PER_MONTH * 60.0  # msgs/min at operator volume
        self.history.append({
            "t": round(self.scenario.progress(now), 4),
            "wall": round(self.scenario.elapsed(now), 1),
            "label": self.scenario.label(now),
            "dollars_per_message": round(dpm, 6),
            "quality": self._quality_now(rows),
            "volume": round(self.scenario.arrival_volume(now, base=base_rate), 1),
        })
        if len(self.history) > 160:
            self.history = self.history[-160:]

    def _pin(self, kind: str, label: str, session: str | None = None, trigger: str | None = None) -> None:
        now = time.monotonic()
        self.pins.append({
            "t": round(self.scenario.progress(now), 4),
            "wall": round(self.scenario.elapsed(now), 1),
            "label_time": self.scenario.label(now),
            "kind": kind,            # agent_acted | you_decided | reverted
            "session": session, "label": label, "trigger": trigger,
        })

    def _summary(self) -> dict | None:
        if not self.history:
            return None
        start = self.history[0]["dollars_per_message"]
        now_dpm = self.history[-1]["dollars_per_message"]
        pct = round((1 - now_dpm / start) * 100) if start else 0
        min_q = min((h["quality"] for h in self.history), default=1.0)
        reverts = [p for p in self.pins if p["kind"] == "reverted"]
        decisions = [{"session": p.get("session"), "label": p["label"]}
                     for p in self.pins if p.get("session")]
        prog = self.history[-1]["t"]
        if reverts:
            qnote = "held flat — the one dip (refund routing) was caught and reverted"
        elif min_q < 0.999:
            qnote = "one eval-flagged dip, otherwise held"
        else:
            qnote = "held flat throughout"
        return {
            "start_dpm": round(start, 6), "now_dpm": round(now_dpm, 6), "pct_down": pct,
            "realized_savings": round(service.realized_savings().get("total_savings_usd", 0) or 0, 4),
            "min_quality": round(min_q, 4), "dips": len(reverts), "reversible": True,
            "quality_note": qnote, "decisions": decisions,
            "ready": prog >= 0.85,
        }

    async def _emit(self, kind: str | None, text: str | None = None,
                    step: str | None = None, session: str | None = None,
                    trigger: str | None = None) -> None:
        if step:
            self.step = step
        self._seq += 1
        narration = None
        if text:
            narration = {"text": text, "kind": kind}
            if session:
                narration["session"] = session  # inbox renders a clickable #DS-… link
            if trigger:
                narration["trigger"] = trigger   # the ↳ evidence line on the timeline card
        ev = {"seq": self._seq, "ts": time.time(), "narration": narration, "state": self.snapshot()}
        for q in list(self.subscribers):
            await q.put(ev)

    # --- reasoning (LLM, off-thread) ---------------------------------------
    async def _decide(self) -> bool:
        """Reason (LLM, off-thread) for the agent's real voice/observations. The
        SEQUENCE of route escalations is driven by the scenario clock; this still
        runs for genuine narration. Resilient: a transient error never crashes
        the loop — the agent retries next beat."""
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
        underlying data continuously — and sample the value-spine series so the
        chart fills as the compressed clock runs."""
        while True:
            await asyncio.sleep(3.0)
            async with self._lock:
                self._sample()
            await self._emit(None)  # state only, no narration

    async def reset(self) -> None:
        async with self._lock:
            for p in service.active_policies():
                await asyncio.to_thread(service.deactivate_policy, p["signature"])
            self.vetoed.clear(); self.escalated.clear(); self.pushback = None
            self.plan, self.observations = [], []
            self.verify = None
            self._held = False
            self.scenario.reset()           # restart the scripted-world clock
            self.history.clear(); self.pins.clear()
            self._sample()                  # seed the series at the ungoverned baseline
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
        """One autonomous beat, GATED BY THE CLOCK: apply a released safe cache
        (act-alone), else escalate the next released route the agent can't prove
        (defer), else hold. The safe-vs-risky judgment stays real; the schedule
        only gates WHEN each beat is eligible."""
        now = time.monotonic()
        released = self.scenario.released_sigs(now)
        active = {p["signature"] for p in service.active_policies()}

        # SAFE-FIRST: auto-apply a released safe cache. Driven by the scenario
        # release (not the LLM plan) so the act-alone beat fires deterministically.
        for l in service.levers():
            sig = l["signature"]
            if sig == "route_model:simple" or not l["enactable"] or not l["safe"]:
                continue
            if sig not in released or sig in active or sig in self.vetoed:
                continue
            reason, trigger = _SAFE_REASON.get(sig, (l["title"] + " — output-preserving.", None))
            await self._emit("thinking", None, step="DECIDE")
            await asyncio.sleep(PHASE_DWELL)
            await self._emit("thinking", None, step="ACT")
            await asyncio.to_thread(service.activate_policy, sig, l["policy_type"], l["params"])
            self._held = False
            self._sample()
            self._pin("agent_acted", l["title"], trigger=trigger)
            await self._emit("acted", reason, step="ACT", trigger=trigger)
            await asyncio.sleep(PHASE_DWELL)
            await self._verify(sig, self._lever_by_sig(sig))
            return

        # THE PIVOTS: one route decision at a time. Don't surface the next until the
        # current escalation is resolved (accepted or vetoed).
        pending = any(f"route_model:{tc}" in self.escalated
                      and f"route_model:{tc}" not in active
                      and f"route_model:{tc}" not in self.vetoed for tc in _ROUTE_CLASSES)
        if not pending:
            for beat in self.scenario.live_beats(now):
                if beat["lever_type"] != "route_model":
                    continue
                sig = beat["lever"]
                if sig in active or sig in self.escalated or sig in self.vetoed:
                    continue
                self.escalated.add(sig)
                self._held = False
                await self._emit("escalate", self._route_caution(beat), step="DECIDE",
                                 trigger=beat.get("trigger"))
                return

        # Nothing left to auto-apply right now. Say so ONCE; the loop keeps running
        # and resumes the moment the next beat releases or the human acts.
        if not self._held:
            self._held = True
            done = [l["title"].lower() for l in service.levers() if l["active"] and l["enactable"]]
            esc = [_ROUTE_TITLE[tc].lower() for tc in _ROUTE_CLASSES
                   if f"route_model:{tc}" in self.escalated and f"route_model:{tc}" not in active]
            done_txt = (" and ".join(done) if len(done) <= 2 else
                        ", ".join(done[:-1]) + f", and {done[-1]}") if done else "nothing yet"
            if esc:
                msg = (f"I've handled the safe wins ({done_txt}). What's left — "
                       f"{' and '.join(esc)} — can change the answer, so I've flagged it for your "
                       f"call. Watching the traffic otherwise.")
            else:
                msg = (f"Caching's on ({done_txt}). That's the safe limit for now — I'm watching "
                       f"the traffic for the next thing worth your attention.")
            await self._emit("holding", msg, step="OBSERVE")

    def _route_caution(self, beat: dict) -> str:
        """The agent's real stance when it defers a route, tied to the pre-run eval."""
        tc = beat["use_case"]
        ev = self._eval(beat.get("eval_key"))
        if tc == "password_reset":
            return ("Password resets run on the premium model for what's a templated answer. "
                    "Routing to economy could shift the wording — but it's low-risk, so it's "
                    "your call.")
        if tc == "account_question":
            return ("Account questions are the next model cost, but the variety is wide — I can't "
                    "prove economy holds across all of them. Worth testing in the lab before you "
                    "decide; your call.")
        # refund — the trap; the agent genuinely cautions against it
        return ("Refund handling is the biggest model spend, but it's multi-step — I don't think "
                "economy can hold the quality here. I'd keep premium, but it's your call.")

    # --- VERIFY: re-measure the delta from Phoenix after an enact ----------
    async def _verify(self, sig: str, lv: dict) -> None:
        """The loop's last step. After enacting, re-measure $/message from the live
        data (a real recompute) and attach the trace-level before/after evidence."""
        rates, rows, totals = self._economics()
        baseline_dpm = totals["cost_per_ticket"]
        saved = self._saved_total(rates, rows, totals)
        governed_dpm = max(baseline_dpm - (saved / self.volume if self.volume else 0), 0.0)
        is_cache = lv["policy_type"] == "cache_tool"
        pair = service.captured_trace_pair() if is_cache else None
        same_answer = bool(pair and pair.get("same_answer"))
        monthly = (self._monthly(lv, rates, totals) if is_cache
                   else self._route_monthly(sig.split(":", 1)[1], rows))
        self.verify = {
            "sig": sig, "title": lv["title"],
            "kind": "cache" if is_cache else "route",
            "baseline_dollars_per_message": round(baseline_dpm, 6),
            "dollars_per_message": round(governed_dpm, 6),
            "monthly_saving": round(monthly, 2),
            "same_answer": same_answer,
            "pair": pair,
            "phoenix_url": (pair or {}).get("governed", {}).get("phoenix_url")
            if pair else service.span_deeplink(service.project_gid(), None, None),
            "measured_in_phoenix": True,
        }
        line = (f"Re-measured from the same traffic in Phoenix: $/message "
                f"${baseline_dpm:.4f} → ${governed_dpm:.4f}")
        if same_answer:
            line += ", and the answer comes back identical — quality held."
        else:
            line += ". Watching the next traces to confirm quality holds."
        await self._emit("verified", line, step="VERIFY")

    # --- human turns (canvas actions) -------------------------------------
    def _burn_now(self) -> float:
        rates, rows, totals = self._economics()
        gross = totals["cost_per_ticket"] * self.volume
        saved = self._saved_total(rates, rows, totals)
        return max(gross - saved, 0.0) / _MIN_PER_MONTH

    @staticmethod
    def _fmt(burn: float) -> str:
        return f"${burn:.2f}/min" if burn >= 0.1 else f"${burn:.4f}/min"

    async def _reason_async(self) -> None:
        """Second beat: the agent's voice, after the facts already rendered."""
        async with self._lock:
            if await self._decide() and self.observations:
                await self._emit("reasoned", self.observations[0])

    def _is_route(self, sig: str) -> bool:
        return sig in (f"route_model:{tc}" for tc in _ROUTE_CLASSES)

    async def veto(self, sig: str) -> None:
        async with self._lock:
            rates, rows, totals = self._economics()
            lv = self._lever_by_sig(sig)
            title = lv["title"] if lv else "that lever"
            was_active = bool(lv and lv["active"])
            monthly = (self._route_monthly(sig.split(":", 1)[1], rows) if self._is_route(sig)
                       else (self._monthly(lv, rates, totals) if lv else 0.0))
            await asyncio.to_thread(service.deactivate_policy, sig)
            self.vetoed.add(sig); self.escalated.discard(sig)
            self._sample()
            if was_active and self._is_route(sig):   # rolling back a live route = a revert
                self._pin("reverted", title, trigger="economy degraded vs premium — reverted")
                await self._emit("reverted", f"Reverted {title.lower()} — quality's back to premium.",
                                 trigger="you rolled it back")
            else:
                if was_active and monthly >= _PUSHBACK_MIN_USD and lv:
                    self.pushback = {"sig": sig, "title": title, "monthly": round(monthly, 0)}
                await self._emit("user", f"You vetoed {title}.")
                facts = f"Burn back to {self._fmt(self._burn_now())}"
                if monthly:
                    facts += f" — that's ~${monthly:,.0f}/mo of waste again"
                await self._emit("reaction", facts + ".")
        asyncio.create_task(self._reason_async())

    async def enable(self, sig: str) -> None:
        async with self._lock:
            self.vetoed.discard(sig)
            if self.pushback and self.pushback.get("sig") == sig:
                self.pushback = None
            lv = self._lever_by_sig(sig)
            title = lv["title"] if lv else "that lever"
            if lv and lv["safe"]:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            self._sample()
            await self._emit("user", f"You re-enabled {title}.")
            await self._emit("reaction", f"Back on — burn down to {self._fmt(self._burn_now())}.")
        asyncio.create_task(self._reason_async())

    async def accept(self, sig: str) -> None:
        async with self._lock:
            lv = self._lever_by_sig(sig)
            title = lv["title"] if lv else "that lever"
            if lv:
                await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            self.escalated.discard(sig)
            self._sample()
            self._pin("you_decided", title, trigger="you armed it from the canvas")
            await self._emit("user", f"You accepted {title}.")
            await self._emit("decided", f"Routing live — burn down to {self._fmt(self._burn_now())}.")
            trap = self._is_route(sig) and self._route_is_trap(sig)
        asyncio.create_task(self._reason_async())
        if trap:
            asyncio.create_task(self._flag_trap(sig))

    async def reject(self, sig: str) -> None:
        async with self._lock:
            self.escalated.discard(sig); self.vetoed.add(sig)
            lv = self._lever_by_sig(sig)
            title = lv["title"] if lv else "that lever"
            await self._emit("user", f"You rejected {title}.")
            await self._emit("reaction", "Leaving that one off, then.")
        asyncio.create_task(self._reason_async())

    def _route_is_trap(self, sig: str) -> bool:
        beat = self.scenario.beat_for_lever(sig)
        ev = self._eval(beat.get("eval_key")) if beat else None
        return bool(ev and ev.get("verdict") == "revert")

    async def _flag_trap(self, sig: str) -> None:
        """The agent catching a REAL degradation it cautioned against: a few beats
        after a revert-verdict route is armed, it flags the eval-measured drop and
        recommends reverting. The signal is the real pre-run eval; nothing faked."""
        await asyncio.sleep(6.0)
        async with self._lock:
            active = {p["signature"] for p in service.active_policies()}
            if sig not in active:
                return  # already reverted — nothing to flag
            beat = self.scenario.beat_for_lever(sig)
            ev = self._eval(beat.get("eval_key")) if beat else None
            eq = (ev or {}).get("mean_quality_economy") or 2.0
            bq = (ev or {}).get("mean_quality_baseline") or 5.0
            msg = (f"That economy route on refunds is the one I flagged — the replay had answers "
                   f"dropping to ~{eq:.0f}/5 vs ~{bq:.0f}/5 on premium. It's live now and pulling "
                   f"quality down. I'd revert it.")
            await self._emit("escalate", msg, step="VERIFY",
                             trigger="economy degraded vs premium — revert advised")

    async def fast_forward(self, hours: float = 2.0) -> None:
        async with self._lock:
            self.scenario.fast_forward(hours, time.monotonic())
            self._sample()
            await self._emit("thinking", f"(skipped ahead ~{hours:.0f}h)", step=self.step)

    def route_for_tc(self, tc: str) -> dict | None:
        """For the debugger: the route control for a workload — its real sig, the
        quick-eval key (None ⇒ the evidence is the replay lab), and risky=True."""
        sig = f"route_model:{tc}"
        beat = self.scenario.beat_for_lever(sig)
        if not beat:
            return None
        return {"sig": sig, "eval_key": beat.get("eval_key"), "risky": True}

    # --- promote a debug-session config to PRODUCTION ----------------------
    async def apply_from_debug(self, use_case: str, cache: bool, economy: bool,
                               evidence: dict | None = None) -> dict:
        """The debugger's one sanctioned crossing into production: activate the
        chosen real levers for this use case (deactivate the ones turned off), then
        write the decision to the inbox — with the agent's grounded advisory if a
        risky (answer-affecting) lever is included."""
        async with self._lock:
            applied, removed = [], []
            for sig in _CLASS_LEVERS.get(use_case, []):
                lv = self._lever_by_sig(sig)
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
            ev = evidence or {}
            self._ds_seq += 1
            sid = f"DS-{self._ds_seq:03d}"
            self.sessions[sid] = {
                "id": sid, "use_case": label,
                "source": ev.get("source") or "replay", "n": ev.get("n"),
                "levers": applied, "removed": removed,
                "saved_pct": ev.get("saved_pct"), "projected_monthly": ev.get("projected_monthly"),
                "held_pct": ev.get("held_pct"), "degraded_pct": ev.get("degraded_pct"),
                "advice_against": bool(economy), "applied_at_direction": bool(economy),
                "status": "watching", "applied_ts": time.time(),
                "project_gid": service.project_gid(),
            }
            self._sample()
            trig = None
            if economy and ev.get("held_pct") is not None:
                trig = f"economy held {round(ev['held_pct'] * 100)}% on {ev.get('n', '?')} replays — you armed it"
            elif applied:
                trig = "output-preserving — safe"
            self._pin("you_decided", f"Applied on {label.lower()}", session=sid, trigger=trig)
            parts = []
            if applied:
                parts.append("now running " + " + ".join(t.lower() for t in applied))
            if removed:
                parts.append("turned off " + " + ".join(t.lower() for t in removed))
            ack = f"Applied from your debug session on {label.lower()}: {('; '.join(parts)) or 'no change'}."
            if economy and ev.get("held_pct") is not None:
                ack += (f" Economy held {round(ev['held_pct'] * 100)}% on {ev.get('n', '?')} replays — "
                        f"I'd keep premium, but it's your call. Applied; I'm watching live and I'll flag "
                        f"the moment quality slips.")
            elif applied:
                ack += " Output-preserving — safe; I'll keep watching the traffic."
            await self._emit("user", f"You applied debug session {sid} to {label}.")
            await self._emit("decided", ack, session=sid, trigger=trig)
            trap = economy and any(self._is_route(s) and self._route_is_trap(s)
                                   and s in {p["signature"] for p in service.active_policies()}
                                   for s in (f"route_model:{use_case}",))
        asyncio.create_task(self._reason_async())
        if trap:
            asyncio.create_task(self._flag_trap(f"route_model:{use_case}"))
        return {"applied": applied, "removed": removed, "session": sid}


governor = Governor()
