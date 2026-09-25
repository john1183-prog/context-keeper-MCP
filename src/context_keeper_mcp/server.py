"""
Context & Design Keeper — MCP server

Stateful project memory for AI coding clients (Cursor, Claude Desktop, Windsurf).
Tracks focus, hierarchical task progress, critical decisions, and a
per-project/per-section design system in .ai_context/, so an AI coding
assistant stays consistent and picks up where it left off across sessions.

Run (stdio, for IDE config):
  context-keeper-mcp

Test with Inspector:
  npx -y @modelcontextprotocol/inspector context-keeper-mcp
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Context & Design Keeper")

# Prefer the process cwd (usually the project root when launched from an IDE).
# Override with CONTEXT_KEEPER_DIR if you need a fixed path.
CONTEXT_DIR = Path(os.environ.get("CONTEXT_KEEPER_DIR", ".ai_context"))
MEM_FILE = CONTEXT_DIR / "session_memory.json"
BLUEPRINT_FILE = CONTEXT_DIR / "design_blueprint.json"
DECISIONS_FILE = CONTEXT_DIR / "decisions.json"
PROGRESS_FILE = CONTEXT_DIR / "progress.json"
LOCK_FILE = CONTEXT_DIR / ".lock"
LOCK_TIMEOUT_SECONDS = 5

# Central, cross-project index — separate from any single project's .ai_context/
CENTRAL_DIR = Path(os.environ.get("CONTEXT_KEEPER_HOME", str(Path.home() / ".context-keeper")))
CENTRAL_INDEX_FILE = CENTRAL_DIR / "projects.json"
CENTRAL_LOCK_FILE = CENTRAL_DIR / ".lock"

IGNORE_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    ".ai_context", ".idea", ".vscode", "target", ".gradle", ".next", ".cache",
}

MAX_SHORT = 500       # focus, component/section names, task titles, goals
MAX_MEDIUM = 2000     # notes, rationale, decisions
MAX_LONG = 4000        # architectural rules, design-system fields
HISTORY_CAP = 200
DECISIONS_CAP = 500
TASKS_CAP = 2000
VALID_TASK_STATUSES = {"todo", "in_progress", "blocked", "done"}
VALID_TASK_PRIORITIES = {"high", "medium", "low"}
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _register_project() -> None:
    """
    Best-effort: record this project in the central cross-project index
    (~/.context-keeper/projects.json by default). Never lets index
    bookkeeping break the actual tool call it's attached to.
    """
    try:
        CENTRAL_DIR.mkdir(parents=True, exist_ok=True)
        with FileLock(str(CENTRAL_LOCK_FILE), timeout=2):
            if CENTRAL_INDEX_FILE.exists():
                idx = json.loads(CENTRAL_INDEX_FILE.read_text(encoding="utf-8"))
            else:
                idx = {"projects": {}}
            key = str(Path.cwd().resolve())
            idx.setdefault("projects", {})[key] = {
                "path": key,
                "name": Path.cwd().resolve().name,
                "last_used": _now(),
            }
            tmp = CENTRAL_INDEX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx, indent=2), encoding="utf-8")
            tmp.replace(CENTRAL_INDEX_FILE)
    except Exception:
        pass


def ensure_storage() -> None:
    """Create local storage files if they don't exist, and register the project."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

    if not MEM_FILE.exists():
        MEM_FILE.write_text(
            json.dumps({"history": [], "current_focus": "None"}, indent=2),
            encoding="utf-8",
        )

    if not BLUEPRINT_FILE.exists():
        BLUEPRINT_FILE.write_text(
            json.dumps(
                {
                    "architecture": "Undefined",
                    "components": {},
                    "design_system": {
                        "project_type": "Undefined",
                        "visual_direction": "",
                        "typography": "",
                        "color_palette": "",
                        "ux_principles": "",
                        "anti_patterns": "",
                        "sections": {},
                        "last_updated": None,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    if not DECISIONS_FILE.exists():
        DECISIONS_FILE.write_text(
            json.dumps({"decisions": [], "next_id": 1}, indent=2),
            encoding="utf-8",
        )

    if not PROGRESS_FILE.exists():
        PROGRESS_FILE.write_text(
            json.dumps({"tasks": [], "next_id": 1}, indent=2),
            encoding="utf-8",
        )

    _register_project()


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)  # atomic on POSIX; avoids a torn/truncated file on crash


def _locked():
    """
    Single file lock guarding read-modify-write cycles across all four
    project-local stores, against concurrent MCP clients (e.g. two IDE
    windows on the same project).
    """
    ensure_storage()
    return FileLock(str(LOCK_FILE), timeout=LOCK_TIMEOUT_SECONDS)


def _require(value: str, field: str, max_len: int, allow_empty: bool = False) -> str:
    value = (value or "").strip()
    if not value and not allow_empty:
        raise ValueError(f"{field} cannot be empty.")
    if len(value) > max_len:
        raise ValueError(f"{field} is {len(value)} chars; max is {max_len}.")
    return value


def _locked_error() -> str:
    return "Error: another session is writing to this project's context right now. Try again."


def _tid_num(task_id: str) -> int:
    try:
        return int(task_id[1:])
    except (ValueError, IndexError):
        return 0


def _compute_next_task(tasks: list[dict]) -> dict | None:
    """
    Smallest actionable unit: a task with no children (a leaf), not done
    or blocked. Ranked by: already in_progress (resume before starting
    something new) > priority (high > medium > low) > creation order.
    A task with no priority set is treated as medium.
    """
    parent_ids = {t.get("parent_id") for t in tasks if t.get("parent_id")}
    leaves = [t for t in tasks if t.get("id") not in parent_ids]
    candidates = [t for t in leaves if t.get("status") in ("in_progress", "todo")]
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: (
            0 if t.get("status") == "in_progress" else 1,
            PRIORITY_RANK.get(t.get("priority", "medium"), 1),
            _tid_num(t.get("id", "T0")),
        )
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# RESOURCE — background data the model can read on demand
# ---------------------------------------------------------------------------
@mcp.resource("context://project_memory")
def get_project_memory() -> str:
    """
    Full current project state: focus, design system, architecture rules,
    recent critical decisions, task progress (with the next actionable
    task), and recent session actions. Read this at the start of a
    session, or before a non-trivial change.
    """
    ensure_storage()
    mem = _read_json(MEM_FILE)
    design = _read_json(BLUEPRINT_FILE)
    decisions = _read_json(DECISIONS_FILE)
    progress = _read_json(PROGRESS_FILE)

    lines = [
        "=== CURRENT CONTEXT FOCUS ===",
        f"Focus: {mem.get('current_focus', 'None')}",
        "",
        "=== DESIGN SYSTEM ===",
        json.dumps(design.get("design_system", {}), indent=2),
        "",
        "=== ARCHITECTURE ===",
        f"Summary: {design.get('architecture', 'Undefined')}",
        "Components:",
        json.dumps(design.get("components", {}), indent=2),
        "",
        "=== RECENT CRITICAL DECISIONS ===",
    ]
    recent_decisions = (decisions.get("decisions") or [])[-8:]
    if recent_decisions:
        for d in recent_decisions:
            lines.append(
                f"- [{d.get('id')}] {d.get('title')}: {d.get('decision')}"
                + (f" (why: {d.get('rationale')})" if d.get("rationale") else "")
            )
    else:
        lines.append("(none recorded yet)")

    lines += ["", "=== TASK PROGRESS ==="]
    tasks = progress.get("tasks") or []
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.get("status", "todo")] = by_status.get(t.get("status", "todo"), 0) + 1
    if tasks:
        lines.append("Counts: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
        next_task = _compute_next_task(tasks)
        if next_task:
            prio = next_task.get("priority", "medium")
            lines.append(f"Next up: [{next_task.get('id')}] {next_task.get('title')} ({next_task.get('status')}, {prio})")
        else:
            lines.append("Next up: (none actionable — everything done or blocked)")
    else:
        lines.append("(no tasks recorded yet)")

    lines += ["", "=== RECENT SESSION ACTIONS ==="]
    history = mem.get("history") or []
    if history:
        for action in history[-8:]:
            lines.append(f"- [{action.get('timestamp', '?')}] {action.get('note', '')}")
    else:
        lines.append("(no actions recorded yet)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TOOLS — focus & session notes
# ---------------------------------------------------------------------------
@mcp.tool()
def update_current_focus(focus_description: str) -> str:
    """
    Update what part of the system is being designed or fixed right now.
    Call this when switching files, tasks, features, or areas of the codebase.
    """
    focus_description = _require(focus_description, "focus_description", MAX_SHORT)
    try:
        with _locked():
            data = _read_json(MEM_FILE)
            data["current_focus"] = focus_description
            data.setdefault("history", []).append(
                {"timestamp": _now(), "note": f"Shifted focus to: {focus_description}"}
            )
            data["history"] = data["history"][-HISTORY_CAP:]
            _write_json(MEM_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Current focus updated to '{focus_description}'."


@mcp.tool()
def append_session_note(note: str) -> str:
    """
    Append a free-form note to session history without changing current focus.
    Use for small observations. For anything that should bind future
    sessions, use record_decision instead.
    """
    note = _require(note, "note", MAX_MEDIUM)
    try:
        with _locked():
            data = _read_json(MEM_FILE)
            data.setdefault("history", []).append({"timestamp": _now(), "note": note})
            data["history"] = data["history"][-HISTORY_CAP:]
            _write_json(MEM_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Note recorded — {note[:80]}{'…' if len(note) > 80 else ''}."


@mcp.tool()
def search_session_history(query: str, limit: int = 10) -> str:
    """Search past session notes and focus changes for a keyword."""
    query = _require(query, "query", 200)
    limit = max(1, min(limit, 50))
    ensure_storage()
    mem = _read_json(MEM_FILE)
    history = mem.get("history") or []
    q = query.lower()
    hits = [h for h in history if q in h.get("note", "").lower()][-limit:]
    if not hits:
        return f"No history entries matched '{query}'."
    lines = [f"- [{h.get('timestamp', '?')}] {h.get('note', '')}" for h in hits]
    return f"{len(hits)} match(es) for '{query}':\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# TOOLS — architecture
# ---------------------------------------------------------------------------
@mcp.tool()
def update_design_blueprint(component_name: str, architectural_rules: str) -> str:
    """
    Add or update architectural (code-level) design rules for a named
    component. For VISUAL/UX design, use set_design_system or
    set_section_design_notes instead.
    """
    component_name = _require(component_name, "component_name", MAX_SHORT)
    architectural_rules = _require(architectural_rules, "architectural_rules", MAX_LONG)
    try:
        with _locked():
            data = _read_json(BLUEPRINT_FILE)
            data.setdefault("components", {})[component_name] = {
                "rules": architectural_rules,
                "last_updated": _now(),
            }
            _write_json(BLUEPRINT_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Architecture component '{component_name}' synced to design schema."


@mcp.tool()
def set_architecture_summary(summary: str) -> str:
    """Set the high-level architecture summary string for the whole project."""
    summary = _require(summary, "summary", MAX_LONG)
    try:
        with _locked():
            data = _read_json(BLUEPRINT_FILE)
            data["architecture"] = summary
            data["architecture_updated"] = _now()
            _write_json(BLUEPRINT_FILE, data)
    except Timeout:
        return _locked_error()
    return "Success: Project architecture summary updated."


@mcp.tool()
def get_component_rules(component_name: str) -> str:
    """Look up the locked-in architectural rules for one named component."""
    component_name = _require(component_name, "component_name", MAX_SHORT)
    ensure_storage()
    design = _read_json(BLUEPRINT_FILE)
    component = design.get("components", {}).get(component_name)
    if not component:
        known = ", ".join(sorted(design.get("components", {}).keys())) or "(none defined yet)"
        return f"No rules recorded for '{component_name}'. Known components: {known}"
    return (
        f"Component: {component_name}\n"
        f"Last updated: {component.get('last_updated', '?')}\n"
        f"Rules:\n{component.get('rules', '')}"
    )


@mcp.tool()
def infer_project_type_hints() -> str:
    """
    Read-only scan of the project root for signals about what kind of
    project this is: manifest files (package.json, pyproject.toml, etc.),
    top-level folders, dominant file types, and the README's opening.
    This does NOT decide anything — it only gathers evidence. Read the
    output, then call set_design_system yourself with an actual
    project_type and anti_patterns based on what you see here and what
    you already know from the conversation.
    """
    root = Path.cwd()
    lines = ["=== PROJECT SIGNALS (evidence only — you interpret it) ==="]

    manifest_names = [
        "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
        "composer.json", "Gemfile", "requirements.txt", "pom.xml",
        "build.gradle", "build.gradle.kts",
    ]
    found = []
    for name in manifest_names:
        p = root / name
        if p.exists():
            try:
                snippet = p.read_text(encoding="utf-8", errors="ignore").strip()[:200]
            except OSError:
                snippet = "(unreadable)"
            found.append(f"{name}: {snippet}")
    lines.append("Manifest files found:")
    lines.extend(f"  - {m}" for m in found) if found else lines.append("  (none of the common ones)")

    readme_snippet = ""
    for name in ["README.md", "README.rst", "README.txt", "README"]:
        p = root / name
        if p.exists():
            try:
                readme_snippet = p.read_text(encoding="utf-8", errors="ignore").strip()[:400]
            except OSError:
                pass
            break
    if readme_snippet:
        lines.append(f"README opening: {readme_snippet}")

    try:
        top_dirs = sorted(
            d.name for d in root.iterdir() if d.is_dir() and d.name not in IGNORE_DIRS and not d.name.startswith(".")
        )[:30]
    except OSError:
        top_dirs = []
    lines.append(f"Top-level directories: {', '.join(top_dirs) or '(none)'}")

    ext_counts: dict[str, int] = {}
    scanned = 0
    try:
        for p in root.rglob("*"):
            if scanned > 3000:
                break
            if p.is_file() and not any(part in IGNORE_DIRS for part in p.parts):
                ext_counts[p.suffix or "(no ext)"] = ext_counts.get(p.suffix or "(no ext)", 0) + 1
                scanned += 1
    except OSError:
        pass
    top_ext = sorted(ext_counts.items(), key=lambda kv: -kv[1])[:8]
    lines.append(
        "Dominant file types: " + (", ".join(f"{ext}={n}" for ext, n in top_ext) or "(none found)")
    )

    lines.append("")
    lines.append("This is evidence, not a conclusion — infer the real project type and audience yourself.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TOOLS — design system (visual/UX, project-wide and per-section)
# ---------------------------------------------------------------------------
@mcp.tool()
def set_design_system(
    project_type: str,
    visual_direction: str,
    typography: str = "",
    color_palette: str = "",
    ux_principles: str = "",
    anti_patterns: str = "",
) -> str:
    """
    Lock in the project's visual/UX direction, ONCE, so every future
    session builds consistent UI instead of re-deciding or defaulting to
    generic AI-app conventions.

    Before calling this: actually look at what the project is for and who
    uses it (use infer_project_type_hints for evidence if unsure) and let
    THAT drive every field below. Do not fill these in with a generic
    template.

    - project_type: what kind of product this is and who it's for, in
      your own words.
    - visual_direction: the concrete look/mood — not "clean and modern"
      (that describes nothing), but e.g. "dense data-grid heavy, muted
      grays, sharp corners, minimal color except for status indicators."
    - anti_patterns: name the generic patterns to actively avoid for THIS
      project (e.g. "no default shadcn purple-to-blue gradient hero, no
      centered hero-with-stock-illustration layout"). Don't skip this —
      it's what stops the output looking like every other AI-generated app.
    """
    project_type = _require(project_type, "project_type", MAX_LONG)
    visual_direction = _require(visual_direction, "visual_direction", MAX_LONG)
    typography = _require(typography, "typography", MAX_LONG, allow_empty=True)
    color_palette = _require(color_palette, "color_palette", MAX_LONG, allow_empty=True)
    ux_principles = _require(ux_principles, "ux_principles", MAX_LONG, allow_empty=True)
    anti_patterns = _require(anti_patterns, "anti_patterns", MAX_LONG, allow_empty=True)
    try:
        with _locked():
            data = _read_json(BLUEPRINT_FILE)
            ds = data.setdefault("design_system", {})
            ds.update(
                {
                    "project_type": project_type,
                    "visual_direction": visual_direction,
                    "typography": typography,
                    "color_palette": color_palette,
                    "ux_principles": ux_principles,
                    "anti_patterns": anti_patterns,
                    "last_updated": _now(),
                }
            )
            ds.setdefault("sections", {})
            _write_json(BLUEPRINT_FILE, data)
    except Timeout:
        return _locked_error()
    return "Success: Project design system recorded."


@mcp.tool()
def set_section_design_notes(section_name: str, notes: str) -> str:
    """
    Record design notes for one section/area that should deliberately
    differ from the rest (e.g. a bold "marketing_site" vs. a dense,
    neutral "admin_dashboard" in the same project). Call set_design_system
    first for the baseline.
    """
    section_name = _require(section_name, "section_name", MAX_SHORT)
    notes = _require(notes, "notes", MAX_LONG)
    try:
        with _locked():
            data = _read_json(BLUEPRINT_FILE)
            ds = data.setdefault("design_system", {})
            ds.setdefault("sections", {})[section_name] = {"notes": notes, "last_updated": _now()}
            _write_json(BLUEPRINT_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Design notes for section '{section_name}' recorded."


@mcp.tool()
def get_design_system(section_name: str = "") -> str:
    """
    Retrieve the project-wide design system, or one section's notes
    layered on top of it. Call this before generating or modifying any UI.
    """
    ensure_storage()
    design = _read_json(BLUEPRINT_FILE)
    ds = design.get("design_system", {})
    if not section_name:
        if ds.get("project_type", "Undefined") == "Undefined":
            return (
                "No design system recorded yet. Call infer_project_type_hints, "
                "then set_design_system, before generating UI."
            )
        return json.dumps(ds, indent=2)

    section_name = _require(section_name, "section_name", MAX_SHORT)
    section = ds.get("sections", {}).get(section_name)
    out = {
        "project_wide": ds,
        "section": section or f"(no specific notes for '{section_name}'; project-wide applies)",
    }
    return json.dumps(out, indent=2)


# ---------------------------------------------------------------------------
# TOOLS — critical decisions
# ---------------------------------------------------------------------------
@mcp.tool()
def record_decision(
    title: str, decision: str, rationale: str = "", alternatives_considered: str = ""
) -> str:
    """
    Record a critical decision that should bind future sessions (a real
    choice made — use append_session_note for passing observations).
    Include rationale whenever there was a real tradeoff.
    """
    title = _require(title, "title", MAX_SHORT)
    decision = _require(decision, "decision", MAX_MEDIUM)
    rationale = _require(rationale, "rationale", MAX_MEDIUM, allow_empty=True)
    alternatives_considered = _require(
        alternatives_considered, "alternatives_considered", MAX_MEDIUM, allow_empty=True
    )
    try:
        with _locked():
            data = _read_json(DECISIONS_FILE)
            next_id = data.get("next_id", 1)
            entry = {
                "id": f"D{next_id}",
                "title": title,
                "decision": decision,
                "rationale": rationale,
                "alternatives_considered": alternatives_considered,
                "timestamp": _now(),
            }
            data.setdefault("decisions", []).append(entry)
            data["decisions"] = data["decisions"][-DECISIONS_CAP:]
            data["next_id"] = next_id + 1
            _write_json(DECISIONS_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Decision {entry['id']} recorded — {title}."


@mcp.tool()
def search_decisions(query: str, limit: int = 10) -> str:
    """Search recorded critical decisions by keyword before making a related change."""
    query = _require(query, "query", 200)
    limit = max(1, min(limit, 50))
    ensure_storage()
    data = _read_json(DECISIONS_FILE)
    q = query.lower()
    hits = [
        d
        for d in (data.get("decisions") or [])
        if q in d.get("title", "").lower() or q in d.get("decision", "").lower() or q in d.get("rationale", "").lower()
    ][-limit:]
    if not hits:
        return f"No decisions matched '{query}'."
    lines = [
        f"- [{d.get('id')}] {d.get('title')}: {d.get('decision')}"
        + (f" (why: {d.get('rationale')})" if d.get("rationale") else "")
        for d in hits
    ]
    return f"{len(hits)} match(es) for '{query}':\n" + "\n".join(lines)


@mcp.tool()
def get_recent_decisions(limit: int = 10) -> str:
    """List the most recent critical decisions, newest first."""
    limit = max(1, min(limit, 50))
    ensure_storage()
    data = _read_json(DECISIONS_FILE)
    decisions = (data.get("decisions") or [])[-limit:]
    if not decisions:
        return "No decisions recorded yet."
    lines = [
        f"- [{d.get('id')}] ({d.get('timestamp', '?')}) {d.get('title')}: {d.get('decision')}"
        for d in reversed(decisions)
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TOOLS — task planning & progress (hierarchical)
# ---------------------------------------------------------------------------
@mcp.tool()
def add_task(title: str, status: str = "todo", priority: str = "medium") -> str:
    """
    Add a single, standalone task. priority is one of: high, medium, low —
    it decides ordering in get_next_task, so set it deliberately rather
    than leaving everything at the medium default when tasks actually
    differ in urgency. For a goal you want split into steps, use
    plan_tasks instead.
    """
    title = _require(title, "title", MAX_SHORT)
    status = (status or "todo").strip().lower()
    priority = (priority or "medium").strip().lower()
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_TASK_STATUSES)}")
    if priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(VALID_TASK_PRIORITIES)}")
    try:
        with _locked():
            data = _read_json(PROGRESS_FILE)
            next_id = data.get("next_id", 1)
            task = {
                "id": f"T{next_id}",
                "title": title,
                "status": status,
                "priority": priority,
                "parent_id": None,
                "created": _now(),
                "updated": _now(),
            }
            data.setdefault("tasks", []).append(task)
            data["tasks"] = data["tasks"][-TASKS_CAP:]
            data["next_id"] = next_id + 1
            _write_json(PROGRESS_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Task {task['id']} added — {title} [{status}, {priority}]."


@mcp.tool()
def plan_tasks(goal: str, subtasks: list[str], priorities: list[str] | None = None) -> str:
    """
    Submit a goal and break it into an ordered list of small subtasks up
    front, before starting work. Creates a parent task for the goal plus
    one child task per subtask. Use get_next_task to pick up the highest-
    priority actionable one, and update_task_status as each is finished.
    If a subtask turns out bigger than expected once you're in it, call
    break_down_task on it to split it further.

    priorities (optional): one of "high"/"medium"/"low" per subtask, same
    order and length as subtasks. If omitted, every subtask defaults to
    medium — only skip this if the subtasks genuinely don't differ in
    urgency, since get_next_task uses it to decide what to surface first.
    """
    goal = _require(goal, "goal", MAX_SHORT)
    if not subtasks:
        raise ValueError("subtasks cannot be empty — pass at least one step.")
    cleaned = [_require(s, "subtask", MAX_SHORT) for s in subtasks]
    if priorities:
        if len(priorities) != len(cleaned):
            raise ValueError("priorities must be the same length as subtasks (or omitted).")
        clean_priorities = [(p or "medium").strip().lower() for p in priorities]
        for p in clean_priorities:
            if p not in VALID_TASK_PRIORITIES:
                raise ValueError(f"priority '{p}' must be one of {sorted(VALID_TASK_PRIORITIES)}")
    else:
        clean_priorities = ["medium"] * len(cleaned)
    try:
        with _locked():
            data = _read_json(PROGRESS_FILE)
            next_id = data.get("next_id", 1)
            parent = {
                "id": f"T{next_id}",
                "title": goal,
                "status": "todo",
                "priority": "medium",
                "parent_id": None,
                "created": _now(),
                "updated": _now(),
            }
            next_id += 1
            data.setdefault("tasks", []).append(parent)
            child_ids = []
            for sub, prio in zip(cleaned, clean_priorities):
                child = {
                    "id": f"T{next_id}",
                    "title": sub,
                    "status": "todo",
                    "priority": prio,
                    "parent_id": parent["id"],
                    "created": _now(),
                    "updated": _now(),
                }
                data["tasks"].append(child)
                child_ids.append(child["id"])
                next_id += 1
            data["tasks"] = data["tasks"][-TASKS_CAP:]
            data["next_id"] = next_id
            _write_json(PROGRESS_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: Goal {parent['id']} '{goal}' planned with {len(child_ids)} subtasks: {', '.join(child_ids)}."


@mcp.tool()
def break_down_task(task_id: str, subtasks: list[str], priorities: list[str] | None = None) -> str:
    """
    Split an existing task (even one already in progress) into smaller
    child subtasks, when it turns out too large to do in one step.
    priorities (optional): one of "high"/"medium"/"low" per subtask, same
    order/length as subtasks; defaults to medium if omitted.
    """
    task_id = _require(task_id, "task_id", 50)
    if not subtasks:
        raise ValueError("subtasks cannot be empty — pass at least one step.")
    cleaned = [_require(s, "subtask", MAX_SHORT) for s in subtasks]
    if priorities:
        if len(priorities) != len(cleaned):
            raise ValueError("priorities must be the same length as subtasks (or omitted).")
        clean_priorities = [(p or "medium").strip().lower() for p in priorities]
        for p in clean_priorities:
            if p not in VALID_TASK_PRIORITIES:
                raise ValueError(f"priority '{p}' must be one of {sorted(VALID_TASK_PRIORITIES)}")
    else:
        clean_priorities = ["medium"] * len(cleaned)
    try:
        with _locked():
            data = _read_json(PROGRESS_FILE)
            tasks = data.get("tasks") or []
            if not any(t.get("id") == task_id for t in tasks):
                return f"Error: no task with id '{task_id}' found."
            next_id = data.get("next_id", 1)
            child_ids = []
            for sub, prio in zip(cleaned, clean_priorities):
                child = {
                    "id": f"T{next_id}",
                    "title": sub,
                    "status": "todo",
                    "priority": prio,
                    "parent_id": task_id,
                    "created": _now(),
                    "updated": _now(),
                }
                tasks.append(child)
                child_ids.append(child["id"])
                next_id += 1
            data["tasks"] = tasks[-TASKS_CAP:]
            data["next_id"] = next_id
            _write_json(PROGRESS_FILE, data)
    except Timeout:
        return _locked_error()
    return f"Success: {task_id} broken down into {len(child_ids)} subtasks: {', '.join(child_ids)}."


@mcp.tool()
def update_task_status(task_id: str, status: str) -> str:
    """Update a task's status: todo, in_progress, blocked, or done."""
    task_id = _require(task_id, "task_id", 50)
    status = (status or "").strip().lower()
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_TASK_STATUSES)}")
    try:
        with _locked():
            data = _read_json(PROGRESS_FILE)
            tasks = data.get("tasks") or []
            for t in tasks:
                if t.get("id") == task_id:
                    t["status"] = status
                    t["updated"] = _now()
                    _write_json(PROGRESS_FILE, data)
                    return f"Success: {task_id} set to '{status}'."
    except Timeout:
        return _locked_error()
    return f"Error: no task with id '{task_id}' found."


@mcp.tool()
def update_task_priority(task_id: str, priority: str) -> str:
    """
    Change a task's priority (high, medium, low) — e.g. once you learn a
    task is more urgent than it looked when created. Affects what
    get_next_task surfaces.
    """
    task_id = _require(task_id, "task_id", 50)
    priority = (priority or "").strip().lower()
    if priority not in VALID_TASK_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(VALID_TASK_PRIORITIES)}")
    try:
        with _locked():
            data = _read_json(PROGRESS_FILE)
            tasks = data.get("tasks") or []
            for t in tasks:
                if t.get("id") == task_id:
                    t["priority"] = priority
                    t["updated"] = _now()
                    _write_json(PROGRESS_FILE, data)
                    return f"Success: {task_id} priority set to '{priority}'."
    except Timeout:
        return _locked_error()
    return f"Error: no task with id '{task_id}' found."


@mcp.tool()
def get_next_task() -> str:
    """
    Get the single smallest actionable task to work on right now: the
    first leaf task (no children of its own) that isn't done or blocked,
    preferring one already in_progress over starting a new one.
    """
    ensure_storage()
    data = _read_json(PROGRESS_FILE)
    tasks = data.get("tasks") or []
    next_task = _compute_next_task(tasks)
    if not next_task:
        return "No actionable tasks — everything is done or blocked." if tasks else "No tasks recorded yet."
    parent_id = next_task.get("parent_id")
    parent_note = ""
    if parent_id:
        parent = next((t for t in tasks if t.get("id") == parent_id), None)
        if parent:
            parent_note = f" (part of {parent_id}: {parent.get('title')})"
    prio = next_task.get("priority", "medium")
    return f"Next up: [{next_task['id']}] {next_task['title']}{parent_note} — status: {next_task['status']}, priority: {prio}"


@mcp.tool()
def list_tasks(status_filter: str = "") -> str:
    """List tasks, optionally filtered by status. Leave empty to list all, with hierarchy shown."""
    ensure_storage()
    data = _read_json(PROGRESS_FILE)
    tasks = data.get("tasks") or []
    id_to_title = {t.get("id"): t.get("title") for t in tasks}
    status_filter = (status_filter or "").strip().lower()
    if status_filter:
        if status_filter not in VALID_TASK_STATUSES:
            raise ValueError(f"status_filter must be one of {sorted(VALID_TASK_STATUSES)}")
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if not tasks:
        return "No tasks found." if not status_filter else f"No tasks with status '{status_filter}'."
    lines = []
    for t in tasks:
        parent_id = t.get("parent_id")
        suffix = f" (part of {parent_id}: {id_to_title.get(parent_id, '?')})" if parent_id else ""
        prio = t.get("priority", "medium")
        lines.append(f"- [{t.get('id')}] ({t.get('status')}, {prio}) {t.get('title')}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TOOLS — cross-project index
# ---------------------------------------------------------------------------
@mcp.tool()
def list_projects() -> str:
    """
    List every project this context-keeper has been used on, most
    recently used first, from a central index outside any single
    project's .ai_context/.
    """
    try:
        if not CENTRAL_INDEX_FILE.exists():
            return "No projects recorded yet."
        idx = json.loads(CENTRAL_INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "Could not read the central project index."
    projects = list((idx.get("projects") or {}).values())
    if not projects:
        return "No projects recorded yet."
    projects.sort(key=lambda p: p.get("last_used", ""), reverse=True)
    lines = [f"- {p.get('name')} — {p.get('path')} (last used {p.get('last_used', '?')})" for p in projects]
    return "\n".join(lines)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
