"""Skill documents as bullet sets: delta ops, deterministic application, rendering, SKILL.md import/export.

Pure functions only. The governance layer applies the same ops to the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .db import short_id
from .models import SECTIONS

SECTION_TITLES = {"steps": "Steps", "pitfalls": "Pitfalls", "heuristics": "Heuristics",
                  "verification": "Verification", "snippets": "Snippets", "notes": "Notes"}
OP_TYPES = ("create_skill", "add", "update", "remove", "set_description")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
CITE = re.compile(r"\[(b-[0-9a-f]{8})\]")

# Incident-diary shape (Hermes linter): rules must be reusable procedures, not records of one event.
_DIARY = re.compile(r"(?i)\b(yesterday|today|last (week|night)|on (mon|tues|wednes|thurs|fri|satur|sun)day|"
                    r"\d{4}-\d{2}-\d{2}|INC-?\d+|JIRA-?\d+|ticket #?\d+|we (tried|found) that)\b")


def slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", name.strip().lower())).strip("-")[:64]


def lint_bullet(content: str, max_words: int) -> list[str]:
    out = []
    words = len(content.split())
    if words > max_words:
        out.append(f"lint:too-long:{words}w")
    if words < 4:
        out.append("lint:too-short")
    if _DIARY.search(content):
        out.append("lint:incident-log-shape")
    return out


# ---------------------------------------------------------------- op validation


@dataclass
class OpBatch:
    ops: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    skills_touched: set[str] = field(default_factory=set)  # skill names

    @property
    def risk(self) -> str:
        """low: purely additive. medium: edits existing rules. high: removes rules / changes routing."""
        kinds = {o["type"] for o in self.ops}
        if "set_description" in kinds or any(o["type"] == "remove" and not o.get("merged_into") for o in self.ops):
            return "high"
        if kinds & {"update", "remove"}:
            return "medium"  # rule edits, or removals that merge a duplicate into a kept rule
        return "low"


def normalise_ops(raw: list[dict[str, Any]], skills: dict[str, dict[str, Any]], bullets: dict[str, dict[str, Any]],
                  max_ops: int, *, restrict_skill: str | None = None) -> OpBatch:
    """Validate LLM-proposed ops against the current library.

    skills: name -> {"id", "description"}; bullets: id -> {"skill" (name), "content", ...}.
    Invalid ops are dropped (with a reason) rather than failing the whole batch.
    """
    b = OpBatch()
    created: set[str] = set()
    seen_content: set[str] = set()
    for o in raw:
        t = o.get("type")
        if t not in OP_TYPES:
            b.dropped.append(f"unknown op {t!r}")
            continue
        op: dict[str, Any] = {"type": t}
        if t in ("update", "remove"):
            bid = (o.get("bullet_id") or "").strip()
            if bid not in bullets:
                b.dropped.append(f"{t}: unknown bullet {bid!r}")
                continue
            sk = bullets[bid]["skill"]
            if restrict_skill and sk != restrict_skill:
                b.dropped.append(f"{t}: bullet {bid} outside {restrict_skill}")
                continue
            op |= {"bullet_id": bid, "skill": sk}
            if t == "update":
                content = (o.get("content") or "").strip()
                if not content or content == bullets[bid]["content"]:
                    b.dropped.append(f"update {bid}: no change")
                    continue
                op |= {"content": content, "issue": o.get("issue") or "", "keywords": o.get("keywords") or []}
            else:
                op["reason"] = o.get("reason") or ""
                into = o.get("merged_into")
                if into and into in bullets and into != bid:
                    op["merged_into"] = into
        else:
            name = slug(o.get("skill") or "")
            if not _NAME.match(name):
                b.dropped.append(f"{t}: bad skill name {o.get('skill')!r}")
                continue
            if restrict_skill and name != restrict_skill:
                b.dropped.append(f"{t}: outside {restrict_skill}")
                continue
            op["skill"] = name
            if t == "add" and o.get("new_id"):
                op["new_id"] = o["new_id"]
            if t == "create_skill":
                if name in skills or name in created:
                    continue  # already exists: subsequent adds simply target it
                desc = (o.get("description") or "").strip()
                if not desc:
                    b.dropped.append(f"create_skill {name}: no description")
                    continue
                op["description"] = desc
                created.add(name)
            elif t == "set_description":
                desc = (o.get("description") or "").strip()
                if name not in skills or not desc:
                    b.dropped.append(f"set_description {name}: unknown skill or empty")
                    continue
                op["description"] = desc
            else:  # add
                content = (o.get("content") or "").strip()
                key = " ".join(content.lower().split())
                if not content or key in seen_content:
                    continue
                if name not in skills and name not in created:
                    b.dropped.append(f"add: skill {name} does not exist and is not created in this batch")
                    continue
                section = o.get("section") if o.get("section") in SECTIONS else "notes"
                seen_content.add(key)
                op |= {"section": section, "content": content, "issue": o.get("issue") or "",
                       "keywords": [k for k in (o.get("keywords") or []) if isinstance(k, str)][:5]}
        if o.get("reason") and "reason" not in op:
            op["reason"] = o["reason"]
        b.ops.append(op)
        b.skills_touched.add(op["skill"])

    # Bounded edits: keep create_skill + at most max_ops content ops (prefer fixes over additions).
    creates = [o for o in b.ops if o["type"] == "create_skill"]
    rest = sorted((o for o in b.ops if o["type"] != "create_skill"),
                  key=lambda o: {"update": 0, "remove": 1, "set_description": 2, "add": 3}[o["type"]])
    if len(rest) > max_ops:
        b.dropped += [f"clipped: {o['type']} {o.get('bullet_id') or o.get('content', '')[:40]}" for o in rest[max_ops:]]
        rest = rest[:max_ops]
    b.ops = creates + rest
    b.skills_touched = {o["skill"] for o in b.ops}
    return b


def simulate(bullets: list[dict[str, Any]], description: str, ops: list[dict[str, Any]],
             skill: str) -> tuple[list[dict[str, Any]], str]:
    """Apply ops for one skill to an in-memory bullet list (used for canaries and offline evaluation)."""
    out = [dict(b) for b in bullets]
    by_id = {b["id"]: b for b in out}
    for o in ops:
        if o["skill"] != skill:
            continue
        if o["type"] == "add":
            pos = max((b.get("position", 0) for b in out if b["section"] == o["section"]), default=0) + 1
            nb = {"id": o.get("new_id") or short_id("b"), "section": o["section"], "content": o["content"],
                  "issue": o.get("issue", ""), "keywords": o.get("keywords", []), "position": pos}
            out.append(nb)
            by_id[nb["id"]] = nb
        elif o["type"] == "update" and o["bullet_id"] in by_id:
            by_id[o["bullet_id"]]["content"] = o["content"]
        elif o["type"] == "remove" and o["bullet_id"] in by_id:
            out.remove(by_id.pop(o["bullet_id"]))
        elif o["type"] in ("set_description", "create_skill"):
            description = o.get("description") or description
    return out, description


def diff_ops(skill: str, before: list[dict[str, Any]], before_desc: str, after: list[dict[str, Any]],
             after_desc: str) -> list[dict[str, Any]]:
    """Net delta between two bullet sets (used to turn an evolved candidate into a reviewable changeset)."""
    prev = {b["id"]: b for b in before}
    nxt = {b["id"]: b for b in after}
    ops: list[dict[str, Any]] = []
    for bid, b in nxt.items():
        if bid not in prev:
            ops.append({"type": "add", "skill": skill, "section": b["section"], "content": b["content"],
                        "issue": b.get("issue", ""), "keywords": b.get("keywords", []), "new_id": bid})
        elif b["content"] != prev[bid]["content"]:
            ops.append({"type": "update", "skill": skill, "bullet_id": bid, "content": b["content"],
                        "issue": b.get("issue", ""), "keywords": b.get("keywords", [])})
    ops += [{"type": "remove", "skill": skill, "bullet_id": bid, "reason": "evolution"} for bid in prev if bid not in nxt]
    if after_desc != before_desc:
        ops.append({"type": "set_description", "skill": skill, "description": after_desc})
    return ops


# ---------------------------------------------------------------- rendering


def ordered(bullets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable order (section, position, id) keeps injected text byte-identical between runs -> prompt cache hits."""
    rank = {s: i for i, s in enumerate(SECTIONS)}
    return sorted(bullets, key=lambda b: (rank.get(b["section"], 99), b.get("position", 0), b["id"]))


def render_bullets(bullets: list[dict[str, Any]], with_ids: bool, limit: int | None = None) -> str:
    lines: list[str] = []
    current = None
    for b in ordered(bullets)[:limit]:
        if b["section"] != current:
            current = b["section"]
            lines.append(f"#### {SECTION_TITLES.get(current, current.title())}")
        prefix = f"[{b['id']}] " if with_ids else ""
        lines.append(f"- {prefix}{b['content']}")
    return "\n".join(lines)


def to_skill_md(name: str, description: str, version: int, tags: list[str], bullets: list[dict[str, Any]]) -> str:
    """agentskills.io-compatible SKILL.md; bullet ids ride along in comments so re-import keeps provenance."""
    parts = [f"---\nname: {name}\ndescription: {description}\nmetadata:\n  version: {version}\n"
             f"  tags: [{', '.join(tags)}]\n  learned_by: myelinate\n---\n\n# {name}\n"]
    current = None
    for b in ordered(bullets):
        if b["section"] != current:
            current = b["section"]
            parts.append(f"\n## {SECTION_TITLES.get(current, current.title())}\n")
        parts.append(f"- {b['content']} <!-- {b['id']} -->\n")
    return "".join(parts)


# ---------------------------------------------------------------- import


def _section_for(heading: str) -> str:
    h = heading.lower()
    for keys, sec in ((("pitfall", "mistake", "avoid", "gotcha", "caveat", "don't", "never"), "pitfalls"),
                      (("verif", "check", "test", "validat", "confirm"), "verification"),
                      (("snippet", "code", "example", "template", "command"), "snippets"),
                      (("step", "procedure", "how to", "workflow", "instructions", "process"), "steps"),
                      (("heuristic", "tip", "strateg", "principle", "guideline", "best practice"), "heuristics")):
        if any(k in h for k in keys):
            return sec
    return "notes"


def parse_skill_md(md: str) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Parse an existing SKILL.md (Anthropic / Hermes / agentskills.io). Returns name, description, bullets."""
    name = desc = None
    body = md
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", md, re.S)
    if m:
        body = md[m.end():]
        for line in m.group(1).splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("'\"")
            elif line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("'\"")
    bullets: list[dict[str, Any]] = []
    section = "steps"
    para: list[str] = []
    code: list[str] | None = None

    def flush_para() -> None:
        if para:
            text = " ".join(para).strip()
            if len(text.split()) >= 4:
                bullets.append({"section": "notes" if section == "steps" else section, "content": text})
            para.clear()

    for raw in body.splitlines():
        line = raw.rstrip()
        if code is not None:
            if line.strip().startswith("```"):
                bullets.append({"section": "snippets", "content": "\n".join(code + [line])})
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush_para()
            code = [line]
            continue
        hm = re.match(r"^#{1,6}\s+(.*)", line)
        if hm:
            flush_para()
            section = _section_for(hm.group(1)) if not line.startswith("# ") else section
            continue
        lm = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)", line)
        if lm:
            flush_para()
            text = re.sub(r"\s*<!--\s*(b-[0-9a-f]{8})\s*-->\s*$", "", lm.group(1)).strip()
            if text:
                bullets.append({"section": section, "content": text})
            continue
        if line.strip():
            para.append(line.strip())
        else:
            flush_para()
    flush_para()
    return name, desc, bullets
