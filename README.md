# Python Watchpoint  <img width="50" height="50" alt="watchpoint" src="https://github.com/user-attachments/assets/aa960e6e-dc14-409a-9f40-8be6a5cff3f7" />

**Adds Python data breakpoints (watchpoints) support to PyCharm.**

Python Watchpoint lets you break on _data_ changes. When a watched local variable or object
attribute changes value, the debugger pauses at the mutation line and highlights the exact location
with the old and new values shown inline.

## What it does

- Watches local variables and object attributes for value changes.
- Pauses the debugger on the mutation line.
- Adds quick UI actions in PyCharm:
  - `Debug with Watchpoint` in the toolbar.
  - `Add Watchpoint` / `Remove Watchpoint` in the Variables panel context menu.
- Highlights watchpoint hits in the editor and marks watched entries in the Variables tree.

<img width="600" height="747" alt="ezgif-68c8ffb42ec7308f" src="https://github.com/user-attachments/assets/1de133e7-daae-4abf-882d-a49aa3861c1f" />


## Current limitations

- Requires **PyCharm 2023.3** or newer.
- Python runtime support is **3.12+**.
- Works with PyCharm's **pydevd** debugger flow.
- **debugpy is not supported yet**.
- **In-place mutation of C-extension objects (NumPy, Pandas, etc.) is not detected.**
  Watchpoints hook Python-level name rebinding and attribute assignment; operations like
  `arr[0] = 99` or `arr += 1` mutate the underlying buffer in C without either, so there
  is nothing to observe. Reassigning the whole variable (`arr = np.zeros(3)`) is still caught.

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

