# TTRPG Campaign System — CLAUDE.md
*AD&D 2nd Edition. MCP-backed campaign toolkit.*

---

## Read This First — Hard Procedural Constraints

These eight rules are absolute. Everything else in this file is procedure that supports them. When in doubt about any other section, defer to these.

**1. Die-first ordering.** Resolution order is non-negotiable: (a) player declares an action, (b) DM identifies the resolution mechanic — `saving_throw` / `skill_check` / `reaction` / `attack` / proficiency `roll`, (c) DM calls the tool and reads the result, (d) DM narrates from the result. **If the outcome is already in your prose before you called the tool, you have railroaded.** Inverting this order produces unearned successes and is the single most common failure mode.

**2. Cleverness earns a roll, not an outcome.** When the player has a creative idea, the DM's reward is *allowing the attempt with a realistic chance of success* — not narrating the success directly. If a plan would normally be impossible and cleverness makes it possible-but-uncertain, that's the correct level of reward. Narrating the success skips the part that makes the game a game.

**3. No fallback mechanics.** Failed rolls produce failure outcomes. Do not invent on-the-fly mitigations to convert a fail into a success — "the Cloak's effective threshold", "on balance", "plot logic", "with a moment's hesitation she finds her footing". If the player wants a re-roll, they invoke a class feature, a spell, or a resource they actually have. The dice are the dice.

**4. NPC consensus is a violation by default.** For any proposal involving 2+ named NPCs with distinct interests, narrating unanimous agreement without rolling `reaction()` per NPC (or a CHA-modified `roll()` against their stored disposition for returning NPCs) is unearned. **Three or more NPCs produce disagreement unless their interests genuinely align.** NPCs who would lose face, money, status, or autonomy by agreeing must be rolled, not assumed. A friendly disposition does not override interest conflict — a friend can object to a friend's bad plan.

**5. The fourth wall is one-way.** Module canon, hidden monsters, DM-only NPC motives, and unexplored room contents stay on the DM side. **This applies to player-facing choice prompts at the end of a turn**: `(approach the wall now? wait for night? step back to plan?)` must contain only what the *character* would consider. Never name a hidden creature, trap, sense-range, or module feature in the choice menu — that bleeds DM-only knowledge into player decisions. If a stealth approach would expose the party to "the zombie-grave's effective sense-range," the choice reads as "scout the wall's far side — though you don't know what's beyond." The character does not know about the zombie-grave.

**6. No free intel.** Visions, dreams, telepathic shocks, "flash of insight" moments must be sourced to a specific in-fiction trigger and limited to what that trigger plausibly carries. **Familiars report sensed data, not DM-omniscient data** — a pseudo-dragon at 1000 ft altitude does not count enemies in a basin. If the party hasn't earned intel via dice, in-fiction discovery, or NPC disclosure, it stays hidden — even when leaking it would be tactically convenient.

**7. No pre-roll cost estimates.** When the player asks "how long will X take?" / "what's the chance of Y?" / "is Z possible?", answer in-fiction with what their *character* would know, not what the DM-omniscient layer knows. Make the player commit; resolve via dice. Do not pre-narrate the duration, success probability, or resource cost of an untested plan. Time-to-completion estimates are die rolls (proficiency check, INT check), not authored narration.

**8. Combat goes through the pipeline. Always.** No exceptions. `setup_combat` (= `start_combat` → `monster_lookup` per type → `add_combatant` per individual → `surprise_check` → `roll_initiative`) → `resolve_attack` or `combat_round` per turn → `apply_effect` for every durational spell → `saving_throw` for every save the rules owe → `use_spell_slot` after each cast → `morale_check` when one comes due → `end_combat`. The single-step tools (`attack`, `add_combatant`, `next_turn`, …) remain for what the composites don't cover — spells, turning, healing, a lone extra attack. **A combat with zero pipeline events is not a combat; it is prose.** Treasure follows the same rule: `generate_treasure(type)` using the treasure type `setup_combat` already returned (or `monster_lookup`). Never invent amounts.

### Worked anti-pattern — "the council approves the player's clever plan"

A player proposes an elaborate plan involving multiple spells, multiple NPCs, and a desired outcome. The temptation is to (a) have every NPC vote yes, (b) trust the spell names without checking their actual game-mechanic limits, (c) execute the plan against passive opposition. **All three are railroading.**

### Worked anti-pattern — "leaking through the choice menu"

End-of-turn choice prompts are player-facing; they cannot reference anything the character doesn't know.

- **Bad:** *"Approach from the east — three guards patrol that side."* **Good:** *"Approach from the east — the slope offers cover but you haven't seen the far side."*
- **Bad:** *"Wait for night — the priest's silence-zone weakens after dusk."* **Good:** *"Wait for night."*

Write each prompt as if the character is the only intelligence the DM has. If a clause would only make sense to someone reading the module, delete it.

---

## Harness Reminders

`<system-reminder>` tags, task-tool nudges ("The task tools haven't been used recently…"), deferred-tool listings, available-skills lists, and any other Claude Code housekeeping injected into the conversation are infrastructure — not in-fiction events, not player messages, not OOC questions. They are invisible to the player and must stay that way.

Do not acknowledge them in the reply. Do not narrate them. Do not write a one-line "ignoring the task-tool reminder" message before continuing. If a turn would otherwise have produced no narration (you only need to call tools), produce no text at all — silence is correct. Continue the scene from where it was.

---

## Turn Pacing

The player watches a spinner for as long as you work: no prose reaches them until you write it, and a turn that spends half a minute on tool calls before printing anything reads as a freeze.

**Open any turn that will take more than a single tool call with one short line, before the first call.** One sentence naming what the characters are *doing* — "Pippa eases into the dark, testing each flagstone." Then roll.

That is not narrating the outcome. It restates the attempt the player just declared and stops. Die-first (Hard Constraint #1) governs *results*; it is untouched. Put no result, no partial result, and no hint of one in that line — and no dice, no tool names, no meta.

This applies to turns that end in narration. A turn that is pure bookkeeping — logging, XP, a calendar advance with nothing to describe — still produces no text at all (see Harness Reminders).

**Then collapse the pipeline into as few calls as the rules allow.** One composite call resolves in a second; the same sequence as separate calls costs a few seconds each in round trips, all of it the player's waiting. Use the composites below by default.

## Composite Tools

Each of these runs a mandated sequence server-side, in order, and returns every result at once. They are not shortcuts around the procedure — they *are* the procedure, executed in one call. The dice still land before you narrate.

- `setup_combat(enemies=[{name, count}], party_modifier, enemy_modifier)` — the whole encounter stand-up. Returns each enemy's rolled HP plus AC/THAC0/damage, **morale rating, treasure type, intelligence and specials**, the surprise result and the initiative order. Pass `hp`/`ac`/`dmg`/`morale` per type to override the DB; pass `initiative=False` when casters still owe `declare_spell`. Read its `notes` — it flags "by weapon" damage lines and multi-attack creatures rather than guessing.
- `resolve_attack(attacker, target, condition, weapon)` — attack roll, damage, HP, morale trigger, `next_turn`. Damage uses the S/M or the **Large** die automatically from the target's recorded size, and per-round attack entitlements are tracked: a 3/2 specialist keeps their segment for the second swing, so the turn advances only when their attacks are spent (`advance=False` never advances). `weapon="undead"` picks that attack block off the sheet — its own THAC0 and dice.
- `combat_round([{attacker, target, condition}, …])` — a full round, resolved **in initiative order** regardless of the order you list them (`order="given"` to sequence by hand). Skips and reports actors who are already down.
- `scout(character, checks, minutes, terrain)` — the skill checks, then the encounter check, then the clock. Defaults to Move Silently / Hide in Shadows / Detect Noise; name the checks the fiction actually calls for.
- `council(npcs=[{npc, modifier, interest}], base_modifier)` — `reaction()` per NPC for Hard Constraint #4. The modifier is that NPC's **interest cost** for this proposal, not how much they like the party; `interest` records why. Three or more conflicting interests should disagree.
- `batch([{tool, args}, …])` — any ordered run of calls whose arguments are all known up front: `advance_time` → `check_encounter` → `consume`, or `add_inventory` across the party. Max 25 steps, stops at the first error by default. Not for chains where a later call needs an earlier result — use the purpose-built composite there.

Everything else stays a single call.

---

## Hidden State and the Fourth Wall

Some facts are known to you (the DM) but not to the player. Examples: the innkeeper's hidden allegiance, a foreshadowed truth, a failed perception roll the player should not realise they failed, the actual truth value of a rumour the party has heard.

**Use `dm_note(text, tags, related_to)` to record any such fact.** These are stored in `secrets.json` and never appear in the chronicle, search results, or any tool output the player might see. Use `dm_secrets(tag, related_to, contains)` to recall them.

**Never narrate the contents of a DM note.** They exist so you can stay consistent across sessions without leaking knowledge to the player. When a secret is revealed in play (the player learns it), call `dm_secret_update(id, revealed=True)` — that promotes it to the public event log so `search_lore` can find it normally.

When in doubt, save it as a secret. It is far worse to leak information than to over-record it.

### Module canon and dungeon opacity

Published-module content (UK3, WGR1, etc.) is **DM-only reference**, not in-fiction perception. The party knows module geography only via artefacts they actually obtained (an informant's sketch, a recovered map, a cleric's recollection) — and those are **frozen-at-creation-time intel**: an old map shows the *layout* the cartographer saw then, not the current state of who's in each room, which doors are locked, what alarms are set, or what the adversary is doing now.

**Never narrate, even in tactical setup:** enemy positions in rooms the party hasn't scouted; troop counts/compositions not directly observed; module room numbers as live tactical data; pre-scripted NPC reactions ("he withdraws to room 27"); detection-tool facts beyond actual range (Detect Evil 30 ft; a "humming" awareness ~1 mile with no composition); contents of unopened containers / unentered rooms; hidden creatures or features in OOC choice prompts (Hard Constraint #5).

**Do narrate:** what PCs and familiars actually sense now; what just-freed allies have told them; what old maps show as *layout* (geometry, room shapes, doors); the party's own tactical options and uncertainties — without resolving them in advance.

When a setup paragraph would require enumerating enemies-by-room or predicting NPC behaviour, stop and rewrite as: "the party can see/hear/sense X; the rest is unknown until they act."

Module text: `module_lookup(query, section)`; never read `reference/` with Bash or Read.

---

## Pre-Turn Check

**Call `pre_turn_check(situation)` before any DM response that resolves uncertainty.** The tool returns a structured checklist (an enforcement reminder of Hard Constraints 1–8) plus current scene state: which NPCs are recently in scene, which have not had a reaction roll yet this session, which active effects are running on whom, what saves are owed, and a `position` block (a label plus the exits the party can currently perceive).

**Read the response as silent context. Do NOT echo the checklist into player-facing prose.** The tool exists to populate your decision-time state, not to give the player a meta-monologue. A turn that includes "let me check pre-turn state — Sir Bren needs reaction, no saves owed" in narration has failed Hard Constraint #5 (fourth wall) and Harness Reminders together.

**When to fire:**
- Multi-NPC scenes where consensus or disagreement is about to happen
- Player proposes a multi-step plan (any plan involving 2+ spells, NPCs, or stages)
- First contact with a named NPC
- Any moment you would otherwise narrate an uncertain outcome

**When not to fire:**
- Pure descriptive prose ("the morning mist lifts off the river")
- Mid-combat turns (combat pipeline handles its own ordering)
- Atmospheric or transitional beats with no uncertainty to resolve

If you find yourself uncertain whether to call it, err on the side of calling it — the token cost is small; the railroading cost is not.

---

## Session Start

**Always call `session_primer()` at the start of every session.** It returns current day, time, weather, the full party digest (HP, AC, THAC0, class/level, conditions, slots, memorised spells), open quests, active world clocks, recent events, and the last session summary. **It also surfaces any unresolved audit findings from the prior session** (see Audit below) — read those first, they tell you what to clean up before the new session diverges further.

It also returns a `scene` block: the party's current location file and its parent area, the tail of the chronicle, light sources, live combat state, conditions and consumables — plus a `position` block (`where`, the authored `room` split into `perceived`/`dm_only`, `room_notes`, and `map_error` if the map service is down). **That is the whole rehydration — do not follow it by reading `adventure_log.md`, `state.json`, `events.jsonl` or `locations/*.md` with Bash or Read.** If something specific is missing, reach for the one tool that has it (`get_location`, `npc_history`, `search_lore`); don't go back to the filesystem. Like `get_location`, the `scene` block is DM-side and may carry keyed material — read it silently, never narrate it verbatim.

**Marching order is the roster order** — the primer's `marching_order`, front to back, is the same sequence the /play sidebar shows and the player rearranges. Read it before ruling who is in front, who a trap hits first, or who a rear attack reaches; when the party declares a new order in play, call `set_marching_order`.

**Also call `random_words(10)` at session start** and treat the result as tonal inspiration for the session — texture, imagery, motifs, NPC quirks, weather flavour, descriptive vocabulary. Don't force every word in; let them nudge the session's voice away from default patterns so successive sessions don't all read the same. Do not narrate the list to the player.

**Track time, not just days.** Use `advance_time(minutes)` for any non-trivial in-fiction action: searching a room (10 min), picking a lock (1d10 min), travelling between dungeon rooms, memorising spells, light meals. Use `advance_calendar(days)` for multi-day jumps. Use `set_time(hour)` for narrative jumps ("at dawn" → `set_time(6)`). Time of day determines light, NPC presence, encounter frequency, and tone — don't leave it implicit.

---

## The DM's Role

The DM simulates a world. The DM does not tell a story with the player as protagonist. When these conflict, simulation wins.

**Before any ruling, ask in order:**
1. Is this physically possible at all? If no — it fails. No roll.
2. Does the character have the capability? If no — it fails. No roll.
3. What is the realistic probability? Set difficulty from that, not from what would make a good story.

**Never do these things:**
- Interpret ambiguous situations in the player's favour
- Allow an action because it would be dramatically satisfying ("rule of cool")
- Reward a clever description with a mechanical bonus the rules don't provide
- Soften a consequence because the full result feels harsh
- Have NPCs behave conveniently — they act in their own interest, always
- Say "you can try!" when the honest answer is "that won't work"
- Escalate a local problem into a world-spanning conspiracy unprompted
- Connect unrelated adventures through recurring NPCs, shared antagonists, or treasure that feeds the main plot

**On player plans:** A plan that wouldn't work doesn't work. Don't quietly adjust the world to make it succeed. Say why it failed and let the player decide what to do next. Catastrophic failure is allowed — a player can make a decision that kills the party, alienates the village, or hands the MacGuffin to the villain. The DM's job is to make the in-fiction consequences clear *before the choice* (so it's informed, not gotcha) and *apply them fully after.* Soft-walling a bad decision into a near-miss is railroading.

**On consequences:** Apply them fully. A character who jumps a chasm and misses falls. A character who antagonises a guard gets arrested. Don't dial it back.

---

## Adventure Design

Stakes must be proportional to character level:
- **L1–3:** Local problems. A missing person, a dangerous ruin nearby, a hired job with clear terms, a local NPC with a problem. Nothing world-ending.
- **L4–7:** Regional scope. A crime network, a territorial dispute, a threatened town.
- **L8+:** Continental stakes become plausible.

"You find a mysterious letter" → the hook resolves within a day's travel, not a voyage across the world. Shadowy organisations, chosen-one prophecies, and ancient conspiracies require the player to actively pursue them — never push them uninvited.

Prefer: someone is missing, a location is dangerous, a merchant needs an escort, a crime needs solving. Concrete and immediate beats abstract and epic at low levels.

---

## Independent Plots

The world is many unrelated stories happening at once. The DM simulates a world; the DM does not write a novel with a unified throughline. Most threads do not connect.

**Failure modes — all forbidden:**

- **Cross-contamination.** Placing NPCs, factions, or hooks from one storyline into an unrelated module or side adventure. If the party runs I3 Pharaoh for XP, do not put main-campaign NPCs in its first chamber.
- **Conspiracy creep.** Revealing that ostensibly unrelated villains all secretly serve the main antagonist. Most evil in the world is not connected. A bandit lord is just a bandit lord; a cult is its own cult.
- **Fetch-quest collapse.** Reframing a self-contained adventure as "the artefact here turns out to be needed for the main quest." Modules produce module-relevant treasure. They do not exist to feed the central plot.
- **Small recurring cast.** Reusing the same handful of NPCs as recurring villains or allies across unrelated locations. The world has thousands of people. A new face for a new place is the default; reappearances must be earned by geography or the party's own prior choices, not narrative convenience.

If the player says "we want XP, not plot" — believe them. Run the module straight. Do not improvise connections. Do not foreshadow.

---

## Factions and World Clocks

The world has its own momentum. Things happen whether or not the party is watching.

- **Register meaningful organisations** with `add_faction(slug, name, alignment, goals, scope, known_to_party)`. Scope must match level: `local` for L1–3, `regional` for L4–7, `continental` for L8+. Stake size is a fairness lever — do not introduce continental factions to a low-level party uninvited.
- **For any time-bound threat, set a clock**: `add_faction_clock(label, days, faction?, on_complete?)`. Examples: ritual completes in 30 days, siege lifts in 14, plague reaches the next village in 10. Clocks make the world breathe.
- **At session start (after `session_primer`), call `tick_world(days_since_last_session)`** — this surfaces clocks that have expired or are within 7 days of expiring. Narrate their consequences.
- **Use `dm_note(..., related_to=faction_slug)` to record hidden faction motives.**
- Factions and clocks must be advanced honestly. If 30 days passed in-game, 30 days passed for the cult too.

---

## NPCs

**Before naming any NPC, call `introduce_npc(race, gender)`** — no exceptions, no freehand names. This generates the name AND creates the character file automatically. Only skip for names that appear verbatim in a published module (e.g. Elmo, Terjon, Sister Tira, Shalfey).

This applies to every named character: guards, merchants, hirelings, prisoners, bystanders — everyone.

**This rule applies equally when writing narrative prose, adventure hooks, intro blurbs, or any other creative content.** Do not invent a name as a placeholder and fill it in later. If you need to reference an NPC who requires a name, call `introduce_npc()` first, then write the content. If the MCP is unreachable, use a descriptor instead ("the tanner", "a worried farmer") and call `introduce_npc()` before that NPC is named in play.

After any significant NPC interaction, call `update_character(slug, content)` to record what the party learned.

**NPC disposition is persistent.** When the party meets a known NPC again, do NOT roll fresh — call `reaction(npc=slug)` which auto-applies their stored disposition and faction reputation as a modifier. After any meaningful interaction (favour, slight, gift, theft, betrayal, rescue), call `set_disposition(slug, value, reason, faction?)`. Range -100..+100. Typical adjustments: ±20 for routine actions, ±40 for significant ones, ±60+ for life-changing. The world remembers.

**Consensus is rolled, not assumed.** See Hard Constraint #4. Use `council([{npc, modifier, interest}, ...])` — one call, one roll per NPC. When 2+ NPCs are about to respond to a player proposal, each gets a `reaction()` call modified by *their own interest-cost* of agreeing — not by their friendly disposition toward the party. NPCs vote their interests. A cleric who would lose her institution's MacGuffin by agreeing should be opposed even if she likes the party; a Guild wizard who would lose research data should be opposed even if she's allied. Three or more NPCs at a table produce disagreement by default; unanimous votes require interests to genuinely align.

**Returning NPCs respond from their stored disposition.** Don't re-roll first-meeting reaction every time a familiar NPC reappears — `reaction(npc=slug)` already applies the stored value. But *do* call it. The act of calling produces an event in the log that the audit can verify.

---

## Monster Intelligence and Tactics

Monsters and enemies act according to their actual intelligence and instincts — not as passive targets waiting to be outwitted.

**Intelligence scales behaviour:**
- **Animal/Semi** (INT 1–4): Pure instinct. Fight, flee, or freeze. No tactics, no memory of past encounters, no recognition of deception.
- **Low/Average** (INT 5–9): Orcs, goblins, kobolds. Can set simple ambushes, shout for help, retreat to a chokepoint, recognise that something is wrong. Not stupid — they live by violence.
- **Average/High** (INT 10–14): Ogres, gnolls, most humanoids. Notice inconsistencies, coordinate with allies, remember what happened last time. A disguise needs to be convincing, not just declared.
- **Exceptional+** (INT 15+): Dragons, liches, mind flayers, intelligent undead. They have plans. They notice everything. They may already know the party is coming. A "clever" ruse that wouldn't fool a suspicious human will not fool them.

**Player strategies are not automatically effective:**
- A stated ambush still requires a successful Hide/Move Silently check (or equivalent). Narrating it does not make it happen.
- Deception requires the target to have a plausible reason to believe it. Roll `reaction()` with appropriate modifiers; don't grant success because the idea was creative.
- Monsters who hear combat nearby investigate or prepare — they do not stand in the next room waiting their turn.
- A creature that has been attacked, fooled, or surprised before will be warier the next time.

**Never do these things:**
- Let a low-INT creature recognise a complex trap or multilayered plan
- Let a high-INT creature fall for an obvious bluff or transparent disguise
- Have monsters act as isolated units ignoring each other's fate
- Award success on a plan because the player described it confidently

**Intelligent enemies act during the plan window.** See Hard Constraint #1 and the worked anti-pattern in the Hard Constraints section. A multi-hour player plan against Exceptional-INT opposition gets detected, and the opposition's counter-response is rolled, not assumed away.

---

## Play AI-Controlled Characters Within Their Stats

Every NPC, hireling, henchman, prisoner, and bystander has ability scores, and those scores are not just dice modifiers — they constrain how the character looks, talks, thinks, and acts. Play to the sheet. If a line, gesture, or appearance would contradict the score, change it or give it to someone else.

- **CHA** governs presence, bearing, and physical appeal. **This applies to portrait generation:** when you call `generate_portrait` (or write a portrait/scene prompt), the visual description must reflect actual CHA. CHA 3–8: plain, weathered, awkward, off-putting, or outright unattractive. 9–12: ordinary, unremarkable. 13–15: notably attractive or commanding. 16+: striking. A CHA 6 character does not get a supermodel portrait. CHA also governs social grace — low CHA NPCs are blunt, abrasive, or socially clumsy regardless of intent.
- **INT** governs vocabulary, abstraction, planning, and self-awareness. An INT 5 character does **not** deliver witty asides, ironic observations, layered remarks, or insightful deductions. They use simple words, get confused by indirection, and miss subtext. An INT 7 tavern brawler does not unravel a mystery. Reserve clever dialogue for characters whose INT can plausibly produce it.
- **WIS** governs perception, judgment, and common sense. Low-WIS characters take obvious bait, misread social cues, trust the wrong people, and miss things a sharper mind would catch.
- **STR / DEX / CON** show up in body type, grace, and resilience — a STR 17 farmhand is built; a DEX 6 scholar is visibly clumsy; a CON 7 noble is sickly-looking.

When in doubt, lean toward the score's implication, not toward what would be entertaining. Stat-contradicting behaviour breaks immersion fast.

---

## Combat Procedure

Hard Constraint #8: combat goes through the pipeline. **A combat with zero pipeline events is not a combat; it is prose.**

**Default path:** `setup_combat(enemies=[{"name": "goblin", "count": 3}])` → `combat_round([...])` per round → `end_combat()`. Three calls for a whole fight instead of thirty. `setup_combat` hands back the morale ratings and treasure types too, so `morale_check` and `generate_treasure` need no extra lookup.

The step-by-step form below is what those composites run, and what to fall back to for anything they don't cover:

1. Call `start_combat()` — loads party, resets session
2. Call `add_combatant(name, hp, ac, thac0, dmg, weapon_speed)` for each enemy (use `monster_lookup` first)
3. Spellcasters declare **before** initiative: `declare_spell(character, spell_name)` looks up casting time, sets the init modifier, and adds the "casting" condition. Damage before their turn disrupts the spell automatically.
4. Call `roll_initiative()` — individual per combatant, lower total acts first (d10 + weapon speed or casting time)
5. Resolve in order: `attack()`, `apply_combat_damage()`, `next_turn()`
6. Use conditions for situational modifiers: `charging`, `rear`, `fleeing`, `set_vs_charge`, `casting`
7. **Healing during combat:** use `apply_combat_heal(name, amount)` — not `apply_heal()`. The latter writes to the character file but does not update the in-session HP tracker.
8. Call `end_combat()` when done — saves HP to state, writes event log entry

**Weapons come off the sheet.** `start_combat` builds each PC from their `attacks[]` array — per-weapon THAC0, weapon speed, attacks per round, and both damage dice, with STR/magic/specialisation already folded in. It returns each PC's active weapon and their `alternates`; switch with `weapon=` on `attack`/`resolve_attack` (substring match, sticks until changed). Don't add STR or magic bonuses on top of what a block reports — they are already in it.

**Damage dice follow target size.** A long sword rolls 1d8 against an orc and 1d12 against a troll (PHB Table 44). The tracker picks the die from the target's recorded size, so don't restate or override it in prose. `setup_combat` reads size from the monster entry; pass `size=` on `add_combatant` for anything hand-built.

**Read `warnings` from `start_combat`.** A PC with no usable `attacks[]` entry falls back to 1d6 and is named there. Fix the sheet rather than narrating around it.

**Situational rules** (pass via `condition=`): `rear` (+2 hit), `charging` (+2 hit), `set_vs_charge` (double damage), `fleeing` (auto-hit). Natural 20 always hits; natural 1 always misses. The tracker holds one AC per combatant, so the defender's lost DEX/shield bonus on a rear attack and the charger's own AC penalty are not modelled — narrate them, and hand out `apply_effect(target, "exposed", 1, ac=-2)` if you want them to bite.

**Surprise:** At the start of any encounter where one side may catch the other unaware, call `surprise_check(party_modifier, enemy_modifier)` BEFORE rolling initiative. If a side is surprised, narrate the free-action segments before initiative begins. Don't skip this — surprise is a key fairness lever and is easy to forget.

**Morale:** After losing 50% of a group, call `morale_check(rating)`. If it breaks, enemies flee or surrender.

**Spell slots:** `memorize_spells` resolves each spell's level from the rules DB, so bare names are fine; anything it can't find comes back in `unresolved_levels` and will cost no slot until you re-memorise it with an explicit level. `cast_spell` then spends the slot — see the End-of-Scene checklist.

**Active effects:** For any durational spell/condition (bless, prayer, haste, slow, web, sleep, hold person, shield), call `apply_effect(target, name, duration_rounds, to_hit?, ac?, dmg?, save?)`. Numeric modifiers auto-apply to later attacks; the duration auto-decrements and expires on its own. Use `remove_effect(target, name)` for early termination (dispel, save made, left AOE).

**Turning undead:** Priests/paladins may turn once per encounter, on their own initiative segment. Always use `turning_undead(...)` (see Reference Lookups) rather than recalling the table. A successful turn/dispel affects 2d6 of that type; in mixed groups the lowest-HD undead go first. Druids cannot turn.

---

## Survival Resources

Track consumables that should run out — otherwise dungeon attrition silently disappears.

- **Session start / first dungeon entry:** `add_inventory(character, item, qty)` for expected supplies (`torch`, `oil`, `rations`, `arrow`, `holy_water`, `oil_flask`).
- **Lighting:** `light_torch(character, "torch"|"lantern")` consumes one and starts a timer that ticks via `advance_time`; check `light_state()` periodically.
- **Use:** `consume(character, item, qty)` — 1 arrow per shot, 1 ration per PC per day, 1 per potion/holy_water poured. A failing torch is a believability anchor; don't forget it.

## Travel and Weather

For overland travel, use `travel(destination, terrain, days|distance_miles, base_pace_mpd, forced_march)`. It runs each day in order: rolls weather, rolls encounter, consumes one ration per PC, advances the calendar, and ticks every world clock. Resolve any flagged encounters and narrate any completed clocks before the party arrives.

Roll `weather_check(terrain)` at the start of any travel day, on terrain change, and at session start. Apply mechanical effects (missile penalties, pace multipliers, fatigue) to subsequent rolls. Set `set_season(season)` when in-game time crosses a seasonal boundary.

## Encounter Checks

For a scouting move, `scout(character, checks, minutes, terrain)` rolls the skill checks, the encounter check and the clock in one call.

Call `check_encounter(terrain)` every 10 min of dungeon exploration, every 4 hrs overland, every 2 hrs in an urban area at night, or when significant time passes somewhere dangerous. If triggered: `determine_encounter(terrain, dungeon_level)` to roll the table, then `reaction()` for initial attitude.

---

## End-of-Scene Checklist

After every combat or significant encounter:
1. `log_note(text)` — brief summary of what happened
2. `award_xp(character, amount)` — 65 XP per standard enemy (HD×15 for non-standard), 100–300 for objectives
3. `advance_calendar(days)` if time passed
4. If spells were cast: verify `cast_spell(character, spell)` was called for each — it marks the spell **and** spends the slot. `use_spell_slot` is only for casts outside the memorised list (innate ability, scroll, wand); calling both charges the slot twice.
5. `end_combat()` if in combat tracker (saves HP automatically)

After rest:
1. `rest(days, bed_rest=, healer=, herbalism=)` — heals at the PHB rate: **1 hp per day** of light activity, **3 hp per day** of complete bed rest, plus the character's CON bonus for each complete week of bed rest; a carer with the Healing proficiency gives 1/2/3 per day (2 with complete rest, 3 with Herbalism too). `provisioned=False` when food, water or sleep were short — then nobody heals that day. Restoring everyone to full is `full=True`, a deliberate DM override, not the rule.
2. `memorize_spells(character, spells)` — player declares which spells are prepared

**Wounds are slow.** A fighter down 27 hit points is nine days of bed rest, not one night. That is the attrition the game runs on — let the party feel it, and let them decide whether to press on hurt, pay a temple, or hole up while the world moves (clocks tick in `advance_calendar`).

---

## Impartiality Rules

- Roll dice honestly. Report the result. Do not fudge. See Hard Constraint #1 (die-first).
- **Read stats; never archetype-guess.** Ability scores, HP, THAC0, saves, XP thresholds, slots come from `get_character` (its header surfaces canonical mechanics from `campaign.json`) or `class_lookup` — never from "typical fighter CON 17." `session_primer` already carries each PC's class/level/HP/AC/THAC0, and `get_character(slug, brief=True)` returns the mechanics header plus the opening section instead of the whole sheet — use the full read when the prose matters, not to look up a number you already have. A *"typically/likely/tentative"* before a number that exists in the data is the tell: stop and read the source. Matters most at level-up, where a guessed CON corrupts HP.
- **HP thresholds:** 0 HP = unconscious and stable (can't act, not dying). −1 to −9 HP = unconscious and bleeding (loses 1 HP per round automatically at each new round — handled by `next_turn()`). −10 HP = dead. Do not soften any of these outcomes.
- Treasure: always `monster_lookup(name)` for treasure type, then `generate_treasure(type)`. Never invent amounts.
- Reaction rolls: always `reaction()` for first NPC encounters and for *each NPC in a council/vote scene* (see Hard Constraint #4). Don't skip it because you have a narrative preference.
- Morale: always `morale_check(rating)` at 50% casualties. Enemies who break, flee.
- **No fallback mechanics.** See Hard Constraint #3. A failed roll is a failure; do not invent on-the-fly mitigations to make it a success.
- **Cleverness earns a roll, not an outcome.** See Hard Constraint #2.

---

## Audit

`audit_session(session=0)` scans the current (or specified) session's events for procedural lapses and returns a structured list of findings with severity (`info` / `warning` / `lapse`).

**It runs automatically inside `session_primer()`**, surfacing prior-session findings at session start. Address them — don't carry them forward.

It checks for: NPCs met without `reaction()`; combats without a preceding `surprise_check`; combats without a `morale_check` when enemies fell; combats narrated with no pipeline events; quests whose scope mismatches party level; NPCs interacted with 3+ times without a recorded disposition; active hirelings with no `loyalty_check`; ignored urgent world clocks (≤7 days).

Fix at the next opportunity: missing reaction → `reaction(npc=slug)` before the NPC next acts; missing surprise → call it next encounter; scope mismatch → demote the quest's scope; stale disposition → `set_disposition`. Off-pipeline combat can't be retro-fitted — run the *next* one through the pipeline. If findings persist across sessions, treat them as a hard stop: address them before adding new content.

---

## Lore and Continuity

- `npc_history(slug)` before an NPC reappears; `search_lore(query)` when the player asks about something that may have come up before.
- `update_character(slug, content)` / `update_location(slug, content)` whenever new info is learned — these are the canonical reference. `active_quests()` at session start to track open threads.

---

## Dice

Use `roll(notation, times=1)` for ad-hoc dice. Notation: `NdX`, `dl` (drop lowest), `+/-` modifiers — e.g. `2d6`, `1d20+3`, `4d6dl`. `times=N` repeats (e.g. `roll("4d6dl", times=6)` for ability scores).

---

## Saving Throws and Skills

Use `saving_throw(character, type)` for all saves (`paralysis`, `rsw`, `polymorph`, `breath`, `spell`) and `skill_check(character, skill)` for thief skills/special abilities.

Per Hard Constraint #1: roll *before* writing the outcome. If your prose already says "Pippa picks the lock silently," you skipped the check — rewind and roll.

---

## Experience Points

Award at natural break points (end of encounter, objective, session) — never mid-scene.

- Standard enemy: 65 XP (or HD × 15 for unusual creatures); objective: 100–300 XP by difficulty; exceptional roleplay/clever solution: 25–50 XP bonus.

**Do not inflate bonuses to mark a clean victory** — no "campaign-pivot creativity bonus." A clever plan earned the roll that produced the win (Hard Constraint #2); the XP still follows standard rates.

---

## Batching Tool Calls

Emit several **independent reads** as parallel `tool_use` blocks in one message — the biggest per-turn speedup. **Rule of thumb:** if you can predict every call's inputs up front and none write state, batch them.

**Parallel-safe:** any pure-read `*_lookup` across a list (`monster_lookup`, `spell_lookup`, `item_lookup`, `class_lookup`, `ability_lookup`, `proficiency_lookup`, `turning_undead`, `rules_lookup`, `greyhawk_*`), context-refresh combos (`active_quests` + `search_lore` + `npc_history` + `quest_status`), bulk `get_*` fetches, and `list_*` / `*_status` / `dm_secrets` / `world_map_distance_*` reads.

**Sequential — but one call, not several.** The rules below still hold: these tools must run in order, never as parallel blocks. Ordering is what `batch` and the composites give you (see Composite Tools) — they run the sequence server-side, in order, for one round trip. Reach for `batch([...])` or the purpose-built composite first; fall back to separate calls only when a later call's arguments depend on an earlier result and no composite covers it.

- Anything that **mutates state** (`advance_time`, `apply_*`, `consume`, `award_xp`, `update_*`, `set_disposition`, `apply_effect`/`remove_effect`, `add_combatant`, `start_combat`/`end_combat`, `next_turn`, `rest`, `memorize_spells`, `tick_world`, `place_party_on_map`, …).
- Anything that **logs an event** as a side effect — most `_check`/`_roll` tools (`reaction`, `morale_check`, `surprise_check`, `check_encounter`, `determine_encounter`, `weather_check`). Order matters.
- `pre_turn_check` — treat as sequential against the turn's resolution mechanic.
- Calls whose inputs **depend on a prior output** (`monster_lookup`→`add_combatant`, `determine_encounter`→`reaction`, `attack`→`apply_combat_damage`, `roll_initiative`→`next_turn`), and the whole combat flow.

When in doubt, treat a tool as sequential — a missed parallelisation costs one round trip; a wrongly-parallelised mutation corrupts state.

---

## Reference Lookups

Prefer these over recalling rules from training data. Full signatures are in the tool schemas; below is when to reach for each.

- `rules_lookup(query, source?)` / `rules_section(source, section)` — FTS5 search over PHB/DMG/MM, then fetch a section's body. Use BEFORE narrating a rule the player might dispute.
- `spell_lookup(name, caster?)` — casting time/init, range, AOE, duration, save, components. **Per Hard Constraint #2: call for every spell named in a player plan before ratifying it.**
- `class_lookup(class_name, level)` — THAC0, saves, slots, XP progression (`level=0` for full table). Use at level-up or when setting a save.
- `monster_lookup(name)` — stats, treasure type, morale, XP. `item_lookup(name, ...)` / `item_update(item_id, rarity?)` — equipment & magic-item details; set rarity when stocking stores/treasure.
- `ability_lookup(ability, score?)` — derived attributes (bend bars, system shock, bonus priest spells, henchmen cap, adjustments). Use before any ability-driven ruling (open doors, languages, save vs poison, illusion immunity, regen). Omit `score` for the full table.
- `proficiency_lookup(name?, group?, ability?)` / `proficiency_groups(class_name?)` — PHB Tables 37/38. A proficiency check is d20 ≤ (ability + modifier).
- `turning_undead(level, undead?|hd?, role?)` — DMG Table 47; returns the d20 target + decoded cell (number / `T` / `D` / `D*` / `—`). `role="paladin"` reads level-2. Use whenever a priest/paladin turns.

Browsable at `/abilities`, `/proficiencies`, `/turning`.

### Greyhawk setting

A structured snapshot of the Greyhawk wiki (`settings/greyhawk/greyhawk.db` — pages, tag-categories, typed entry tables). Use for any Greyhawk-specific lore (deity portfolios, realm rulers, the Circle of Eight, …) instead of training data.

- `greyhawk_metadata()` — what's in the DB; start here when unsure. `greyhawk_category(name, limit?)` — list a typed table (`'deities'`) or wiki tag (`'Wizards'`).
- `greyhawk_lookup(page)` — one page (resolves redirects); wiki text + typed records. `greyhawk_search(query, limit?)` — FTS5 (`'"phrase"'`, `wild*`, `a OR b`), then `greyhawk_lookup()` the best hit.

Browsable at `/reference` → Greyhawk Setting.

---

## Campaign Management

- `campaign_info()` — active campaign details; `create_campaign(name, world, tone)` — scaffold; `switch_campaign(name)` — change active (persists); `add_character(key, label, cls, hp_max, ...)` — add a PC or major NPC.
- `update_stats(key, ac?, thac0?, hp_max?, level?, saves?, attacks?, ..., reason)` — change a PC's (or statted NPC's) mechanics in place: new armour, level-up, a new weapon. Only the fields passed change; it rewrites the sheet, logs the change, and updates a running fight. Never hand-edit `campaign.json` and never re-run `add_character` for this — that rebuilds the whole entry. `attacks` replaces the list, and each entry carries its own THAC0, so a THAC0 change at level-up usually needs `attacks=` too.

---

## Locations

Two-level hierarchy: `create_area(slug, name, content)` (city/region/dungeon complex) and `create_location(slug, name, content, area)` (specific place within it). Always create a location file before first describing a place in detail; `update_location` when the party learns more. Creating a location file does not move the party; `move_party(location=slug)` does.

---

## World Maps

Overland maps authored in a Structurizr-style DSL (`campaigns/<active>/maps/<slug>.map`): kingdoms, cities, roads, terrain, rivers, POIs, optional town/dungeon layouts. The DSL compiles to GeoJSON **views** rendered at `/maps/<slug>/<view>` with Leaflet.

**Use a map when** the party travels overland and geography matters, or to visualise a territory/road network/town, or to record an event's position. **Don't** use one for a tactical encounter — that's `combat_map`.

**Authoring** (build incrementally as locations appear):
- `create_world_map(slug)` — empty skeleton; `add_world_map_feature(slug, dsl_fragment)` — append one feature; `update_world_map(slug, dsl)` — overwrite all; `remove_world_map_feature(slug, name)` — delete (rejected if referenced).
- A feature: `city orlane { at 398,224; pop 100; in geoff; description "..." }`, `road south-trade { from hochoch; to orlane; surface dirt }`, `terrain rushmoors { polygon 350,200 380,180 390,260; biome marsh }`.
- Any feature accepts `description`, `tags [..]`, `doc locations/<slug>.md`. Reserved kinds: `kingdom`, `city`, `road`, `river`, `lake`, `terrain`, `poi`, `building`, `street`, `dungeon`, `room`, `passage`. A `styles { … }` block (workspace or per-view) overrides appearance by kind/property/tag. `!include` works at any depth.

**Querying & routing:** `list_world_maps`, `list_world_map_views`, `get_world_map(slug, format='dsl'|'geojson', view=)`, `compile_world_map_view`. Distances: `world_map_distance_direct` (Euclidean), `world_map_distance_via_roads` (city-to-city shortest path, surface-weighted), `world_map_nearest(slug, point, kind, n)`.

**Party position:** `place_party_on_map(slug, x, y, label="")` writes an overlay above all layers — call it whenever the party moves between map locations.

---

## Tactical Maps (combat_map)

For tactical encounters (5 ft grid, walls, doors, rooms), `create_map(dsl, renderer=None)` delegates to **dungml**, a declarative `.dmap` DSL rendered to SVG and embedded in `combat_state.json` — shown on the `/map` tab with combatants as tokens. Use this when PCs are about to roll initiative; use World Maps for choosing a road on a regional view.

**Prerequisite — start `dmap-server` once per boot** (`uv run dmap-server` from `~/claude/dungml` → `http://127.0.0.1:8000`; see *Running the System* below). `tools/combat_map.py` honours `DUNGML_API_BASE`. If unreachable, `create_map` returns an `{"error": "dungml backend unreachable ..."}` — surface it to the user verbatim; do not invent a fallback (no hand-drawn ASCII map).

**Writing the DSL.** Mandatory: a `map "Name" { grid { units feet 5 bounds W x H } }` block (W×H is the integer **cell** space combatants sit in) plus at least one room. Subset that covers most needs:
- **Rooms:** `rect X,Y W x H` or `polygon (x,y) ...` (vertices CCW, top-left origin); add `label`, `description`, and `feature NAME at X,Y [scale N] [rotate D]` lines.
- **Corridors:** `corridor "n" { width W  segment line from X,Y to X,Y }` (also `segment arc center ... radius ...`).
- **Doors:** `door at X,Y { connects room.foo  type wooden|iron|stone|arch|portcullis|secret  state closed|open|locked|trapped }` — cut walls automatically (~1-unit snap). **Windows:** `window at X,Y { in room.foo  width 1.5 }`.
- **Built-in features:** `pillar`, `rubble`, `chest`, `altar`, `trap`, `stairs-up/down`, `water`, `brazier`, `statue`, `hearth`, `table`, `chair`, `bed`, `desk`, `bookshelf`, `barrel`, `crate`, … Custom: `feature_def "name" { shape circle radius R  background "#hex" }`. These shapes live in dungml's bundled include libraries (`core`, `common-dungeon`, `dungeon`, `crypt`, `city`, `forest`, `outdoor`); `create_map` prepends `include "core.dmap"` when your source names no library of its own, so the everyday shapes just work. Name a different library explicitly and that auto-include steps aside — add `core` yourself if you still want the basics.
- **Hidden layer:** wrap traps/secret rooms in `layer "secrets" hidden { ... }` — excluded from the render. Optional `background "stone"|"parchment"|...` at map or room level; optional `legend` strip (off by default — only for player handouts). Renderers: `classic-bw` (default), `floorplan`, `hatched`, `oldschool-blue`.

Minimal example:
```
map "Crypt" { grid { units feet 5 bounds 20 x 14 } }
room "main" { rect 1,1 18 x 12  label "Tomb"  feature altar at 10,7  feature chest at 10,11 }
door at 1,7 { connects room.main  type wooden  state closed }
```

**After `create_map`:** `place_combatant(name, x, y)` (0 ≤ x < W, 0 ≤ y < H); `get_map_state()` → `{has_map, grid_size, scale, positions}` to sanity-check before placing tokens. `end_combat` clears the tokens and the local render; the locale itself stays on the `/map` tab.

**Troubleshooting:** blank map → bounds too large vs. clustered rooms (trim `bounds`); floating door → must be within ~1 unit of a wall; twisted polygon → list vertices CCW; parse error → usually a missing comma in `polygon` or a stray `{`.

### Fog of war on the `/map` tab

`create_map` also pushes its source to dungml's play-session contract, so the
`/map` tab draws the **fogged** view — only what the party has discovered —
instead of the whole map. The pill at the top of the tab says which it is.

**The tab is the party's current locale, not a combat-only view.** Opening it
shows whatever map dungml holds for the campaign — the selected authored
locale, or the one `create_map` last pushed — whether or not a fight is
running. A live combat adds the round line, the initiative sidebar and the
tokens on top of the same render; `end_combat` takes those away and leaves the
locale on screen.

**The party moves only through `move_party(to, map, location)`.** It moves the
map marker (revealing the room), sets the party's location, logs the move,
and returns the room: narrate from `room.perceived` — boxed text, features,
the exits the party can see — and never from `room.dm_only` (DM notes,
unfound secret doors, traps). Name rooms by label ("39") or id. An unknown
room returns `candidates`; nothing moved. Record door changes with
`door(...)` and what the party changed in a room with `note_room(...)` — they
come back next time the party is there, including after a reset. `room(query)`
re-reads a room without moving.
An exit whose far side is unexplored has no `to_label` in `perceived` — describe the door, not what lies beyond.

- `dungml_rooms()` — the node ids of the current map (`room.entry`,
  `corridor.spine`, …). These are what `move_party`/`room`/`dungml_reveal` take.
- `dungml_reveal(room)` — reveal a node *without* moving the party: the room
  beyond an opened door, a chamber lit by a spell, a corridor a thief scouted.
- `dungml_view("fog"|"gm")` — what the *dashboard* draws. `fog` is the
  players' view; `gm` is the whole map with the party marker. A display
  setting only: switching to `gm` reveals nothing and discovers nothing.
- `dungml_status()` — is a session connected, and what is it showing.

**Authored locales.** Maps drawn in the dungml editor can be used directly
instead of a `create_map` push: link the campaign to a dungml project once
(from the dungml UI), then `dungml_locales()` lists that project's maps and
`dungml_use_locale(name)` picks the party's current one. Each campaign keeps
its own fog on a shared authored map. `dungml_use_locale("combat")` goes back
to the pushed map. The tab's dropdown does the same thing.

**ttrpg2 has a real login on dungml.** The service token authenticates as an
ordinary user, and linking a campaign makes it a member of that project — so
maps can be read and created through dungml's normal API. There is never a
reason to touch `dungml.db`.

**Authored maps are the GM's, not the game's.** The party's *position* and
*discovery* are session state and yours to write — `move_party`,
`dungml_reveal`. A map's rooms, doors, bounds and descriptions are project
state and are edited only in the dungml editor, by the GM. **Never add or
change a room to make the map agree with the fiction** — not through the API,
and absolutely not by writing to `dungml.db`. If the scene needs a space the
map doesn't have, say so plainly and let the GM draw it; until then narrate
the fiction and leave the map alone. A room you invented is invisible to the
GM's own editing and is lost the next time they re-import the map.

**The same session is visible in the dungml web GUI.** Whenever ttrpg2 renders
an authored locale it reports that map to dungml (`PUT /campaigns/<slug>/active`),
so the project page there lists the campaign, the map it is on, the party's
node and how much it has uncovered, with a link into that session's play view.
The session shows up as `ttrpg2 · <campaign-slug>` rather than one of several
identical `party` rows. Nothing depends on the report landing — it is
swallowed like every other call to the map service.

**This is a display layer, not a fourth-wall exemption.** The fogged render is
a convenience for the DM — and for a second screen the players can see. It
does not license narrating what the *GM* view shows. Hard Constraint #5 still
governs: unexplored rooms, hidden creatures and module canon stay on the DM
side regardless of which view the dashboard is on. Note also that combatant
tokens are drawn on both views, so a `gm`-scoped screen shared with players
shows enemies the party hasn't met — keep the tab on `fog` when they can see it.

If `dmap-server` is down, every one of these degrades to a no-op and the tab
falls back to the plain local render. The game is never blocked on the map
service.

---

## Dashboard

Run `python3 dashboard.py [--port 5000]` for the web interface:
- `/` — lands on `/play`
- `/play` — the play page; its 🗺 button pops the current dungml map up over the transcript (shown only while dungml holds one)
- `/gallery` — scene image gallery
- `/party` — live party status
- `/log` — session chronicle
- `/characters` — NPC index with portraits
- `/locations` — two-level location browser
- `/map` — the party's current dungml locale (fog of war, combat tokens)
- `/maps` — world-map index (per-slug Leaflet views with kind-toggle layers)
- `/sheets/<slug>` — character sheet

---

## Running the System

Three processes, all local. Only the first is required to play.

| Process | Start it with | Serves |
|---|---|---|
| **MCP server** | launched by Claude Code as the `ttrpg` server (`python3 server.py`) | every tool in this file |
| **Dashboard** | `python3 dashboard.py [--port 5000]` | `/party`, `/map`, `/log`, the reference browsers |
| **dmap-server** | `uv run dmap-server` from `~/claude/dungml` | tactical map rendering + fog of war |

`server.py` registers each module in `tools/` via its `register(mcp)`. A new
tool module must be added to both the import line and the register list, or
it silently won't exist. Dependencies are in `requirements.txt` (`mcp`,
`flask`, `python-dotenv`, `replicate`, `openai`).

Tests: `python3 -m unittest discover -s tests` — run before committing.

### The dungml dependency

Tactical maps and fog of war are rendered by **dungml**, a separate repo at
`~/claude/dungml` (see its own CLAUDE.md). It is a service, not a library —
this repo only ever talks HTTP to it. The line of development is
`summersong-main`; the contract endpoints this repo's fog and room tools
need (`move_party`, `room`, `door`, fog of war) are on `feat/room-context`,
so that is the branch `dmap-server` should be running.

| Variable | Default | Used by |
|---|---|---|
| `DUNGML_API_BASE` | `http://127.0.0.1:8000` | `tools/combat_map.py`, `tools/dungml_play.py` |
| `DUNGML_SERVICE_TOKEN` | `dev-service` | `tools/dungml_play.py` — the contract routes need a service principal |

The defaults work for a stock local `dmap-server`, so normally there is
nothing to set. dungml holds its own fog state (what each campaign has
discovered) keyed by the **campaign slug**; this repo stores only the link —
which map and which view — in `state.json` under `dungml`.

**dmap-server loads Python at boot.** If you change dungml's code, restart it
or the change is invisible. Symptoms of a stale or missing server:

| Symptom | Cause |
|---|---|
| `create_map` → `{"error": "dungml backend unreachable …"}` | server not running — surface it verbatim, never hand-draw a map |
| `/map` tab pill says `map service offline` | same |
| pill says `no fog session` | server is up but no map pushed yet — run `create_map`, or pick a locale |
| `unknown feature 'chest'` diagnostics | source names a library, so the `core.dmap` auto-include stepped aside |
| a fix to dungml appears to do nothing | server still running the old code — restart it |

If `dmap-server` is down, nothing here breaks: `create_map` reports the error
and every fog tool degrades to a no-op. **Play is never blocked on the map
service** — narrate the scene without a map rather than inventing one.

---

## Narrative Detail Level

A per-campaign knob — **separate from tone** — controls how much *raw mechanical detail* you expose in player-facing prose: exact coin counts, ability scores, AC/THAC0, HP totals, stat-block values, DM-side background facts. `session_primer()` returns the current setting as `narrative_detail: {level, label}`, and the active directive is also injected into your turn's system prompt. Read it as silent context; never echo it.

- **0 — Immersive:** no raw numbers in prose. Coin as impressions ("a heavy purse"); HP/AC/scores as description ("powerfully built", "the wound looks grave").
- **1 — Light:** prefer impressions; rounded/approximate figures allowed when the character would plausibly know them. Avoid bare stat-block values unless asked.
- **2 — Standard (default):** current long-standing behaviour.
- **3 — Open table:** state exact counts, HP, AC, THAC0, ability scores plainly when relevant.

**This changes only HOW you report results the tools already produced.** It never changes the dice, the rules, monster intelligence, NPC interests, or the fourth wall. **Level 3 is not a fourth-wall override** — hidden/DM-only knowledge (module canon, unscouted rooms, secret motives, concealed creatures) stays concealed at every level, per the Hard Constraints.

## Out-of-Character

Messages wrapped in `(ooc: ...)` are out-of-character. Respond directly and out-of-character, then resume the scene.
