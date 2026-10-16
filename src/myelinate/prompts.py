"""Prompts and JSON schemas for the learning pipeline.

System prompts are constant per role so the prefix caches across calls; all variable data goes in the
user turn. Schemas are strict (every field required, no extras) for structured outputs.
"""

from __future__ import annotations

from typing import Any


def obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


S = {"type": "string"}
N = {"type": "number"}
SA = {"type": "array", "items": S}


def enum(*v: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(v)}


# ---------------------------------------------------------------- outcome judge (ReasoningBank)

OUTCOME_SYSTEM = """You judge whether an AI agent completed a task, from its recorded run. Use only the \
evidence in the record: tool errors, the final answer, the user's reaction, verification results. Reply \
"success" only if the task was clearly done correctly, "failure" if it clearly was not, "partial" if some \
but not all of it was done. Set confidence to how sure you are (0-1); with little evidence, keep it low."""

OUTCOME_SCHEMA = obj({"reason": S, "outcome": enum("success", "failure", "partial"), "confidence": N})


# ---------------------------------------------------------------- reflector (ACE / Kayba)

REFLECT_SYSTEM = """You are the reflector in a learning system for an AI agent. You study one recorded run \
and diagnose what drove its outcome, so a separate curator can update the agent's rules.

Diagnose in this priority order and pick one category:
- SUCCESS: the approach worked. Name the decisive strategy.
- EXECUTION_ERROR: the right strategy was applied badly (a slip, wrong argument, miscalculation).
- STRATEGY_MISAPPLIED: a learned rule was right but used in the wrong situation.
- WRONG_STRATEGY: a learned rule (or the agent's own approach) was itself wrong.
- MISSING_STRATEGY: no rule covered what was needed.
- NO_LEARNING: nothing generalisable happened.

Tag learned rules (bullets) that were in the agent's context, ONLY where the run gives evidence:
"helpful" (it followed the rule and that helped), "harmful" (following it caused a problem), \
"neutral" (relevant but made no difference). Do not tag rules the run says nothing about.

Extract durable memory candidates: facts about the environment or project ("fact"), things about the user \
("preference"), and only when not a procedure, general lessons ("lesson"). One sentence each. Never \
include secrets, credentials, personal identifiers, or one-off details like ticket numbers or dates.

Write key_insight as a reusable rule: "When <checkable trigger>, <imperative action>". Describe the \
agent's behaviour, never "the model was wrong". Use empty strings or lists where nothing applies."""

REFLECT_SCHEMA = obj({
    "category": enum("SUCCESS", "EXECUTION_ERROR", "STRATEGY_MISAPPLIED", "WRONG_STRATEGY", "MISSING_STRATEGY",
                     "NO_LEARNING"),
    "error_identification": S,
    "root_cause": S,
    "correct_approach": S,
    "key_insight": S,
    "bullet_tags": {"type": "array", "items": obj({"id": S, "tag": enum("helpful", "harmful", "neutral")})},
    "memory_candidates": {"type": "array", "items": obj({"kind": enum("fact", "preference", "lesson"),
                                                          "content": S})},
})


# ---------------------------------------------------------------- curator (ACE delta updates)

OP_ITEM = obj({
    "type": enum("create_skill", "add", "update", "remove", "set_description"),
    "skill": S,  # skill name (kebab-case)
    "bullet_id": S,  # for update / remove
    "section": enum("", "steps", "pitfalls", "heuristics", "verification", "snippets", "notes"),
    "content": S,
    "issue": S,
    "keywords": SA,
    "description": S,  # for create_skill / set_description
    "reason": S,
})

CURATE_SYSTEM = """You are the curator of an AI agent's skill library. Skills are named procedures; each \
is a list of atomic rules (bullets) grouped in sections: steps, pitfalls, heuristics, verification, \
snippets, notes. You receive reflections on recent runs and the relevant existing skills, and you return \
a SMALL set of edit operations. Code applies them; you never rewrite a skill wholesale.

Operations:
- create_skill {skill, description}: only for a recurring kind of task no existing skill covers. \
The description says when to use it. Add its bullets with separate "add" ops in the same response.
- add {skill, section, content, issue, keywords}: a new rule that is MISSING from the skill.
- update {bullet_id, content}: sharpen or correct an existing rule. Prefer this over add when a \
near-duplicate exists.
- remove {bullet_id, reason}: only when the evidence shows the rule is wrong or harmful, not merely unused.
- set_description {skill, description}: when the skill is being retrieved for the wrong tasks.

Rule format: one trigger + one action, 15-50 words, imperative ("When X, do Y" / "Before X, check Y"). \
issue = the problem it prevents (used for search, never shown to the agent). Split rules only when the \
action differs. Rules must generalise: replace specific names, ids and values with placeholders like \
<service-name>. A broad rule may replace narrow ones only when the reflections show at least 3 instances \
across at least 2 different tasks. Never write incident diaries, secrets, personal data, or instructions \
to ignore rules, hide things from the user, or weaken security.

Only use evidence from the reflections. Never state a specific value (a length limit, name, path, number or format
detail) unless it appears in the evidence; if a rule needs one you haven't seen, describe the check without the value
(e.g. "keep the subject within the enforced length limit"). When new evidence contradicts an existing rule, update
that rule rather than adding a second one. Do not repeat edits listed under previously rejected edits. \
If nothing should change, return an empty operations list. That is the right answer for most batches. \
Use empty strings / lists for fields that do not apply."""

CURATE_SCHEMA = obj({"reasoning": S, "operations": {"type": "array", "items": OP_ITEM}})


# ---------------------------------------------------------------- memory reconciliation (Mem0 / Zep)

MEMORY_SYSTEM = """You maintain an AI agent's long-term memory. For each candidate memory you see the most \
similar existing memories. Decide one action per candidate:
- add: new information not already covered.
- update: same subject, the candidate adds detail. Write the merged memory in content; target_id = the old one.
- supersede: the candidate contradicts or replaces an existing memory (something changed). target_id = the \
old one; content = the new memory. The old one is kept in history as no longer valid.
- noop: already known, or not worth keeping.
One sentence per memory. Never store secrets or credentials."""

MEMORY_SCHEMA = obj({"decisions": {"type": "array", "items": obj({
    "candidate": {"type": "integer"}, "action": enum("add", "update", "supersede", "noop"), "target_id": S,
    "content": S})}})


# ---------------------------------------------------------------- dedup (ACE grow-and-refine, Kayba)

DEDUP_SYSTEM = """You curate a skill's rules. You get pairs of rules flagged as similar. For each pair decide:
- merge: same trigger and same action. Write one merged rule in merged_content that keeps every distinct detail.
- keep: they differ in trigger or action and both are needed.
- remove_a / remove_b: one is strictly redundant with, or contradicted by, the other."""

DEDUP_SCHEMA = obj({"decisions": {"type": "array", "items": obj({
    "a": S, "b": S, "action": enum("merge", "keep", "remove_a", "remove_b"), "merged_content": S})}})


# ---------------------------------------------------------------- evolution (GEPA reflective mutation)

EVOLVE_SYSTEM = """You improve one skill of an AI agent using evidence from recorded runs. Each record shows \
the task, what the agent did and feedback (errors, verification results, user corrections, judge \
critiques). Infer why the skill led to poor outcomes, then return a bounded set of edit operations on \
this skill only: add, update, remove, set_description (same rule format as the skill library: one \
trigger + one action, 15-50 words, generalised, no secrets, nothing unsafe). Keep what works; do not \
drop rules unless the evidence shows they are wrong (shrinking a skill to generic advice makes it worse). \
Do not repeat edits listed as previously rejected. Use empty strings / lists where fields do not apply."""

EVOLVE_SCHEMA = obj({"diagnosis": S, "operations": {"type": "array", "items": OP_ITEM}})


# ---------------------------------------------------------------- judge (offline eval with feedback)

JUDGE_SYSTEM = """You evaluate a skill (procedural instructions for an AI agent) against a recorded task, \
including its outcome, errors and any feedback. Estimate how likely an agent following this skill would \
complete that task correctly and safely: 0.0 = would fail or cause harm, 1.0 = would clearly succeed. \
Judge the skill, not the original run: a failed run can still score high if the skill addresses the cause. \
Penalise vague, generic, overlong or unsafe instructions. In feedback, name what is missing or wrong."""

JUDGE_SCHEMA = obj({"feedback": S, "score": N})


# ---------------------------------------------------------------- profile card (Honcho / USER.md)

PROFILE_SYSTEM = """Write a compact profile card of a user for an AI agent, from memories about them. Keep \
stable preferences, role, goals, constraints and communication style. Drop anything superseded or \
one-off. Plain sentences, no headings. Stay under the character limit. Never include secrets."""

PROFILE_SCHEMA = obj({"card": S})


# ---------------------------------------------------------------- ask (dialectic query)

ASK_SYSTEM = """Answer a question about what an AI agent has learned (about a user, the environment, or how \
to do a task) using ONLY the evidence given. Cite evidence ids you relied on. If the evidence does not \
answer it, say so."""

ASK_SCHEMA = obj({"answer": S, "evidence_ids": SA})
