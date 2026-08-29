"""Draft a candidate driver for a never-observed function using watsonx.ai.

The whole point of this project is that a claim is worth nothing until something
checks it, and a model's output is a claim like any other. So this tool does not
promote anything. It writes a candidate into its own directory and stops. The
same gate that judges a hand-written driver then judges this one, with the same
rules and the same refusal classes, and it refuses most of them.

Credentials are read from the environment, never from the repository:

    WATSONX_APIKEY        the key itself, or
    WATSONX_APIKEY_FILE   a path to a file holding it
    WATSONX_PROJECT_ID    the project the key can reach
    WATSONX_URL           optional, defaults to the Dallas endpoint

Usage:
    python tools/wx_draft_driver.py --unit "<qualname>" [--out drivers-candidates]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODEL = "ibm/granite-4-h-small"
DEFAULT_URL = "https://us-south.ml.cloud.ibm.com"
IAM_URL = "https://iam.cloud.ibm.com/identity/token"


def _api_key() -> str:
    key = os.environ.get("WATSONX_APIKEY", "").strip()
    if key:
        return key
    path = os.environ.get("WATSONX_APIKEY_FILE", "").strip()
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8-sig").strip()
    sys.exit(
        "no credential: set WATSONX_APIKEY or WATSONX_APIKEY_FILE. The key must "
        "never be written into this repository."
    )


def _token() -> str:
    data = urllib.parse.urlencode(
        {"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": _api_key()}
    ).encode()
    req = urllib.request.Request(
        IAM_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["access_token"]


def chat(messages: list[dict], max_tokens: int = 1400) -> str:
    """Call watsonx.ai text chat and return the assistant's message."""
    project = os.environ.get("WATSONX_PROJECT_ID", "").strip()
    if not project:
        sys.exit("set WATSONX_PROJECT_ID to the project the key can reach")
    base = os.environ.get("WATSONX_URL", DEFAULT_URL).rstrip("/")
    body = json.dumps(
        {
            "model_id": MODEL,
            "project_id": project,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        base + "/ml/v1/text/chat?version=2024-10-08",
        data=body,
        headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit("watsonx.ai returned %s: %s" % (e.code, e.read().decode()[:500]))
    return payload["choices"][0]["message"]["content"]


def find_unit(evidence: dict, needle: str) -> tuple[str, dict]:
    hits = [(k, u) for k, u in evidence["units"].items() if needle in k]
    never = [(k, u) for k, u in hits if u.get("provenance") == "never_observed"]
    if not never:
        sys.exit("no never_observed unit matches %r (%d units matched at all)"
                 % (needle, len(hits)))
    never.sort(key=lambda kv: len(kv[0]))
    return never[0]


def source_of(unit: dict) -> str:
    lines = Path(unit["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[unit["def_line"] - 1 : unit["body_end"]])


def call_sites(package: Path, name: str) -> list[str]:
    """Every line in the package that mentions the name, as raw material.

    The model is given real lines rather than left to invent one. It still has to
    choose, and the gate still has to agree: it parses the cited line and refuses
    it unless the name is called there, in a file that defines or imports the
    function. That is a name match, not a resolved binding. See the limit
    documented under "Testing the gate" in the README.
    """
    try:
        out = subprocess.run(
            ["grep", "-rnE", "--include=*.py",
             r"(^|[^A-Za-z0-9_.])%s *\(" % re.escape(name), str(package)],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    keep = []
    for line in out.splitlines():
        if re.search(r"def\s+%s\b" % re.escape(name), line):
            continue
        keep.append(line.strip()[:200])
    return keep[:25]


PROMPT = """You are writing a driver that proves a Python function actually runs.

Function `{name}` from `{relfile}` has never been observed executing. Write a
standalone Python script that imports the real function and calls it with inputs
that mirror how production calls it.

Rules the script must satisfy:
- Start with `import sys` then `sys.path.insert(0, 'target/visidata')`.
- Import the real function from its real module. Do not redefine it.
- Include exactly one line of the form:
  `# call site: <file>:<line> -- <the code on that line>`
  The file must be one that calls this function, and the line number must be the
  line where the call appears. This is checked against the source; a wrong file
  or line is rejected.
- Exit 0 when the checks pass and non-zero when they fail.
- Output only Python. No prose, no markdown fences.

Source of the function:
```python
{source}
```

Lines in the package that mention `{name}` (file:line:code):
{sites}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", required=True, help="substring of the unit key")
    ap.add_argument("--evidence", default=str(REPO / "evidence.json"))
    ap.add_argument("--out", default=str(REPO / "drivers-candidates"))
    ap.add_argument("--package", default=str(REPO / "target" / "visidata" / "visidata"))
    args = ap.parse_args()

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    key, unit = find_unit(evidence, args.unit)
    name = key.split("::")[-1].split("#")[0].split(".")[-1]
    relfile = str(Path(unit["file"]).relative_to(REPO)).replace("\\", "/")

    sites = call_sites(Path(args.package), name)
    print("unit      :", key)
    print("provenance:", unit.get("provenance"))
    print("call sites found in package:", len(sites))

    text = chat([{"role": "user", "content": PROMPT.format(
        name=name, relfile=relfile, source=source_of(unit),
        sites="\n".join(sites) or "(none found)")}])

    code = text.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-z]*\n", "", code)
        code = re.sub(r"\n```\s*$", "", code)

    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)
    dest = out_dir / ("%s.py" % key.split("::")[-1].split("#")[0])
    dest.write_text(code + "\n", encoding="utf-8")

    # The declaration may be indented; anchoring on the line start missed those
    # and reported "no call site declared" for drivers that had one.
    declared = re.search(r"^[ \t]*#\s*call site.*$", code, re.M)
    print("written   :", dest.relative_to(REPO))
    print("declares  :", declared.group(0).strip() if declared else "(no call site declared)")
    print()
    print("This is a candidate. Nothing is promoted until the gate agrees:")
    print("  python first_light.py --promote-driver --all --drivers-dir %s "
          "--evidence <a copy>" % out_dir.name)
    print("Point it at a copy. The published evidence is what the report is built")
    print("from, and the promotion step would edit it.")


if __name__ == "__main__":
    main()
