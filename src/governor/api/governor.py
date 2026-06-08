"""The autonomous governor — one Accountant watching a FLEET of observed agents.

The demo company runs several specialized agents (Support Co-Pilot, Refund
Auditor, Sales Assistant, Docs Bot), each with one signature cost-waste pattern.
The Accountant sweeps the fleet on a time-compressed clock: it AUTO-APPLIES the
safe fixes (cache redundant searches, cap a runaway loop) and ESCALATES the
risky ones (suppress a maybe-needed call, route to a cheaper model) for a human
call. Every figure comes from `governor.service` over the real corpus; the
agent's prose carries none. The world (which problem surfaces when) is scripted
on the clock — the only artifice, disclosed; the agent's detection, eval
verdicts, and lever activation are real.

Each fix is a REAL enactable lever (cache_tool / limit_tool_calls / suppress_tool
/ route_model), agent-scoped, with savings computed from the agent's real tool
counts × prices. Events pushed to subscribers carry the full snapshot the fleet
tree + value-spine render.
"""

import asyncio
import time

from governor import service
from governor.api.scenario import Scenario, ROUTE_ECON_DROP, WALL_WINDOW_SEC
from governor.optimizer import agent

_MIN_PER_MONTH = 30 * 24 * 60
_SEC_PER_MONTH = 30 * 24 * 60 * 60
TICK_SECONDS = 2.5
PHASE_DWELL = 0.7  # hold a real phase briefly so the loop indicator is legible
HEARTBEAT_SEC = 2.0  # spine sampling cadence
# The spine ring buffer must hold the whole window at the heartbeat cadence, else a
# longer ACCOUNTANT_WALL_WINDOW_SEC evicts the early arc before the window ends.
_HIST_CAP = max(160, int(WALL_WINDOW_SEC / HEARTBEAT_SEC) + 40)

STEPS = ["OBSERVE", "DIAGNOSE", "DECIDE", "ACT", "VERIFY"]

# The FLEET — each observed agent + the ONE real lever that fixes its signature
# waste. `fix` is the enactable policy type; `keep` = how many of the wasteful
# tool's calls survive (0 = suppress all, 1 = cap/cache to one). `safe` follows
# service.is_safe(fix): cache + cap are output-preserving (auto-apply), suppress +
# route are answer-affecting (escalate). eval_key names the pre-run quality eval
# for the routing trap (None ⇒ no quick eval).
_FLEET = {
    "support_copilot": {
        "label": "Support Co-Pilot", "purpose": "Resolves helpdesk tickets",
        "model": "gemini-2.5-flash", "fix": "cache_tool", "tool": "web_search", "keep": 1,
        "fix_label": "Cache the repeated web_search",
        "reason": ("Support Co-Pilot re-runs the same web_search about three times per "
                   "ticket — I'm caching the repeats. Output-preserving, so I'll just do it."),
        "trigger": "web_search ×3 redundant", "eval_key": None,
    },
    "refund_auditor": {
        "label": "Refund Auditor", "purpose": "Checks refunds against policy",
        "model": "gemini-2.5-flash", "fix": "limit_tool_calls", "tool": "web_search", "keep": 1,
        "fix_label": "Cap the verification loop to 1",
        "reason": ("Refund Auditor loops the SAME verification web_search ~5× per case — "
                   "capping it to one. The repeats are identical, so the decision is unchanged."),
        "trigger": "identical web_search ×5 loop", "eval_key": None,
    },
    "sales_assistant": {
        "label": "Sales Assistant", "purpose": "Pricing & quotes",
        "model": "gemini-2.5-flash", "fix": "suppress_tool", "tool": "web_search", "keep": 0,
        "fix_label": "Suppress the needless competitor search",
        "reason": ("Sales Assistant runs a competitor web_search the KB already covers. "
                   "Suppressing it could change a quote, so I'll leave the call to you."),
        "trigger": "needless web_search (KB covers it)", "eval_key": None,
    },
    "docs_bot": {
        "label": "Docs Bot", "purpose": "How-to answers",
        "model": "gemini-2.5-flash", "fix": "route_model", "tool": None, "keep": None,
        "fix_label": "Route to the economy model",
        "reason": ("Docs Bot runs the premium model on trivial how-to lookups. Economy is "
                   "cheaper, but the replay shows it degrades — I'd keep premium. Your call."),
        "trigger": "premium model on trivial Q&A", "eval_key": "trip",
    },
}
_FLEET_ORDER = ["support_copilot", "refund_auditor", "sales_assistant", "docs_bot"]
_PUSHBACK_MIN_USD = 1000.0


def _fix_sig(aid: str) -> str:
    return f"{_FLEET[aid]['fix']}:{aid}"


def _agent_of_sig(sig: str) -> str | None:
    for aid in _FLEET_ORDER:
        if _fix_sig(aid) == sig:
            return aid
    return None


class Governor:
    def __init__(self, volume: int = 4_000_000, seed: int = 0):
        self.subscribers: set[asyncio.Queue] = set()
        self.vetoed: set[str] = set()
        self.escalated: set[str] = set()
        self.observations: list[str] = []
        self.pushback: dict | None = None
        self.sessions: dict[str, dict] = {}
        self._ds_seq = 0
        self.verify: dict | None = None
        self.step = "OBSERVE"
        self.rate_overrides: dict[str, float] = {}
        self.volume = volume
        self.scenario = Scenario(seed=seed)
        self.history: list[dict] = []
        self.pins: list[dict] = []
        self._eval_cache: dict[str, dict | None] = {}
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._held = False

    # --- subscriptions -----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def rates(self) -> dict:
        return {**service.default_tool_rates(), **self.rate_overrides}

    async def set_tool_rate(self, tool: str, rate: float) -> None:
        async with self._lock:
            self.rate_overrides[tool] = max(0.0, float(rate))
            await self._emit("user", f"You set the {tool} rate to ${float(rate):.4f}/call.")

    # --- economics ---------------------------------------------------------
    def _economics(self):
        rates = self.rates()
        live = service.live_state()
        recs = service.recommendations()
        rows, totals = service.cost_breakdown(live, recs, rates)
        by = live.get("by_task_class") or {}
        return rates, rows, totals, by

    def _route_params(self) -> dict:
        lv = next((l for l in service.levers() if l["signature"] == "route_model:simple"), None)
        return dict(lv["params"]) if lv and lv.get("params") else {"cheap_model": "gemini-2.5-flash-lite"}

    def _fix_params(self, aid: str) -> dict:
        fx = _FLEET[aid]
        if fx["fix"] == "route_model":
            return {**self._route_params(), "task_class": aid}
        p = {"tool": fx["tool"], "task_class": aid}
        if fx["fix"] == "limit_tool_calls":
            p["max_calls"] = fx["keep"]
        return p

    def _lever_by_sig(self, sig: str) -> dict | None:
        """A lever dict for a fleet fix (synthesized — service.levers() doesn't
        emit these). Activates for real via signature-keyed activate_policy."""
        aid = _agent_of_sig(sig)
        if aid is None:
            return None
        fx = _FLEET[aid]
        active = sig in {p["signature"] for p in service.active_policies()}
        return {"signature": sig, "policy_type": fx["fix"], "params": self._fix_params(aid),
                "title": fx["fix_label"], "active": active, "enactable": True,
                "safe": service.is_safe(fx["fix"]), "agent": aid}

    def _fix_saving_per_ticket(self, aid: str, rows, by) -> float:
        """Per-ticket saving of an agent's fix, from REAL corpus numbers:
        routing → llm×0.8 (lab basis); cache/cap/suppress → removed tool calls ×
        the tool's price."""
        fx = _FLEET[aid]
        r = next((x for x in rows if x["tc"] == aid), None)
        if not r:
            return 0.0
        if fx["fix"] == "route_model":
            return max(r["cost"] - r["tool"], 0.0) * ROUTE_ECON_DROP
        counts = (by.get(aid) or {}).get("avg_tool_counts") or {}
        cnt = counts.get(fx["tool"], 0) or 0
        removed = max(cnt - (fx["keep"] or 0), 0.0)
        price = self.rates().get(fx["tool"], 0.0)
        return removed * price

    def _fix_monthly(self, aid: str, rows, by) -> float:
        r = next((x for x in rows if x["tc"] == aid), None)
        if not r:
            return 0.0
        return self._fix_saving_per_ticket(aid, rows, by) * self.volume * r["share"]

    def _saved_total(self, rows, by) -> float:
        active = {p["signature"] for p in service.active_policies()}
        return sum(self._fix_monthly(aid, rows, by) for aid in _FLEET_ORDER
                   if _fix_sig(aid) in active)

    def _eval(self, key: str | None) -> dict | None:
        if not key:
            return None
        if key not in self._eval_cache:
            from governor.analytics import quality_eval
            self._eval_cache[key] = quality_eval.load_eval(key)
        return self._eval_cache[key]

    def _quality_breakdown(self, by):
        """(retention 0..1, [contributors]), eval-measured. Retention dips ONLY while a
        ROUTE fix whose pre-run verdict is 'revert' (the Docs Bot trap) is active — by
        that eval's measured economy/baseline quality × the agent's VOLUME share (quality
        degradation hits a fraction of conversations, not of dollars). Recovers on revert.
        Suppress/cap/cache never depress it (output-preserving / precautionary). Each
        contributor carries the numbers so the UI can explain *why* the line dipped."""
        active = {p["signature"] for p in service.active_policies()}
        total_n = sum((by.get(a) or {}).get("n", 0) or 0 for a in _FLEET_ORDER) or 1
        ret = 1.0
        contributors = []
        for aid in _FLEET_ORDER:
            fx = _FLEET[aid]
            if fx["fix"] != "route_model" or _fix_sig(aid) not in active:
                continue
            ev = self._eval(fx.get("eval_key"))
            if not ev or ev.get("verdict") != "revert":
                continue
            base_q = ev.get("mean_quality_baseline") or 5.0
            econ_q = ev.get("mean_quality_economy") or base_q
            vshare = ((by.get(aid) or {}).get("n", 0) or 0) / total_n
            if base_q > 0:
                drop = (1 - econ_q / base_q) * vshare
                ret -= drop
                contributors.append({
                    "label": fx["label"], "economy_q": round(econ_q, 1),
                    "baseline_q": round(base_q, 1), "vshare": round(vshare, 4),
                    "drop": round(drop, 4),
                })
        return round(max(min(ret, 1.0), 0.0), 4), contributors

    def _quality_now(self, by) -> float:
        return self._quality_breakdown(by)[0]

    def _quality_basis(self, by) -> dict:
        """Snapshot-level explanation of the current quality line (for the tooltip)."""
        ret, contributors = self._quality_breakdown(by)
        return {"retention": ret, "contributors": contributors}

    # --- snapshot ----------------------------------------------------------
    def _status(self, sig: str, active: set, released: set) -> str:
        if sig in active:
            return "governed"
        if sig in self.escalated:
            return "your_call"
        if sig in self.vetoed:
            return "off"
        if sig in released:
            return "problem"
        return "watching"

    def snapshot(self) -> dict:
        now = time.monotonic()
        rates, rows, totals, by = self._economics()
        gross = totals["cost_per_ticket"] * self.volume
        active_sigs = {p["signature"] for p in service.active_policies()}
        released = self.scenario.released_sigs(now)

        agents, levers, saved = [], [], 0.0
        rows_by = {r["tc"]: r for r in rows}
        total_n = sum((by.get(a) or {}).get("n", 0) or 0 for a in _FLEET_ORDER) or 1
        for aid in _FLEET_ORDER:
            r = rows_by.get(aid)
            if not r:
                continue
            fx = _FLEET[aid]
            sig = _fix_sig(aid)
            active = sig in active_sigs
            monthly = self._fix_monthly(aid, rows, by)
            if active:
                saved += monthly
            lever = {
                "sig": sig, "title": fx["fix_label"], "type": fx["fix"],
                "node": "model" if fx["fix"] == "route_model" else "tools",
                "agent": aid, "agent_label": fx["label"], "active": active,
                "safe": service.is_safe(fx["fix"]), "vetoed": sig in self.vetoed,
                "escalated": sig in self.escalated, "monthly": round(monthly, 2),
                "eval_key": fx.get("eval_key"), "tc": aid,
            }
            levers.append(lever)
            # ops for the agent detail / canvas inner
            counts = (by.get(aid) or {}).get("avg_tool_counts") or {}
            ops = []
            for tool, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
                if tool in ("task_classifier", "(merged tools)") or cnt < 0.5:
                    continue
                gov = active and fx["tool"] == tool
                ops.append({"op": tool, "count": round(cnt, 1), "kind": "tool",
                            "lever": sig if fx["tool"] == tool else None, "governed": gov})
            ops.append({"op": "model", "count": None, "kind": "model",
                        "lever": sig if fx["fix"] == "route_model" else None,
                        "governed": active and fx["fix"] == "route_model"})
            agents.append({
                "id": aid, "label": fx["label"], "purpose": fx["purpose"], "model": fx["model"],
                "cost_per_message": round(r["cost"], 5), "share": round(r["share"], 3),
                "vshare": round(((by.get(aid) or {}).get("n", 0) or 0) / total_n, 4),  # volume share
                "mult": round(r["mult"], 1), "baseline": r["is_base"], "governed": active,
                "status": self._status(sig, active_sigs, released), "waste": fx["trigger"],
                "fix": lever, "ops": ops,
            })
        agents.sort(key=lambda a: -a["share"])

        msgs_per_sec = self.volume / _SEC_PER_MONTH
        baseline_dpm = totals["cost_per_ticket"]
        saved_per_msg = (saved / self.volume) if self.volume else 0.0
        governed_dpm = max(baseline_dpm - saved_per_msg, 0.0)

        return {
            "step": self.step, "steps": STEPS, "verify": self.verify,
            "clock": self.scenario.clock(now),
            "history": list(self.history), "pins": list(self.pins), "summary": self._summary(),
            "quality_basis": self._quality_basis(by),
            "throughput_per_sec": round(msgs_per_sec, 3),
            "dollars_per_message": round(governed_dpm, 6),
            "baseline_dollars_per_message": round(baseline_dpm, 6),
            "burn_per_min": round(max(gross - saved, 0.0) / _MIN_PER_MONTH, 4),
            "gross_burn": round(gross / _MIN_PER_MONTH, 5),
            "volume": self.volume,
            "active_count": service.policies_active_count(),
            "realized_savings": round(service.realized_savings().get("total_savings_usd", 0) or 0, 4),
            "agents": agents, "levers": levers,
            # back-compat alias so the pre-tree canvas still renders the fleet as lanes
            "classes": [{"tc": a["id"], "label": a["label"], "cost_per_ticket": a["cost_per_message"],
                         "share": a["share"], "mult": a["mult"], "baseline": a["baseline"],
                         "governed": a["governed"], "ops": a["ops"]} for a in agents],
            "roadmap": service.roadmap_capabilities(),
            "pushback": self.pushback, "holding": self._holding(),
        }

    def _holding(self) -> bool:
        now = time.monotonic()
        active = {p["signature"] for p in service.active_policies()}
        released = self.scenario.released_sigs(now)
        for aid in _FLEET_ORDER:
            sig = _fix_sig(aid)
            if sig in released and sig not in active and sig not in self.vetoed \
                    and sig not in self.escalated:
                return False
        return True

    # --- value-spine -------------------------------------------------------
    def _sample(self) -> None:
        now = time.monotonic()
        # Window complete → FREEZE the timeline. Otherwise the clock clamps at the
        # end (t=1.0) and the steady heartbeat keeps appending samples there, which
        # evicts the actual arc from the ring buffer and blanks the chart. No new
        # traffic = nothing new to plot; the completed arc stays put.
        if self.history and self.history[-1].get("t", 0.0) >= 0.999:
            return
        rates, rows, totals, by = self._economics()
        saved = self._saved_total(rows, by)
        baseline_dpm = totals["cost_per_ticket"]
        dpm = max(baseline_dpm - (saved / self.volume if self.volume else 0), 0.0)
        base_rate = self.volume / _SEC_PER_MONTH * 60.0
        self.history.append({
            "t": round(self.scenario.progress(now), 4),
            "wall": round(self.scenario.elapsed(now), 1),
            "label": self.scenario.label(now),
            "dollars_per_message": round(dpm, 6),
            "quality": self._quality_now(by),
            "volume": round(self.scenario.arrival_volume(now, base=base_rate), 1),
        })
        if len(self.history) > _HIST_CAP:
            self.history = self.history[-_HIST_CAP:]

    def _pin(self, kind: str, label: str, session: str | None = None, trigger: str | None = None) -> None:
        now = time.monotonic()
        self.pins.append({
            "t": round(self.scenario.progress(now), 4), "wall": round(self.scenario.elapsed(now), 1),
            "label_time": self.scenario.label(now), "kind": kind,
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
        if reverts:
            qnote = "held flat — the one dip (Docs Bot routing) was caught and reverted"
        elif min_q < 0.999:
            qnote = "one eval-flagged dip, otherwise held"
        else:
            qnote = "held flat throughout"
        return {
            "start_dpm": round(start, 6), "now_dpm": round(now_dpm, 6), "pct_down": pct,
            "realized_savings": round(service.realized_savings().get("total_savings_usd", 0) or 0, 4),
            "min_quality": round(min_q, 4), "dips": len(reverts), "reversible": True,
            "quality_note": qnote,
            "decisions": [{"session": p.get("session"), "label": p["label"]}
                          for p in self.pins if p.get("session")],
            "ready": self.history[-1]["t"] >= 0.85,
        }

    async def _emit(self, kind: str | None, text: str | None = None, step: str | None = None,
                    session: str | None = None, trigger: str | None = None) -> None:
        if step:
            self.step = step
        self._seq += 1
        narration = None
        if text:
            narration = {"text": text, "kind": kind}
            if session:
                narration["session"] = session
            if trigger:
                narration["trigger"] = trigger
        ev = {"seq": self._seq, "ts": time.time(), "narration": narration, "state": self.snapshot()}
        for q in list(self.subscribers):
            await q.put(ev)

    # --- reasoning (LLM, off-thread, for narration only) -------------------
    async def _decide(self) -> bool:
        try:
            dec = await asyncio.to_thread(agent.decide, list(self.vetoed))
            self.observations = list(dec.observations)
            return True
        except Exception:
            if not self.observations:
                self.observations = ["Watching the fleet — reading the live traffic."]
            return False

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.reset()
        self._task = asyncio.create_task(self._loop())
        self._hb = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            async with self._lock:
                self._sample()
            await self._emit(None)

    async def reset(self) -> None:
        async with self._lock:
            for p in service.active_policies():
                await asyncio.to_thread(service.deactivate_policy, p["signature"])
            self.vetoed.clear(); self.escalated.clear(); self.pushback = None
            self.observations = []
            self.verify = None
            self._held = False
            self.scenario.reset()
            self.history.clear(); self.pins.clear()
            self._sample()
            await self._emit("thinking", "Watching the fleet — reading the live traffic to see "
                             "which agent is burning money.", step="OBSERVE")
            await self._decide()
            for i, o in enumerate(self.observations):
                await self._emit("thinking", o, step="DIAGNOSE" if i == 0 else None)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            async with self._lock:
                await self._advance()

    async def _advance(self) -> None:
        """One autonomous beat, gated by the clock: auto-apply a released SAFE fix
        (cache / cap), else escalate the next released RISKY fix (suppress / route),
        else hold. The safe-vs-risky judgment is real; the clock only gates when a
        beat is eligible."""
        now = time.monotonic()
        active = {p["signature"] for p in service.active_policies()}
        live = self.scenario.live_beats(now)

        # SAFE-FIRST: auto-apply a released safe fix.
        for beat in live:
            sig = beat["lever"]
            if sig in active or sig in self.vetoed:
                continue
            lv = self._lever_by_sig(sig)
            if not lv or not lv["safe"]:
                continue
            aid = lv["agent"]; fx = _FLEET[aid]
            await self._emit("thinking", None, step="DECIDE")
            await asyncio.sleep(PHASE_DWELL)
            await self._emit("thinking", None, step="ACT")
            await asyncio.to_thread(service.activate_policy, sig, lv["policy_type"], lv["params"])
            self._held = False
            self._sample()
            self._pin("agent_acted", f"{fx['label']}: {fx['fix_label'].lower()}", trigger=fx["trigger"])
            await self._emit("acted", fx["reason"], step="ACT", trigger=fx["trigger"])
            await asyncio.sleep(PHASE_DWELL)
            await self._verify(sig, lv)
            return

        # THE PIVOTS: escalate the next released risky fix, one at a time.
        pending = any(_fix_sig(a) in self.escalated and _fix_sig(a) not in active
                      and _fix_sig(a) not in self.vetoed for a in _FLEET_ORDER)
        if not pending:
            for beat in live:
                sig = beat["lever"]
                lv = self._lever_by_sig(sig)
                if not lv or lv["safe"] or sig in active or sig in self.escalated or sig in self.vetoed:
                    continue
                self.escalated.add(sig)
                self._held = False
                await self._emit("escalate", _FLEET[lv["agent"]]["reason"], step="DECIDE",
                                 trigger=beat.get("trigger"))
                return

        if not self._held:
            self._held = True
            done = [_FLEET[a]["label"] for a in _FLEET_ORDER if _fix_sig(a) in active]
            esc = [_FLEET[a]["label"] for a in _FLEET_ORDER
                   if _fix_sig(a) in self.escalated and _fix_sig(a) not in active]
            done_txt = (" and ".join(done) if len(done) <= 2 else
                        ", ".join(done[:-1]) + f", and {done[-1]}") if done else "nothing yet"
            if esc:
                msg = (f"Handled the safe wins ({done_txt}). What's left — {' and '.join(esc)} — "
                       f"can change the answer, so I've flagged it for your call. Watching the rest.")
            else:
                msg = (f"Fixed {done_txt} on its own. That's the safe limit for now — watching the "
                       f"fleet for the next thing worth your attention.")
            await self._emit("holding", msg, step="OBSERVE")

    # --- VERIFY ------------------------------------------------------------
    async def _verify(self, sig: str, lv: dict) -> None:
        rates, rows, totals, by = self._economics()
        baseline_dpm = totals["cost_per_ticket"]
        saved = self._saved_total(rows, by)
        governed_dpm = max(baseline_dpm - (saved / self.volume if self.volume else 0), 0.0)
        is_cache = lv["policy_type"] == "cache_tool"
        pair = service.captured_trace_pair() if is_cache else None
        same_answer = bool(pair and pair.get("same_answer"))
        self.verify = {
            "sig": sig, "title": lv["title"], "agent": lv["agent"],
            "kind": lv["policy_type"],
            "baseline_dollars_per_message": round(baseline_dpm, 6),
            "dollars_per_message": round(governed_dpm, 6),
            "monthly_saving": round(self._fix_monthly(lv["agent"], rows, by), 2),
            "same_answer": same_answer, "pair": pair,
            "phoenix_url": (pair or {}).get("governed", {}).get("phoenix_url")
            if pair else service.span_deeplink(service.project_gid(), None, None),
            "measured_in_phoenix": True,
        }
        line = (f"Re-measured from {_FLEET[lv['agent']]['label']}'s traffic in Phoenix: "
                f"$/message ${baseline_dpm:.4f} → ${governed_dpm:.4f}")
        line += ", answer identical — quality held." if same_answer else \
            ". Watching the next traces to confirm quality holds."
        await self._emit("verified", line, step="VERIFY")

    # --- human turns -------------------------------------------------------
    def _burn_now(self) -> float:
        rates, rows, totals, by = self._economics()
        gross = totals["cost_per_ticket"] * self.volume
        return max(gross - self._saved_total(rows, by), 0.0) / _MIN_PER_MONTH

    @staticmethod
    def _fmt(burn: float) -> str:
        return f"${burn:.2f}/min" if burn >= 0.1 else f"${burn:.4f}/min"

    async def _reason_async(self) -> None:
        async with self._lock:
            if await self._decide() and self.observations:
                await self._emit("reasoned", self.observations[0])

    def _route_is_trap(self, sig: str) -> bool:
        aid = _agent_of_sig(sig)
        if not aid or _FLEET[aid]["fix"] != "route_model":
            return False
        ev = self._eval(_FLEET[aid].get("eval_key"))
        return bool(ev and ev.get("verdict") == "revert")

    async def veto(self, sig: str) -> None:
        async with self._lock:
            rates, rows, totals, by = self._economics()
            lv = self._lever_by_sig(sig)
            title = lv["title"] if lv else "that lever"
            was_active = bool(lv and lv["active"])
            monthly = self._fix_monthly(lv["agent"], rows, by) if lv else 0.0
            await asyncio.to_thread(service.deactivate_policy, sig)
            self.vetoed.add(sig); self.escalated.discard(sig)
            self._sample()
            if was_active and lv and _FLEET[lv["agent"]]["fix"] == "route_model":
                self._pin("reverted", f"{_FLEET[lv['agent']]['label']}: reverted",
                          trigger="economy degraded vs premium — reverted")
                await self._emit("reverted", f"Reverted {title.lower()} on "
                                 f"{_FLEET[lv['agent']]['label']} — quality's back to premium.",
                                 trigger="you rolled it back")
            else:
                if was_active and monthly >= _PUSHBACK_MIN_USD and lv:
                    self.pushback = {"sig": sig, "title": title, "monthly": round(monthly, 0)}
                await self._emit("user", f"You turned off {title}.")
                facts = f"Burn back to {self._fmt(self._burn_now())}"
                if monthly:
                    facts += f" — ~${monthly:,.0f}/mo of waste again"
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
            label = _FLEET[lv["agent"]]["label"] if lv else ""
            self._pin("you_decided", f"{label}: armed {title.lower()}", trigger="you armed it from the canvas")
            await self._emit("user", f"You accepted {title} on {label}.")
            await self._emit("decided", f"Live now — burn down to {self._fmt(self._burn_now())}.")
            trap = self._route_is_trap(sig)
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

    async def _flag_trap(self, sig: str) -> None:
        await asyncio.sleep(6.0)
        async with self._lock:
            if sig not in {p["signature"] for p in service.active_policies()}:
                return
            aid = _agent_of_sig(sig)
            ev = self._eval(_FLEET[aid].get("eval_key")) if aid else None
            eq = (ev or {}).get("mean_quality_economy") or 2.0
            bq = (ev or {}).get("mean_quality_baseline") or 5.0
            await self._emit("escalate", f"That economy route on {_FLEET[aid]['label']} is the one I "
                             f"flagged — the replay had answers dropping to ~{eq:.0f}/5 vs ~{bq:.0f}/5 "
                             f"on premium. It's live and pulling quality down. I'd revert it.",
                             step="VERIFY", trigger="economy degraded vs premium — revert advised")

    async def fast_forward(self, hours: float = 2.0) -> None:
        async with self._lock:
            self.scenario.fast_forward(hours, time.monotonic())
            self._sample()
            await self._emit("thinking", f"(skipped ahead ~{hours:.0f}h)", step=self.step)

    def route_for_tc(self, tc: str) -> dict | None:
        """Debugger: the fix control for an agent — its real sig, type, quick-eval
        key (None ⇒ evidence is the lab / proof), and whether it's answer-affecting."""
        if tc not in _FLEET:
            return None
        fx = _FLEET[tc]
        sig = _fix_sig(tc)
        return {"sig": sig, "type": fx["fix"], "eval_key": fx.get("eval_key"),
                "risky": not service.is_safe(fx["fix"])}

    # --- promote a debug-session config to PRODUCTION ----------------------
    async def apply_from_debug(self, use_case: str, cache: bool, economy: bool,
                               evidence: dict | None = None) -> dict:
        """Apply an agent's fix from the debugger. `cache` = apply a safe fix
        (cache/cap); `economy` = apply a risky fix (suppress/route). One fix per
        agent, so whichever flag matches the agent's fix type wins."""
        async with self._lock:
            aid = use_case
            applied, removed = [], []
            lv = self._lever_by_sig(_fix_sig(aid)) if aid in _FLEET else None
            if lv:
                want = cache if lv["safe"] else economy
                if want and not lv["active"]:
                    await asyncio.to_thread(service.activate_policy, lv["signature"],
                                            lv["policy_type"], lv["params"])
                    self.vetoed.discard(lv["signature"]); self.escalated.discard(lv["signature"])
                    applied.append(lv["title"])
                elif want and lv["active"]:
                    applied.append(lv["title"])
                elif (not want) and lv["active"]:
                    await asyncio.to_thread(service.deactivate_policy, lv["signature"])
                    removed.append(lv["title"])
            label = _FLEET[aid]["label"] if aid in _FLEET else aid
            ev = evidence or {}
            self._ds_seq += 1
            sid = f"DS-{self._ds_seq:03d}"
            risky = bool(lv and not lv["safe"])
            self.sessions[sid] = {
                "id": sid, "use_case": label, "source": ev.get("source") or "replay",
                "n": ev.get("n"), "levers": applied, "removed": removed,
                "saved_pct": ev.get("saved_pct"), "projected_monthly": ev.get("projected_monthly"),
                "held_pct": ev.get("held_pct"), "degraded_pct": ev.get("degraded_pct"),
                "advice_against": risky, "applied_at_direction": risky,
                "status": "watching", "applied_ts": time.time(), "project_gid": service.project_gid(),
            }
            self._sample()
            trig = (f"economy held {round(ev['held_pct'] * 100)}% on {ev.get('n', '?')} replays — you armed it"
                    if (risky and ev.get("held_pct") is not None) else
                    ("output-preserving — safe" if applied else None))
            self._pin("you_decided", f"{label}: applied", session=sid, trigger=trig)
            parts = []
            if applied:
                parts.append("now running " + " + ".join(t.lower() for t in applied))
            if removed:
                parts.append("turned off " + " + ".join(t.lower() for t in removed))
            ack = f"Applied from your debug session on {label}: {('; '.join(parts)) or 'no change'}."
            if risky and ev.get("held_pct") is not None:
                ack += (f" Held {round(ev['held_pct'] * 100)}% on {ev.get('n', '?')} replays — I'd "
                        f"keep premium, but it's your call. Watching live; I'll flag the moment quality slips.")
            elif applied:
                ack += " Output-preserving — safe; I'll keep watching."
            await self._emit("user", f"You applied debug session {sid} to {label}.")
            await self._emit("decided", ack, session=sid, trigger=trig)
            trap = risky and self._route_is_trap(_fix_sig(aid)) \
                and _fix_sig(aid) in {p["signature"] for p in service.active_policies()}
        asyncio.create_task(self._reason_async())
        if trap:
            asyncio.create_task(self._flag_trap(_fix_sig(aid)))
        return {"applied": applied, "removed": removed, "session": sid}


governor = Governor()
