# fl_hook.py -- Bob hook configuration

`tools/fl_hook.py` is wired into Bob as a `PreToolUse` hook.  Bob invokes it
before every file-write operation and the hook prints one advisory line
describing the provenance of the function at the target location.

## Entry to add in `~/.claude/hooks/hooks.json`

Add the following object inside the `"PreToolUse"` array:

```json
{
  "matcher": "Edit|Write|MultiEdit",
  "hooks": [
    {
      "type": "command",
      "command": "python <repo-root>/tools/fl_hook.py",
      "timeout": 5
    }
  ],
  "description": "First Light: report provenance of the function being edited",
  "id": "pre:edit-write:first-light"
}
```

Replace `<repo-root>` with the absolute path to this repository, e.g.
`C:\Users\User\Downloads\proyectos2026\bobhackathon\first-light`.

## Environment variable

Set `FIRST_LIGHT_EVIDENCE` to the absolute path of `evidence.json` so the hook
can find it regardless of which file is being edited:

```
FIRST_LIGHT_EVIDENCE=<repo-root>\evidence.json
```

Without this variable the hook walks up the directory tree from the edited file
to find `evidence.json`.  This works when editing files inside the target
package (which is a subdirectory of the repo), but setting the variable
explicitly is more reliable.

## What the hook outputs

```
[first_light] never observed -- visidata._input.injectInput has never been
observed executing (baselines run: cli, test_suite, replay)
```

```
[first_light] observed in situ -- visidata.addGlobals was observed executing
under normal operation (observed by: cli, test_suite, replay)
```

```
[first_light] observed under driver -- visidata.aggregators._percentile only
ran because a driver was built to reach it (not reached by: cli, test_suite,
replay)
```

By default the hook exits 0 and never blocks an edit.

Pass `--strict` in the command (or set `FIRST_LIGHT_STRICT=1`) and an edit to a
function with no execution record exits 2, which stops the write:

```json
"command": "python C:/path/to/first-light/tools/fl_hook.py --strict"
```

Strict mode blocks on the resolved provenance of the unit being edited, never on
the text of the advisory. When the payload carries no line number the edit
cannot be placed in one function, and not knowing which function is being edited
is not evidence that it is unobserved, so nothing is blocked. An internal error
in the hook does not block either.

## Selftest

```
python tools/fl_hook.py --selftest --evidence evidence.json
```

This exercises three payload shapes and five graceful-degradation cases and
prints OK/FAIL for each.
