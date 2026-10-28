"""Deterministic aggregation of independent reviews: anchor validation, dedupe, cross-confirmation,
dispute handling and the blocking policy. No LLM involved - this is the orchestrator's judgement."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

SEV_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
_WORD = re.compile(r"[a-z0-9_]{3,}")


@dataclass
class Finding:
    id: str
    severity: str
    category: str
    file: str
    line: int
    title: str
    trigger: str = ""
    impact: str = ""
    rule: str = ""
    fix: str = ""
    confidence: str = "medium"
    reviewers: list[str] = field(default_factory=list)
    previous_id: str | None = None
    status: str = "open"          # open | fixed | rejected | disputed | advisory | accepted_rejection | accepted_gate_change
    round: int = 1
    anchor_ok: bool = True
    dispute_count: int = 0
    resolution: str = ""          # fixer's note
    resolution_status: str = ""   # fixed | rejected | partial
    also: list[dict] = field(default_factory=list)  # the same defect as other reviewers described it

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def mechanical(self) -> bool:
        """Produced by the orchestrator from gate output (required tests), not by a reviewer."""
        return self.reviewers == ["orchestrator"]

    @property
    def confirmed(self) -> bool:
        return len(set(self.reviewers)) >= 2

    def anchor(self) -> str:
        return f"{self.file}:{self.line}" if self.file else "(no anchor)"

    def for_prompt(self) -> str:
        who = ", ".join(sorted(set(self.reviewers)))
        conf = "confirmed by 2 reviewers" if self.confirmed else f"confidence {self.confidence}"
        lines = [f"- [{self.id}] [{self.severity}] {self.anchor()} ({self.category}; {conf}; from {who})",
                 f"  {self.title}"]
        for label, val in (("Trigger", self.trigger), ("Impact", self.impact), ("Rule", self.rule),
                           ("Suggested fix", self.fix)):
            if val:
                lines.append(f"  {label}: {val}")
        for other in self.also[:3]:
            # A duplicate often carries a different failing scenario; the fixer needs all of them.
            where = f" at {other['file']}:{other['line']}" if other.get("file") and (
                other.get("file"), other.get("line")) != (self.file, self.line) else ""
            extra = f" - trigger: {other['trigger']}" if other.get("trigger") else ""
            lines.append(f"  Also reported by {other.get('reviewer', '?')}{where}: {other.get('title', '')}{extra}")
        if self.resolution:
            lines.append(f"  Fixer said ({self.resolution_status}): {self.resolution}")
        return "\n".join(lines)


@dataclass
class Aggregate:
    findings: list[Finding]
    dropped: list[dict]
    verdicts: dict[str, str]
    escalate: bool
    acceptance: list[dict]
    residual_risks: list[str]

    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "open"]

    def advisory(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "advisory"]

    def disputed(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "disputed"]


def normalize_path(raw: str, root: Path) -> str:
    p = (raw or "").strip().strip("`'\"").replace("\\", "/")
    p = re.sub(r":\d+(?::\d+)?$", "", p)
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("a/") or p.startswith("b/"):
        cand = p[2:]
        if (root / cand).exists():
            p = cand
    pp = Path(p)
    if pp.is_absolute():
        try:
            p = pp.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return p


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def similar(a: Finding, b: Finding) -> bool:
    if a.previous_id and a.previous_id == b.previous_id:
        return True
    if a.category == "acceptance" and b.category == "acceptance":
        wa, wb = _words(a.title), _words(b.title)
        return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5
    if a.file != b.file or not a.file:
        return False
    if abs(a.line - b.line) > 3:
        return False
    if a.category == b.category:
        return True
    wa, wb = _words(a.title + " " + a.trigger), _words(b.title + " " + b.trigger)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.3


def _evidence(f: Finding) -> dict:
    return {"reviewer": ",".join(f.reviewers), "file": f.file, "line": f.line, "title": f.title,
            "trigger": f.trigger, "impact": f.impact}


def merge_into(base: Finding, other: Finding) -> None:
    # Keep the losing description too: two reviewers often reach one defect through different inputs.
    loser = _evidence(base) if SEV_RANK[other.severity] < SEV_RANK[base.severity] else _evidence(other)
    if loser["title"] or loser["trigger"]:
        base.also.append(loser)
    if SEV_RANK[other.severity] < SEV_RANK[base.severity]:
        base.severity = other.severity
        base.title, base.trigger, base.impact = other.title, other.trigger, other.impact
        base.rule, base.fix = other.rule or base.rule, other.fix or base.fix
        if other.anchor_ok and other.file:  # the anchor belongs to the description it came with
            base.file, base.line = other.file, other.line
    if CONF_RANK[other.confidence] > CONF_RANK[base.confidence]:
        base.confidence = other.confidence
    for r in other.reviewers:
        if r not in base.reviewers:
            base.reviewers.append(r)
    base.previous_id = base.previous_id or other.previous_id
    base.anchor_ok = base.anchor_ok or other.anchor_ok


class IdAllocator:
    def __init__(self, start: int = 0):
        self.n = start

    def next(self) -> str:
        self.n += 1
        return f"F{self.n}"


def aggregate(
    reviews: dict[str, dict],
    *,
    root: Path,
    round_no: int,
    previous: list[Finding],
    ids: IdAllocator,
    block_on: list[str],
    min_confidence_single: str = "medium",
    escalation_signals: int = 0,
) -> Aggregate:
    prev_by_id = {f.id: f for f in previous}
    raw: list[Finding] = []
    dropped: list[dict] = []
    verdicts: dict[str, str] = {}
    acceptance: list[dict] = []
    residual: list[str] = []
    dispute_votes: dict[str, list[str]] = {}

    for reviewer, data in reviews.items():
        verdicts[reviewer] = data.get("verdict", "REQUEST_CHANGES")
        residual += [f"{reviewer}: {r}" for r in data.get("residual_risks", [])]
        for d in data.get("disputes", []):
            dispute_votes.setdefault(d.get("id", ""), []).append(d.get("decision", "accept_rejection"))
        for item in data.get("findings", []):
            f = Finding(id="", severity=item["severity"], category=item["category"],
                        file=normalize_path(item.get("file", ""), root), line=int(item.get("line") or 0),
                        title=item.get("title", "").strip(), trigger=item.get("trigger", ""),
                        impact=item.get("impact", ""), rule=item.get("rule", ""), fix=item.get("fix", ""),
                        confidence=item.get("confidence", "medium"), reviewers=[reviewer],
                        previous_id=item.get("previous_id") or None, round=round_no)
            if f.previous_id and f.previous_id not in prev_by_id:
                f.previous_id = None
            if f.category != "acceptance":
                target = root / f.file if f.file else None
                if not f.file or target is None or not target.is_file():
                    dropped.append({"reviewer": reviewer, "reason": "file does not exist", **item})
                    continue
                try:
                    n_lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    n_lines = 0
                if f.line < 1 or f.line > max(n_lines, 1):
                    f.anchor_ok = False
                    f.line = min(max(f.line, 1), max(n_lines, 1))
                    f.confidence = {"high": "medium", "medium": "low", "low": "low"}[f.confidence]
            raw.append(f)
        for crit in data.get("acceptance", []):
            acceptance.append({"reviewer": reviewer, **crit})
            if crit.get("status") == "not_met":
                raw.append(Finding(id="", severity="P1", category="acceptance", file="", line=0,
                                   title=f"Acceptance criterion not met: {crit.get('criterion', '')}",
                                   trigger=crit.get("note", ""), confidence="medium",
                                   reviewers=[reviewer], round=round_no))

    # Dedupe across reviewers.
    merged: list[Finding] = []
    for f in raw:
        for m in merged:
            # Same reviewer listing two nearby issues = two findings; different reviewers = merge.
            if similar(m, f) and not set(f.reviewers) & set(m.reviewers):
                merge_into(m, f)
                break
        else:
            merged.append(f)

    # Link persisting findings that a reviewer re-reported without `previous_id` (LLMs forget it):
    # otherwise ids change, stall detection, fixer escalation and dispute counting all break.
    linked = {f.previous_id for f in merged if f.previous_id}
    prev_open = [p for p in previous if p.status == "open"]
    for f in merged:
        if f.previous_id:
            continue
        for p in prev_open:
            if p.id not in linked and similar(f, p):
                f.previous_id = p.id
                linked.add(p.id)
                break

    # Stable ids: re-raised findings keep their id.
    used: set[str] = set()
    for f in merged:
        if f.previous_id and f.previous_id not in used:
            f.id = f.previous_id
            prev = prev_by_id[f.previous_id]
            f.dispute_count = prev.dispute_count
            if prev.resolution_status == "rejected":
                f.dispute_count += 1
        else:
            f.id = ids.next()
        used.add(f.id)

    # Advisory/disputed items are not re-reviewed in later rounds (reviewers focus on the delta and
    # the blocking items) - carry them forward so they reach the final report.
    for prev in previous:
        if (prev.status in ("advisory", "disputed", "accepted_rejection", "accepted_gate_change") and prev.id not in used
                and prev.resolution_status != "fixed"):
            merged.append(Finding.from_dict(prev.to_dict()))
            used.add(prev.id)

    # Open findings whose reviewers produced no result this round (failed, or skipped by
    # rereview="first") stay open unless the fixer fixed them - nobody has verified otherwise.
    participants = set(reviews)
    for prev in previous:
        if (prev.status == "open" and prev.id not in used and not set(prev.reviewers) & participants
                and prev.resolution_status not in ("fixed", "rejected")):
            carried = Finding.from_dict(prev.to_dict())
            carried.round = round_no
            merged.append(carried)
            used.add(carried.id)

    # Previously rejected findings that nobody re-raised: decide by dispute votes.
    for prev in previous:
        if prev.resolution_status != "rejected" or prev.id in used:
            continue
        votes = dispute_votes.get(prev.id, [])
        if "reraise" in votes:
            again = Finding.from_dict(prev.to_dict())
            again.round = round_no
            again.dispute_count += 1
            again.status = "open"
            merged.append(again)
            used.add(again.id)
        else:
            closed = Finding.from_dict(prev.to_dict())
            closed.status = "accepted_rejection"
            merged.append(closed)

    # Policy.
    threshold = CONF_RANK[min_confidence_single]
    for f in merged:
        if f.status != "open":
            continue
        if f.dispute_count >= 2:
            f.status = "disputed"            # fixer and reviewers disagree twice -> human decides
        elif f.severity == "P0":
            f.status = "open"
        elif f.severity not in block_on:
            f.status = "advisory"
        elif not f.confirmed and CONF_RANK[f.confidence] < threshold:
            f.status = "advisory"

    merged.sort(key=lambda f: (f.status != "open", SEV_RANK[f.severity], f.file, f.line))
    escalate = escalation_signals > 0 or any(v == "ESCALATE" for v in verdicts.values())
    return Aggregate(merged, dropped, verdicts, escalate, acceptance, residual)


def apply_resolutions(findings: list[Finding], resolutions: list[dict], allow_reject: bool) -> None:
    by_id = {r.get("id"): r for r in resolutions}
    for f in findings:
        r = by_id.get(f.id)
        if not r:
            continue
        status = r.get("status", "")
        if status == "rejected" and not allow_reject:
            status = "partial"
        f.resolution_status = status
        f.resolution = r.get("note", "")
