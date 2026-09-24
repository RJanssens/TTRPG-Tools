"""Cross-module composite plumbing plus the generic ``batch`` tool.

Every mandated step in CLAUDE.md used to cost one model round trip: measured at
~2.6 s of prefill plus a few hundred thinking tokens each, so a turn that owed
the rules eight sequential mutations spent most of a minute on protocol rather
than on play. Composite tools run those sequences server-side, in order, and
hand back every result at once.

Ordering is preserved, so nothing here weakens the "Sequential only — never
parallelize" rule in CLAUDE.md: the calls still happen one after another, and
the dice still land before the DM writes a word (Hard Constraint #1). What
disappears is the round trip between them.
"""
from typing import Any

# A generous ceiling: long enough for a full combat round's worth of
# bookkeeping, short enough that a runaway list can't stall a turn.
_MAX_STEPS = 25

# Tools that must never run inside a batch: nesting (batch-in-batch) and the
# turn-scoped checklist, which is meant to be read before the turn's mechanics
# are chosen, not folded in among them.
_BATCH_DENY = {"batch", "pre_turn_check"}


async def call_tool(mcp, tool_name: str, /, **kwargs) -> Any:
    """Invoke another registered MCP tool by name.

    Composite tools routinely span modules — setup_combat lives in tools.combat
    but surprise_check lives in tools.dice — so they go through the tool
    manager rather than duplicating logic. The single implementation, event
    logging included, stays the only one.
    """
    return await mcp._tool_manager.call_tool(tool_name, kwargs)


def register(mcp):

    @mcp.tool()
    async def batch(steps: list[dict], stop_on_error: bool = True) -> dict:
        """Run several tool calls in one round trip, strictly in order.

        For the mandated sequences that CLAUDE.md forbids parallelising because
        they mutate state or log events — advance_time → check_encounter →
        consume, or add_inventory across a party, or three skill_checks in a
        row. The server executes them sequentially, so order and event
        ordering are exactly as if you had made the calls yourself.

        steps: [{"tool": "advance_time", "args": {"minutes": 10}},
                {"tool": "check_encounter", "args": {"terrain": "dungeon"}}]
        stop_on_error: True (default) halts at the first failing step and
                reports what ran; False runs the rest regardless.

        Use it only when every step's arguments are known up front. When a
        later call depends on an earlier result — apply_combat_damage needs the
        damage attack() rolled — use the purpose-built composite instead
        (resolve_attack, combat_round, setup_combat, scout, council), which
        chains the data internally.

        Not for batch itself or pre_turn_check. Max 25 steps."""
        if not isinstance(steps, list) or not steps:
            return {"error": "steps must be a non-empty list of {tool, args} dicts."}
        if len(steps) > _MAX_STEPS:
            return {"error": f"Too many steps ({len(steps)}); the cap is {_MAX_STEPS}."}

        results: list[dict] = []
        stopped_early = False

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                results.append({"step": i, "error": f"not a dict: {step!r}"})
                if stop_on_error:
                    stopped_early = True
                    break
                continue

            name = str(step.get("tool") or "").strip()
            args = step.get("args") or {}
            if not isinstance(args, dict):
                results.append({"step": i, "tool": name, "error": "args must be an object"})
                if stop_on_error:
                    stopped_early = True
                    break
                continue

            if name in _BATCH_DENY:
                results.append({"step": i, "tool": name,
                                "error": f"'{name}' cannot run inside batch — call it on its own."})
                if stop_on_error:
                    stopped_early = True
                    break
                continue

            if mcp._tool_manager.get_tool(name) is None:
                results.append({"step": i, "tool": name, "error": "unknown tool"})
                if stop_on_error:
                    stopped_early = True
                    break
                continue

            try:
                out = await call_tool(mcp, name, **args)
                entry = {"step": i, "tool": name, "result": out}
            except Exception as exc:
                entry = {"step": i, "tool": name, "error": str(exc)}

            results.append(entry)
            if stop_on_error and ("error" in entry
                                  or (isinstance(entry.get("result"), dict)
                                      and "error" in entry["result"])):
                stopped_early = True
                break

        return {
            "ran":           len(results),
            "requested":     len(steps),
            "stopped_early": stopped_early,
            "results":       results,
        }
