# Python Watchpoint

PyCharm plugin that adds Python watchpoints: pause execution when a watched value changes.

## What it does

- Watches local variables and object attributes for value changes.
- Pauses the debugger on the mutation line.
- Adds quick UI actions in PyCharm:
  - `Debug with Watchpoint` in the toolbar.
  - `Add Watchpoint` / `Remove Watchpoint` in the Variables panel context menu.
- Highlights watchpoint hits in the editor and marks watched entries in the Variables tree.

## Current limitations

- Python runtime support is **3.12+**.
- Works with PyCharm's **pydevd** debugger flow.
- **debugpy is not supported yet**.

## Quick start (development)

```bash
./gradlew compileKotlin
./gradlew runIde
```

## Packaging

```bash
./gradlew buildPlugin
```

Output zip:

- `build/distributions/pythonwatchpoint-1.0.0.zip`

## Runtime docs

Python runtime package docs:

- `src/main/resources/python/README.md`
- `src/main/resources/python/CLAUDE.md`

## Related component: pydevd_boost

- `src/main/resources/python/pydevd_boost.py` is a separate pydevd performance patch module.
- It is shipped together with this plugin today, but is conceptually independent from watchpoint logic.
- It can be disabled per run config with `PYCHARM_WATCHPOINT_BOOST=0`.
- Future split into a dedicated plugin/module is possible.

## Roadmap notes

- Add preview GIFs to show the end-to-end workflow.
- Track debugpy support as a future enhancement.

