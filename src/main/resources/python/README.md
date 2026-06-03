# Python Watchpoint Runtime

This directory contains the injected Python runtime used by the PyCharm plugin.

## Scope

- Runtime package: `_pycharm_watchpoint/`
- Runtime test/bootstrap helper: `conftest.py`
- Runtime tests: `tests/`

## pydevd_boost status

- `pydevd_boost.py` is a separate pydevd performance patch module that ships in this repository.
- It is related, but conceptually independent from the core watchpoint runtime package.
- It is enabled by default in injected sessions and can be disabled with `PYCHARM_WATCHPOINT_BOOST=0`.
- Long-term, it can be split into its own plugin/module without changing core watchpoint semantics.

## Compatibility

- Python: **3.12, 3.13, 3.14**
- Debugger integration: **pydevd only**
- `debugpy`: **not supported yet**

## Why this exists

PyCharm does not provide first-class Python watchpoints out of the box. This runtime adds
watchpoint behavior by combining `sys.monitoring` callbacks with pydevd pause integration.

## Run tests

From this directory (`src/main/resources/python/`):

```bash
python3.12 -m pytest
python3.13 -m pytest
python3.14 -m pytest
```

## Implementation notes

- Entry package is `_pycharm_watchpoint`.
- Public API is re-exported from `_pycharm_watchpoint/__init__.py`.
- Watchpoint hit class is rebranded to module `_pycharm_watchpoint` for exception breakpoint matching.
- Detailed design contracts and anti-patterns are documented in `CLAUDE.md`.

## Important references

- `src/main/resources/python/CLAUDE.md` - runtime architecture and invariants
- `src/main/resources/python/pydevd_boost.py` - pydevd boost implementation
- `src/main/resources/python/PYDEVD_BOOST.md` - pydevd boost internals

