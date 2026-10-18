"""S1a's unconditional source/render guard. No production imports or exemptions.

Debt is an exact occurrence, not permission for a word, file or module. This is
a deliberately conservative vocabulary detector, not a semantic proof. Python
strings (including local strings), f-strings, folded concatenation, comments,
docstrings and supported text assets under src are inspected. Tests are tenant data,
and are outside the runtime source root. Presets/marketing are inside it.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tokenize
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).parent / "fixtures" / "agnostic"
SEEN_RENDERS = set()
OBSERVED_RENDERS = {}
REVIEW_DIR = None
# Word families include the previously omitted specialist and clinical desks.
# Neutral capability names (stay, appointment, resource) confer no trade policy.
WORDS = (
    "boiler boilers plumbing plumber plumbers heating radiator radiators engineer engineers "
    "callout callouts kennel kennels dog dogs puppy puppies pet pets labrador spaniel "
    "cockapoo vaccination vaccinations vaccine vaccines jab jabs vet vets veterinary "
    "animal animals breed breeds daycare grooming groomer groomers horse horses "
    "farrier farriers hoof hooves shoeing lameness mot vehicle vehicles car cars tyre tyres "
    "garage garages mechanic mechanics pupil pupils tutor tutors lesson lessons swim swimming "
    "pallet pallets depot depots haulage haulier hauliers freight lorry lorries trailer trailers "
    "landlord landlords salon salons haircut haircuts patient patients practitioner practitioners "
    "surgery surgeon surgeons dentist dentists dental therapist therapists massage massages "
    "cleaner cleaners cleaning electrician electricians electrical roofing roofer roofers "
    "caterer caterers catering florist florists wedding weddings barber barbers stylist stylists "
    "physio clinic clinics grocer butcher butchers bakery bakeries loaves vintages NHS CP12 "
    "consignment consignments subcontractor subcontractors recruitment recruiter recruiters "
    "kennel hotel hotels boarding nursery nurseries child children feeding feed feeds "
    "groom groomed gas installer installers installation installations locksmith locksmiths"
).split() + ["gas safe", "cp12", "fault code", "no heating", "call-out", "fuse board",
             "rewire", "driving test", "letting agent", "hot tub", "oven clean", "meet and greet",
             "meet-and-greet", "look-round", "look round"]
RX = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(set(WORDS), key=len, reverse=True)))
                + r")(?!\w)", re.I)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise(value):
    return " ".join(value.split())


def render_record(value):
    # Canonical JSON removes encoding/key-order noise, not prompt whitespace or
    # any values. Every byte of every string remains covered by the comparison.
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": digest(canonical), "excerpt": canonical[:160]}


def occurrence(path, line, kind, value, column=0):
    words = sorted({m.group().lower() for m in RX.finditer(value)})
    if not words:
        return None
    return {"path": path, "line": line, "column": column, "kind": kind, "fingerprint": digest(normalise(value)),
            "words": words, "text": value}


def index_occurrences(hits):
    counts = Counter()
    for hit in hits:
        identity = (hit["path"], hit["scope"], hit["kind"], hit["fingerprint"])
        hit["occurrence"] = counts[identity]
        counts[identity] += 1
    return sorted(hits, key=key)


def scan_source(path, source):
    hits = []
    lines = source.splitlines()
    if path.endswith(".py"):
        tree = ast.parse(source)
        scopes = []
        def visit(node, parents=()):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parents = (*parents, node.name)
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                scopes.append((start, node.end_lineno, ".".join(parents), len(parents)))
            for child in ast.iter_child_nodes(node):
                visit(child, parents)
        visit(tree)
        # tokenize retains comments and exact physical locations; AST additionally
        # sees adjacent literals and f-string/static concatenation across lines.
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            kind = "comment" if token.type == tokenize.COMMENT else "string"
            for offset, line in enumerate(token.string.splitlines()):
                hit = occurrence(path, token.start[0] + offset, kind, line,
                                 token.start[1] if offset == 0 else 0)
                if hit:
                    hits.append(hit)
        def static(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(static(n) if not isinstance(n, ast.FormattedValue) else "{dynamic}"
                               for n in node.values)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return static(node.left) + static(node.right)
            return "{dynamic}"
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                continue
            value = static(node)
            words = {m.group().lower() for m in RX.finditer(value)}
            if isinstance(node, ast.JoinedStr):
                # Fingerprint the entire expression, so repeating the same
                # leaky fragment within it cannot collapse into existing debt.
                hit = occurrence(path, node.lineno, "f-string", value, node.col_offset)
                if hit:
                    hits.append(hit)
                continue
            # Avoid charging ordinary literals twice, but retain constructed words.
            if not words:
                continue
            original = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if words - {m.group().lower() for m in RX.finditer(original)}:
                hits.append(occurrence(path, node.lineno, "constructed", value, node.col_offset))
        for hit in hits:
            enclosing = [s for s in scopes if s[0] <= hit["line"] <= s[1]]
            hit["scope"] = max(enclosing, key=lambda s: s[3])[2] if enclosing else "<module>"
    else:
        blocks = []
        for line, value in enumerate(source.splitlines(), 1):
            tags = list(re.finditer(r"{%[-+]?\s*(block\s+(\w+)|endblock\b)[^%]*%}", value))
            for tag in tags:
                if tag.group(2):
                    blocks.append(tag.group(2))
            hit = occurrence(path, line, "asset", value)
            if hit:
                hit["scope"] = ".".join(blocks) or "<module>"
                hits.append(hit)
            for tag in tags:
                if not tag.group(2) and blocks:
                    blocks.pop()
    # AST construction can revisit the same physical token. Deduplicate those
    # visits before assigning indices, never separate identical occurrences.
    unique = {(h["line"], h["column"], h["kind"], h["fingerprint"]): h for h in hits if h}
    return index_occurrences(sorted(unique.values(), key=lambda h: (h["line"], h["column"], h["kind"])))


def not_ignored_files(root):
    """Files git would track or add under src/ (tracked, plus untracked-but-not-ignored),
    or None when root is not itself a git checkout or git is unavailable.
    Git-ignored build output (compiled CSS, bundled JS) is not source."""
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", "--", "src"],
                             capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {root / n for n in out.decode("utf-8").split("\0") if n}


def source_inventory(root=ROOT):
    # No module allowlist: additions are automatically in scope, including
    # catalogues, recipes, specialist desks, public pages and UI defaults.
    # In a git checkout, git-ignored files are out of scope; without git, scan all.
    source_root = root / "src"
    if not source_root.is_dir():
        raise ValueError(f"Agnostic source root is missing: {source_root}")
    allowed = not_ignored_files(root)
    paths = sorted(p for p in source_root.rglob("*") if p.is_file()
                  and (allowed is None or p in allowed)
                  and p.suffix.lower() in {".py", ".html", ".jinja", ".jinja2", ".js", ".mjs",
                                           ".json", ".css", ".txt", ".yaml", ".yml"})
    if not paths:
        raise ValueError(f"Agnostic source inventory is empty: {source_root}")
    return paths


def scan_sources(root=ROOT):
    return [h for p in source_inventory(root)
            for h in scan_source(p.relative_to(root).as_posix(), p.read_text(encoding="utf-8-sig"))]


def strings(value, path="$"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in sorted(value.items()):
            yield f"{path}.<key:{k}>", k
            yield from strings(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from strings(v, f"{path}[{i}]")


def scan_render(name, value, provenance=None):
    hits = []
    for pointer, text in strings(value):
        # Only the exact tagged value from a named fixture read boundary is
        # tenant-owned. The same word elsewhere remains a leak. No word removal.
        for boundary, owned in (provenance or {}).items():
            tagged = f"[[TENANT:{boundary}]]{owned}[[/TENANT:{boundary}]]"
            text = text.replace(tagged, f"[[TENANT:{boundary}]]")
        for line, content in enumerate(text.splitlines(), 1):
            hit = occurrence(f"render/{name}/{pointer}", line, "render", content)
            if hit:
                hit["scope"] = "<render-value>"  # JSON pointer is already in path
                hits.append(hit)
    return index_occurrences(hits)


def key(hit):
    return (hit["path"], hit["scope"], hit["kind"], hit["fingerprint"], hit["occurrence"])


def reconcile(actual, debt):
    for row in debt:
        assert re.fullmatch(r"U(?:0[1-9]|1[0-9]|2[0-4])", row.get("owner", "")), row
        assert re.fullmatch(r"S[1-5][a-h]", row.get("closure_step", "")), row
        assert row.get("reason"), row
    expected = {key(h): h for h in debt}
    observed = {key(h): h for h in actual}
    assert len(expected) == len(debt), "Duplicate debt entries"
    assert len(observed) == len(actual), "Duplicate observed occurrences"
    new, stale = observed.keys() - expected.keys(), expected.keys() - observed.keys()
    assert not new and not stale, (
        "New/changed agnostic leaks:\n" + "\n".join(str(k) for k in sorted(new))
        + "\nStale/changed debt (remove or review exact locations):\n"
        + "\n".join(str(k) for k in sorted(stale)))
    # Locations and excerpts are review hints only. Content is already covered
    # by its normalised fingerprint; owner/reason are validated above.


def read_json(name):
    value = json.loads((DATA / name).read_text(encoding="utf-8"))
    if name == "debt.json":
        return [{**row, "reason": value["reasons"][row["reason"]]} for row in value["entries"]]
    return value
