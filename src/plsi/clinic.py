"""The physician's clinic (skill panel/clinic): each patient on the panel has two Drive folders, an inbox the patient
uploads to and a shared folder the patient reads. Both ids live in the patient's roster row. `clinic sync` routes
downloaded files to the right patient by folder id, never by file contents, and skips files already taken in; Drive
itself is reached by plsi.clinic_drive (service account) or by the agent's own connector (`--file`, `clinic link`,
`clinic sharing`). Every outcome is a line of <PLSI_HOME>/work/panel/synced.jsonl. A patient's email is never stored:
the row keeps `patient_id`, the opaque Drive permission id of the person invited, so a later check knows them.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from plsi import clock, role
from plsi.errors import NotFound, ValidationError
from plsi.files import append_jsonl, atomic_write_json, load_json, read_jsonl, sha256_file
from plsi.paths import is_valid_id, plsi_home, safe_join
from plsi.vocab import EventType, PatientAccess, RosterStatus

FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")  # a Drive folder or file id
SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACCESS = {"inbox": "writer", "shared_folder": "reader"}  # roster key -> the patient's access once shared
NAMES = {"inbox": "inbox", "shared_folder": "shared"}  # roster key -> the folder's name, `PLSI <label> <name>`
HANDLE_MAX = 40  # the physician's own name for a patient, shown only to the physician


def _key(name: Any) -> str:
    """A label or handle as compared for collisions: case, spaces and dots ignored ("J.D." is "jd")."""
    return re.sub(r"[\s.]+", "", name).casefold() if isinstance(name, str) else ""


def shown(r: dict[str, Any]) -> str:
    """How the physician sees a patient: their handle with the label, or the label alone."""
    handle = r.get("handle")
    return f"{handle} ({r['label']})" if handle else str(r.get("label"))


def panel_dir() -> Path:
    return plsi_home() / "work" / "panel"


def rows() -> list[dict[str, Any]]:
    """The roster's rows; every clinic command reads them first, so a steward's account is refused here."""
    role.not_a_steward()
    path = panel_dir() / "roster.json"
    if not path.is_file():
        raise NotFound("no panel roster yet", code="ROSTER_NOT_FOUND", hint="add a patient first (skill panel/roster)")
    data = load_json(path)
    found = data.get("patients") if isinstance(data, dict) else data
    if not isinstance(found, list) or not all(isinstance(r, dict) for r in found):
        raise ValidationError("roster.json must be a list of patient rows")
    return found


def save_rows(updated: list[dict[str, Any]]) -> None:
    path = panel_dir() / "roster.json"
    data = load_json(path)
    atomic_write_json(path, {**data, "patients": updated} if isinstance(data, dict) else updated)


def patient_row(found: list[dict[str, Any]], patient: str, active: bool = True) -> dict[str, Any]:
    found_row = next((r for r in found if r.get("label") == patient), None)
    if found_row is None:
        raise NotFound(f"{patient} is not on the roster", code="NOT_ON_ROSTER", hint="skill panel/roster")
    if active and found_row.get("status") != RosterStatus.ACTIVE:
        raise ValidationError(f"{patient} is {found_row.get('status')}, not active")
    return found_row


def sharing_problems(grants: list[dict[str, Any]], key: str, r: dict[str, Any]) -> list[str]:
    """What is wrong with one patient folder's own grants ([{type, role, id?}]), given its row: never a link, domain
    or group share; nobody before the patient is invited; then exactly that person with the folder's role. The
    physician's own grant (owner, on the connector route) is theirs. Names no address."""
    out = [f"a {g.get('type')} share" for g in grants if g.get("type") in ("anyone", "domain", "group")]
    users = [g for g in grants if g.get("type") == "user" and g.get("role") != "owner"]
    if r.get("patient_access", PatientAccess.NONE) == PatientAccess.NONE:
        return out + [f"{len(users)} person(s) can open it, but the patient is not invited yet"] * bool(users)
    if len(users) != 1:
        return out + [f"{len(users)} people can open it; only the patient may"]
    if users[0].get("role") != ACCESS[key]:
        out.append(f"the patient is {users[0].get('role')}, not {ACCESS[key]}")
    if r.get("patient_id") and users[0].get("id") != r["patient_id"]:
        out.append("it is shared with someone other than the person invited")
    return out


def link(patient: str, inbox: str | None = None, shared: str | None = None, patient_id: str | None = None,
         reviewed: str | None = None) -> dict[str, Any]:
    """`clinic link` (connector or by hand): write a patient's folder ids, the invited person's Drive permission id
    (which marks the folders shared) and the as-of date of the last report the patient received into their row."""
    found = rows()
    r = patient_row(found, patient, active=False)
    for key, value in (("inbox", inbox), ("shared_folder", shared), ("patient_id", patient_id)):
        if value is None:
            continue
        if not FOLDER_RE.match(value):
            raise ValidationError(f"{key} is not a Drive id",
                                  hint="copy it from the folder's URL or the connector's answer")
        if key != "patient_id" and any(o is not r and value in (o.get("inbox"), o.get("shared_folder")) for o in found):
            raise ValidationError("that folder is another patient's; every patient folder is that patient's alone",
                                  code="FOLDER_TAKEN")
        r[key] = value
    if patient_id:
        r["patient_access"] = PatientAccess.SHARED.value
    r.setdefault("patient_access", PatientAccess.NONE.value)
    if reviewed is not None:
        if not DATE_RE.match(reviewed):
            raise ValidationError("reviewed must be YYYY-MM-DD, the report's as-of date")
        r["reviewed"] = reviewed
    save_rows(found)
    return {"label": patient, **{k: r.get(k) for k in ("inbox", "shared_folder", "patient_access", "reviewed")}}


def sharing(patient: str, folder: str, file: str = "-") -> dict[str, Any]:
    """`clinic sharing` (connector): check one folder's permissions as the connector returned them (Drive's
    `permissions` list, on stdin by default and never kept) against the patient's row."""
    key = {"inbox": "inbox", "shared": "shared_folder"}[folder]
    r = patient_row(rows(), patient, active=False)
    data = load_json(file)
    found = data.get("permissions", data) if isinstance(data, dict) else data
    if not isinstance(found, list) or not all(isinstance(g, dict) for g in found):
        raise ValidationError("give Drive's permissions list: [{type, role, id}]")
    if r.get("patient_access") == PatientAccess.SHARED and not r.get("patient_id"):
        raise ValidationError(f"{patient}'s row has no patient_id",
                              hint="plsi clinic link --patient-id <permission id>")
    problems_found = sharing_problems(found, key, r)
    return {"label": patient, "folder": folder, "ok": not problems_found, "problems": problems_found}


def ledger() -> list[dict[str, Any]]:
    return read_jsonl(panel_dir() / "synced.jsonl")


def record(entry: dict[str, Any]) -> dict[str, Any]:
    entry = {**entry, "at": clock.now().isoformat()}
    append_jsonl(panel_dir() / "synced.jsonl", entry)
    return entry


def problems(found: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """(roster-wide problems that stop a sync, {label: why} for patients whose files are held)."""
    fatal, held = [], {}
    uses = Counter(r.get(k) for r in found for k in ("inbox", "shared_folder") if r.get(k))
    names = Counter(n for r in found for n in {_key(r.get("label")), _key(r.get("handle"))} if n)
    for r in found:
        label = r.get("label")
        if not isinstance(label, str) or not is_valid_id(label):
            fatal.append(f"invalid label {label!r}")
            continue
        if not (plsi_home() / label / "subject.json").is_file():
            held[label] = f"no instance (plsi init --instance {label} --relation other)"
        elif r.get("status") not in {s.value for s in RosterStatus}:
            held[label] = f"status must be one of {', '.join(s.value for s in RosterStatus)}"
        if names[_key(label)] > 1:
            fatal.append(f"{label}: the label is used twice, or is another patient's handle")
        handle = r.get("handle")
        if isinstance(handle, str) and names[_key(handle)] > 1:
            fatal.append(f"{label}: handle {handle!r} is another patient's handle or label; each must name one patient")
        if handle is not None and not (isinstance(handle, str) and handle.isprintable()
                                       and 0 < len(handle) <= HANDLE_MAX):
            fatal.append(f"{label}: handle must be one line of at most {HANDLE_MAX} characters")
        if r.get("patient_access", PatientAccess.NONE) not in {a.value for a in PatientAccess}:
            fatal.append(f"{label}: patient_access must be one of {', '.join(a.value for a in PatientAccess)}")
        for key in ("inbox", "shared_folder"):
            folder = r.get(key)
            if folder is not None and (not isinstance(folder, str) or not FOLDER_RE.match(folder)):
                fatal.append(f"{label}: {key} is not a Drive folder id")
            elif folder is not None and uses[folder] > 1:
                fatal.append(f"{label}: {key} folder is used twice; every patient folder is that patient's alone")
    return fatal, held


def _waiting(label: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Files taken in for `label` that are still in its inbox and that no recorded observation came from."""
    from plsi.life.store import EventStore

    recorded = {e.payload.origin.file_sha256 for e in EventStore(label).iter_events(EventType.OBSERVATION)}
    folder = plsi_home() / "inbox" / label
    out: dict[str, dict[str, Any]] = {}  # by file name: a return to an earlier version points at the same file
    for e in entries:
        if e.get("status") == "new" and e["label"] == label and e["sha256"] not in recorded and (folder / e["path_name"]).is_file():
            out[e["path_name"]] = {"file": e["file"], "at": e["at"], "path": str(folder / e["path_name"])}
    return list(out.values())


def check(drive: bool = False) -> dict[str, Any]:
    """Every patient with Drive folders, the sharing each folder needs, the files still waiting for intake, and every
    roster problem; `drive` also asks Drive who can open each folder (service account)."""
    found, entries = rows(), ledger()
    fatal, held = problems(found)
    patients = []
    for r in found:
        if not (r.get("inbox") or r.get("shared_folder")):
            continue
        label = r.get("label")
        patients.append({"label": label, "shown": shown(r), "status": r.get("status"),
                         "patient_access": r.get("patient_access", PatientAccess.NONE.value),
                         "sharing": [{"folder": r[k], "role": k, "patient_access": a}
                                     for k, a in ACCESS.items() if r.get(k)],
                         "waiting": [] if label in held or not is_valid_id(str(label)) else _waiting(label, entries)})
    out: dict[str, Any] = {"patients": patients, "problems": fatal + [f"{k}: {v}" for k, v in held.items()]}
    if drive:
        from plsi import clinic_drive

        out["drive"] = clinic_drive.audit(found)
        out["problems"] += out["drive"].pop("problems")
    return out


def sync(file: str | None = None) -> dict[str, Any]:
    """Route downloaded Drive files into <PLSI_HOME>/inbox/<label>/: from `file` ([{folder, file, path}], the agent's
    connector) or, without it, pulled from every active inbox with the service account.

    Returns {new: [{label, path, document, revised}], duplicate, held, unrouted, failed, notice}; `notice` has one
    line per patient with new or failed files (handle and label, counts only), for the physician. A file is routed
    only from a
    patient's own inbox and only while that patient is active. The same version again, or the same content under
    another file id, is a duplicate; a known file id with other content (also a return to an earlier version) is
    `revised` (feed it to `plsi revise --document <file id>`).
    """
    found = rows()
    fatal, held = problems(found)
    if fatal:
        raise ValidationError("the roster has problems: " + "; ".join(fatal), code="ROSTER_INVALID", hint="plsi clinic check")
    entries = ledger()
    out: dict[str, list[Any]] = {"new": [], "duplicate": [], "held": [], "unrouted": [], "failed": []}
    if file is None:
        from plsi import clinic_drive

        items, failed = clinic_drive.pull(found, held, entries)
        out["failed"] += [record({"status": "failed", **f}) for f in failed]
    else:
        items = load_json(file)
        if not isinstance(items, list):
            raise ValidationError("the listing must be a list of {folder, file, path}")
    inbox = {r["inbox"]: r for r in found if r.get("inbox")}
    shared = {r["shared_folder"] for r in found if r.get("shared_folder")}
    last: dict[str, str] = {}
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in entries:
        if e.get("status") == "new":
            last[e["file"]] = e["sha256"]
            seen[(e["label"], e["sha256"])].add(e["file"])
    for item in items:
        status, entry = _route(item, inbox, shared, held, last, seen)
        out[status].append(entry)
    new, failed = Counter(e["label"] for e in out["new"]), Counter(e.get("label", "unknown") for e in out["failed"])
    by_label = {r.get("label"): r for r in found}
    out["notice"] = []
    for label in sorted(set(new) | set(failed)):
        counts = [f"{new[label]} new file(s)"] * bool(new[label]) + [f"{failed[label]} failed"] * bool(failed[label])
        out["notice"].append(f"{shown(by_label.get(label, {'label': label}))}: {', '.join(counts)}")
    return out


def _route(item: Any, inbox: dict[str, dict[str, Any]], shared: set[str], held: dict[str, str],
           last: dict[str, str], seen: dict[tuple[str, str], set[str]]) -> tuple[str, dict[str, Any]]:
    folder, file_id, path = (item.get(k) for k in ("folder", "file", "path")) if isinstance(item, dict) else (None,) * 3
    drive_meta = {k: item[k] for k in ("md5", "modified") if isinstance(item, dict) and item.get(k)}
    if not (isinstance(file_id, str) and FOLDER_RE.match(file_id) and isinstance(path, str) and Path(path).is_file()):
        return "failed", record({"status": "failed", "file": file_id if isinstance(file_id, str) else None,
                                 "why": "needs a Drive file id and the path of the downloaded file"})
    row = inbox.get(folder)
    size = item.get("size")  # Drive's size, when the agent passes it: a damaged download is never taken in
    if size is not None and str(size) != str(Path(path).stat().st_size):
        return "failed", record({"status": "failed", "file": file_id, **({"label": row["label"]} if row else {}),
                                 "why": "the download's size differs from Drive's; download it again"})
    if row is None:
        why = "a patient's shared folder is for the physician's files only" if folder in shared else "not a patient inbox"
        return "unrouted", record({"status": "unrouted", "file": file_id, "why": why, **drive_meta})
    label = row["label"]
    if label in held or row["status"] != RosterStatus.ACTIVE:
        return "held", record({"status": "held", "file": file_id, "label": label, "why": held.get(label, row["status"]), **drive_meta})
    sha = sha256_file(Path(path))
    if sha == last.get(file_id) or seen[(label, sha)] - {file_id}:
        return "duplicate", record({"status": "duplicate", "file": file_id, "label": label, "sha256": sha, **drive_meta})
    suffix = Path(path).suffix.lower()
    name = sha[:12] + (suffix if SUFFIX_RE.match(suffix) else "")
    dest = safe_join(plsi_home(), "inbox", label, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():  # back to a version taken in before: the same bytes are already there
        Path(path).unlink()
    else:
        shutil.move(path, dest)
    revised = file_id in last
    last[file_id] = sha
    seen[(label, sha)].add(file_id)
    record({"status": "new", "file": file_id, "label": label, "sha256": sha, "path_name": name, **drive_meta})
    return "new", {"label": label, "path": str(dest), "document": file_id, "revised": revised}
