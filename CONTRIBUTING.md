# Contributing to Python Watchpoint

Thanks for your interest! This is a side project maintained on a best-effort
basis – **there is no support SLA**. Issues and pull requests are welcome, but
response times will vary.

## Reporting issues

When filing a bug, please include:

- PyCharm version and UI mode (New UI vs Classic UI).
- Python version (the runtime requires **3.12+**).
- Whether `pydevd_boost` was enabled or disabled (`PYCHARM_WATCHPOINT_BOOST=0`).
- A minimal reproduction if possible, plus the contents of
  `/tmp/pythonwatchpoint.log` when run with `PYCHARM_WATCHPOINT_LOG=1`.

## Development setup

You need **JDK 21** (≤ 23) and a Python 3.12+ interpreter with `pyminify`
available (`pip install python-minifier`).

```bash
./gradlew compileKotlin     # build the plugin
./gradlew runIde            # launch a sandbox PyCharm with the plugin loaded
./gradlew buildPlugin       # produce the distributable zip
```

- `org.gradle.java.home` and `pyminifyPath` in `gradle.properties` are
  machine-specific – override them locally (or via `-P` flags) rather than
  committing your paths.
- The bundled Python runtime lives in `src/main/resources/python/`. Run its
  tests with `pytest` from that directory.

## Pull requests

- Keep changes focused; match the existing code style.
- Read `CLAUDE.md` (and `src/main/resources/python/CLAUDE.md`) before any
  architectural change – they document hard-won constraints around pydevd and
  PEP 669 that are easy to break.
- Bump `_RUNTIME_VERSION` in the Python runtime on behavioral changes.

## License

By contributing, you agree that your contributions will be licensed under the
Apache License 2.0, the same license that covers this project.
