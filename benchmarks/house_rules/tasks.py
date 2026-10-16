"""House-rules benchmark: tasks whose success depends on team conventions the agent can't know up front.

Each family has deterministic verifiers that return CI-style feedback, like a linter or review bot would. Nothing
about the rules is ever put in the agent's prompt; the only way to learn them is from verification feedback.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class Task:
    family: str
    prompt: str
    check: Callable[[str], list[str]]  # returns violated-rule messages; empty = pass


# ---------------------------------------------------------------- 1. commit messages (commitlint-style)

def _commit_check(ticket: str) -> Callable[[str], list[str]]:
    def check(out: str) -> list[str]:
        lines = [ln.rstrip() for ln in out.strip().splitlines()]
        errs = []
        subject = lines[0] if lines else ""
        if not re.fullmatch(r"(feat|fix|docs|refactor|perf|test|chore)\([a-z0-9-]+\): [a-z][^\n]{2,}[^.]", subject):
            errs.append(f"commitlint: subject must be 'type(scope): description' in lower case without a final period "
                        f"(got {subject!r})")
        elif len(subject) > 60:
            errs.append(f"commitlint: subject must be at most 60 characters (got {len(subject)})")
        if len(lines) < 3 or lines[1] != "":
            errs.append("commitlint: a blank line and a body explaining why are required after the subject")
        if not lines or lines[-1] != f"Refs: {ticket}":
            errs.append(f"commitlint: the last line must be the trailer 'Refs: {ticket}'")
        return errs
    return check


COMMITS = [
    ("Fixed a crash in the payments webhook handler when the signature header is missing.", "PAY-142"),
    ("Added pagination to the orders listing endpoint.", "ORD-87"),
    ("Upgraded the Postgres driver to the latest major version.", "PLAT-311"),
    ("Removed the unused legacy CSV exporter from the reporting service.", "REP-19"),
    ("Made the retry backoff in the email sender exponential instead of fixed.", "NOTIF-56"),
    ("Documented the environment variables for the search service.", "SRCH-7"),
    ("Cached exchange rates for five minutes to cut API calls.", "FX-204"),
    ("Fixed the timezone bug in the invoice due-date calculation.", "BILL-90"),
    ("Added unit tests for the discount code validator.", "CHK-33"),
    ("Renamed the internal user_service module to accounts.", "ACC-12"),
]

# ---------------------------------------------------------------- 2. SQL migrations (migration-lint)

def _migration_check(out: str) -> list[str]:
    sql = re.sub(r"```\w*", "", out).strip()
    up = sql.split("-- down:")[0]
    errs = []
    if not re.match(r"(?is)^\s*BEGIN;", up) or not re.search(r"(?is)COMMIT;\s*$", up):
        errs.append("migration-lint: the up migration must start with BEGIN; and end with COMMIT;")
    creates = re.findall(r"(?i)\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\b(?!\s+IF\s+NOT\s+EXISTS)", up)
    adds = re.findall(r"(?i)\bADD\s+COLUMN\b(?!\s+IF\s+NOT\s+EXISTS)", up)
    if creates or adds:
        errs.append("migration-lint: CREATE TABLE / CREATE INDEX / ADD COLUMN must use IF NOT EXISTS")
    down = sql.split("-- down:", 1)[1] if "-- down:" in sql else ""
    if not re.search(r"(?i)\b(DROP|ALTER)\b", down):
        errs.append("migration-lint: a '-- down:' section with the rollback statements is required at the end")
    return errs


MIGRATIONS = [
    "Write a PostgreSQL migration that creates a table `refunds` (id bigserial primary key, order_id bigint not null, "
    "amount_cents integer not null, created_at timestamptz default now()).",
    "Write a PostgreSQL migration that adds a nullable `phone` text column to the `customers` table.",
    "Write a PostgreSQL migration that creates an index on `orders(customer_id, created_at)`.",
    "Write a PostgreSQL migration that creates a table `feature_flags` (key text primary key, enabled boolean not "
    "null default false).",
    "Write a PostgreSQL migration that adds a `deleted_at timestamptz` column to the `products` table.",
    "Write a PostgreSQL migration that creates a unique index on `users(lower(email))`.",
    "Write a PostgreSQL migration that creates a table `audit_events` (id bigserial primary key, actor text, action "
    "text, payload jsonb, at timestamptz default now()).",
    "Write a PostgreSQL migration that adds an `archived boolean not null default false` column to `projects`.",
    "Write a PostgreSQL migration that creates a table `api_tokens` (id uuid primary key, user_id bigint not null, "
    "hash text not null, expires_at timestamptz).",
    "Write a PostgreSQL migration that creates an index on `payments(status)`.",
]

# ---------------------------------------------------------------- 3. support replies (support-qa)

_PROMISE = re.compile(r"(?i)\b(will|we'll|we will|going to|have|has been)\b[^.]{0,30}\brefund")


def _support_check(first: str, ticket: str) -> Callable[[str], list[str]]:
    def check(out: str) -> list[str]:
        lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        errs = []
        if not lines or lines[0] != f"Hi {first},":
            errs.append(f"support-qa: the reply must open with exactly 'Hi {first},'")
        if f"#{ticket}" not in out:
            errs.append(f"support-qa: reference the ticket as '#{ticket}'")
        if not lines or lines[-1] != "— Acme Support":
            errs.append("support-qa: the last line must be the signature '— Acme Support'")
        if _PROMISE.search(out):
            errs.append("support-qa: agents must never promise or confirm a refund; say the billing team will review")
        return errs
    return check


SUPPORT = [
    ("Maria Lopez", "48213", "My order arrived damaged and I want my money back."),
    ("Tom Becker", "48251", "I was charged twice for my subscription this month."),
    ("Aisha Khan", "48302", "The app logs me out every few minutes on Android."),
    ("Lukas Weber", "48344", "I cancelled last week but was still billed today. Please refund me."),
    ("Chen Wei", "48390", "How do I export all my invoices as PDF?"),
    ("Sofia Rossi", "48417", "The discount code SPRING20 isn't working at checkout."),
    ("James Okafor", "48466", "My package says delivered but I never received it, I want a refund."),
    ("Emma Larsen", "48501", "Can I change the email address on my account?"),
    ("Diego Alvarez", "48549", "The premium plan features are not showing after I upgraded."),
    ("Hana Sato", "48588", "I was overcharged on my last invoice, please return the difference."),
]

# ---------------------------------------------------------------- 4. Python functions (code-review bot)

def _python_check(name: str) -> Callable[[str], list[str]]:
    def check(out: str) -> list[str]:
        code = re.sub(r"```\w*", "", out).strip()
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"review-bot: code does not parse ({e.msg})"]
        fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name), None)
        if fn is None:
            return [f"review-bot: function {name}() not found"]
        errs = []
        args = fn.args.args + fn.args.kwonlyargs
        if any(a.annotation is None for a in args) or fn.returns is None:
            errs.append("review-bot: every parameter and the return value must have a type annotation")
        doc = ast.get_docstring(fn) or ""
        if not all(s in doc for s in ("Args:", "Returns:", "Raises:")):
            errs.append("review-bot: the docstring must be Google style with Args:, Returns: and Raises: sections")
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
        if not any("ValueError" in ast.unparse(r) for r in raises):
            errs.append("review-bot: invalid input must raise ValueError (house rule: no silent defaults)")
        return errs
    return check


PYTHON = [
    ("parse_duration", "parses strings like '1h30m' or '45s' into a number of seconds"),
    ("slugify", "turns a title into a URL slug (lower case, words joined by hyphens)"),
    ("parse_money", "parses strings like '$1,299.50' into integer cents"),
    ("chunk", "splits a list into consecutive chunks of a given size"),
    ("mask_email", "masks an email like 'jane.doe@x.com' as 'j***@x.com'"),
    ("percent_change", "returns the percentage change from an old value to a new value"),
    ("parse_semver", "parses a version string like '1.4.2' into a tuple of three integers"),
    ("normalize_phone", "normalizes a US phone number to the format '+1XXXXXXXXXX'"),
    ("weekday_name", "returns the English weekday name for an ISO date string"),
    ("clamp", "clamps a number between a minimum and a maximum"),
]


def all_tasks() -> dict[str, list[Task]]:
    return {
        "commit": [Task("commit", f"Write the git commit message for this change: {desc} Ticket: {t}.",
                        _commit_check(t)) for desc, t in COMMITS],
        "migration": [Task("migration", p, _migration_check) for p in MIGRATIONS],
        "support": [Task("support", f"Reply to this customer email (ticket {tk}) from {name}:\n\n\"{msg}\"",
                         _support_check(name.split()[0], tk)) for name, tk, msg in SUPPORT],
        "python": [Task("python", f"Write a Python function `{n}` that {d}.", _python_check(n)) for n, d in PYTHON],
    }


def split(train_per_family: int = 6) -> tuple[list[Task], list[Task]]:
    train, test = [], []
    for tasks in all_tasks().values():
        train += tasks[:train_per_family]
        test += tasks[train_per_family:]
    return train, test
