package com.pythonwatchpoint.actions

import com.intellij.execution.ProgramRunnerUtil
import com.intellij.execution.RunManager
import com.intellij.execution.executors.DefaultDebugExecutor
import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.xdebugger.XDebuggerManager
import com.jetbrains.python.run.AbstractPythonRunConfiguration
import com.pythonwatchpoint.icons.WatchpointIcons
import com.pythonwatchpoint.services.WatchpointSessionManager
import java.io.File
import java.nio.file.Files
import java.util.Base64

/**
 * Toolbar entry point. Clones the currently-selected Python run configuration,
 * augments it with a sitecustomize.py that materializes + imports the
 * `_pycharm_watchpoint` runtime package at interpreter start-up, then launches a
 * debug session on the clone. The original config is left untouched so plain
 * "Debug" still produces a clean session.
 *
 * Clones the selected run config so the original stays clean.
 */
class DebugWithWatchpointAction : AnAction(), DumbAware {
    private val logger = Logger.getInstance(DebugWithWatchpointAction::class.java)

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return

        // Defensive: scrub any leftover watchpoint environment from prior aborted runs.
        cleanAllConfigurations(project)

        val watchpointPackage = loadWatchpointPackage()
        if (watchpointPackage == null) {
            logger.error("Failed to load _pycharm_watchpoint package")
            return
        }
        val runManager = RunManager.getInstance(project)
        val selectedSettings = runManager.selectedConfiguration ?: return
        val originalConfig = selectedSettings.configuration

        if (originalConfig !is AbstractPythonRunConfiguration<*>) {
            logger.warn("Not a Python configuration – Debug with Watchpoint only works with Python configs")
            return
        }

        // Clone the original so we can mutate envs without contaminating the user's saved config.
        val clonedConfig = originalConfig.clone() as AbstractPythonRunConfiguration<*>
        clonedConfig.name = "[WATCHPOINT] ${originalConfig.name}"

        injectViaSiteCustomize(project, clonedConfig, watchpointPackage)

        val newSettings = runManager.createConfiguration(clonedConfig, selectedSettings.factory)
        newSettings.isTemporary = true

        WatchpointSessionManager.getInstance(project).startSession(watchpointPackage)

        val executor = DefaultDebugExecutor.getDebugExecutorInstance()
        ProgramRunnerUtil.executeConfiguration(newSettings, executor)
    }

    /**
     * Removes watchpoint markers (env var + PYTHONPATH temp dir) from every saved Python
     * configuration in the project. Catches the case where a previous run was killed
     * without going through onProcessStopped cleanup and left stale entries behind.
     */
    private fun cleanAllConfigurations(project: Project) {
        val runManager = RunManager.getInstance(project)
        var cleanedCount = 0

        // Check if any config needs cleaning before acquiring write action.
        val dirtyConfigs = runManager.allSettings.filter { settings ->
            val config = settings.configuration
            if (config !is AbstractPythonRunConfiguration<*>) return@filter false
            val envs = config.envs
            envs.containsKey("PYCHARM_WATCHPOINT_ACTIVE") ||
            envs.containsKey("PYCHARM_WATCHPOINT_USER_ROOTS") ||
            envs.containsKey("PYDEVD_USE_CYTHON") ||
            (envs["PYTHONPATH"]?.contains("pycharm_watchpoint_") == true)
        }

        if (dirtyConfigs.isEmpty()) return

        com.intellij.openapi.application.WriteAction.run<Exception>{
            for (settings in runManager.allSettings) {
                val config = settings.configuration
                if (config !is AbstractPythonRunConfiguration<*>) continue

                val envs = config.envs.toMutableMap()
                var changed = false

                if (envs.remove("PYCHARM_WATCHPOINT_ACTIVE") != null) {
                    changed = true
                }
                if (envs.remove("PYCHARM_WATCHPOINT_USER_ROOTS") != null) {
                    changed = true
                }
                if (envs.remove("PYDEVD_USE_CYTHON") != null) {
                    changed = true
                }

                val pythonPath = envs["PYTHONPATH"]
                if (pythonPath != null && pythonPath.contains("pycharm_watchpoint_")) {
                    val cleanPath = pythonPath.split(File.pathSeparator)
                        .filter { !it.contains("pycharm_watchpoint_") }
                        .joinToString(File.pathSeparator)

                    if (cleanPath.isEmpty()) {
                        envs.remove("PYTHONPATH")
                    } else {
                        envs["PYTHONPATH"] = cleanPath
                    }
                    changed = true
                }

                if (changed) {
                    config.envs = envs
                    cleanedCount++
                }
            }
        }

        if (cleanedCount > 0) {
            logger.warn("Sanitized $cleanedCount configurations (removed stale watchpoint settings).")
        }
    }

    /**
     * Load the `_pycharm_watchpoint` runtime package from plugin resources as a
     * `filename -> source` map. Returns null if any module is missing.
     *
     * `MODULE_FILES` is the single source of truth for which files make up the
     * package – keep it in sync with `src/main/resources/python/_pycharm_watchpoint/`
     * (add a line when adding a submodule). We list files explicitly rather than
     * enumerate the resource directory because directory listing is unreliable
     * against the Gradle IntelliJ jar layout.
     */
    private fun loadWatchpointPackage(): Map<String, String>? {
        return try {
            val pkg = LinkedHashMap<String, String>()
            for (name in MODULE_FILES) {
                val text = javaClass.getResourceAsStream("/python/_pycharm_watchpoint/$name")
                    ?.bufferedReader()
                    ?.readText()
                if (text == null) {
                    logger.error("Missing watchpoint runtime module: $name")
                    return null
                }
                pkg[name] = text
            }
            pkg
        } catch (e: Exception) {
            logger.error("Failed to read _pycharm_watchpoint package", e)
            null
        }
    }

    private fun loadBoostScript(): String? {
        return try {
            javaClass.getResourceAsStream("/python/pydevd_boost.py")
                ?.bufferedReader()
                ?.readText()
        } catch (e: Exception) {
            logger.warn("Failed to read pydevd_boost.py – boost disabled", e)
            null
        }
    }

    /**
     * Writes the `_pycharm_watchpoint` package to a temp dir and a sitecustomize.py
     * that imports it at interpreter startup, then prepends the temp dir to
     * PYTHONPATH and sets the activation env var. Gated by PYCHARM_WATCHPOINT_ACTIVE
     * so other interpreters that happen to inherit the path don't accidentally boot
     * watchpoint logic.
     *
     * The package is written as real .py files (so normal import machinery +
     * relative imports just work, and WatchpointHit.__module__ resolves to
     * '_pycharm_watchpoint' – what the IDE exception breakpoint matches). The temp
     * dir is the parent of the package dir and is on PYTHONPATH, so a plain
     * `import _pycharm_watchpoint` resolves.
     *
     * Also forces PYDEVD_USE_CYTHON=NO and installs pydevd_boost patches for
     * dramatically faster debug session startup (15-156x improvement on PEP 669 path).
     */
    private fun injectViaSiteCustomize(
        project: Project,
        config: AbstractPythonRunConfiguration<*>,
        watchpointPackage: Map<String, String>,
    ) {
        try {
            val tempDir = Files.createTempDirectory("pycharm_watchpoint_").toFile()
            val siteCustomize = File(tempDir, "sitecustomize.py")

            // Materialize the runtime package as real files: <tempDir>/_pycharm_watchpoint/*.py
            val pkgDir = File(tempDir, "_pycharm_watchpoint")
            pkgDir.mkdirs()
            for ((name, source) in watchpointPackage) {
                File(pkgDir, name).writeText(source)
            }

            // Load and encode pydevd_boost.py for injection alongside watchpoint
            val boostCode = loadBoostScript()
            val encodedBoost = if (boostCode != null) {
                Base64.getEncoder().encodeToString(boostCode.toByteArray())
            } else {
                ""
            }

            siteCustomize.writeText("""
import sys
import os
import base64
import types

if os.environ.get('PYCHARM_WATCHPOINT_ACTIVE') == '1':
    # Force pure-Python pydevd – we apply performance patches to it that make it
    # faster than the (buggy) Cython version. Also prevents ImportError on Python
    # versions where Cython .so files don't exist (3.13+).
    os.environ['PYDEVD_USE_CYTHON'] = 'NO'

    # Install pydevd boost patches (PEP 669 tracing optimizations).
    # Enabled by default; set PYCHARM_WATCHPOINT_BOOST=0 to disable.
    _boost_code = '$encodedBoost'
    if _boost_code and sys.version_info >= (3, 12) and os.environ.get('PYCHARM_WATCHPOINT_BOOST', '1') != '0':
        try:
            _boost_mod = types.ModuleType('_pycharm_watchpoint_boost')
            sys.modules['_pycharm_watchpoint_boost'] = _boost_mod
            exec(base64.b64decode(_boost_code).decode('utf-8'), _boost_mod.__dict__)
            _boost_mod.install()
        except Exception as e:
            print(f"[WATCHPOINT-BOOST] Install failed (non-fatal): {e}", file=sys.stderr)
    elif _boost_code and os.environ.get('PYCHARM_WATCHPOINT_LOG') == '1':
        if os.environ.get('PYCHARM_WATCHPOINT_BOOST', '1') == '0':
            print(f"[WATCHPOINT-BOOST] Disabled via PYCHARM_WATCHPOINT_BOOST=0", file=sys.stderr)
        else:
            print(f"[WATCHPOINT-BOOST] Skipped – Python {sys.version_info.major}.{sys.version_info.minor} < 3.12 (PEP 669 not available)", file=sys.stderr)

    try:
        # Import the runtime package written next to this sitecustomize. Our temp
        # dir is prepended to PYTHONPATH (and is the dir this file loads from), so
        # it is on sys.path when site.py runs us. Importing the real package makes
        # WatchpointHit.__module__ == '_pycharm_watchpoint' (rebranded in __init__),
        # which is what the IDE-side exception breakpoint matches against. The
        # underscore-prefixed name avoids colliding with a user's own 'watchpoint'.
        import _pycharm_watchpoint
        print(f"[WATCHPOINT] Loaded in process {os.getpid()}", file=sys.stderr)
    except Exception as e:
        print(f"[WATCHPOINT] Boot failed: {e}", file=sys.stderr)

# Chain to the original sitecustomize (if any) that we're shadowing.
# Our temp dir is prepended to PYTHONPATH, so without this, the system's
# sitecustomize never runs – breaking path setup on some Python installs
# (e.g. Homebrew, conda, pyenv).
import importlib as _imp
_this_dir = os.path.dirname(os.path.abspath(__file__))
_orig_path = [p for p in sys.path if os.path.abspath(p) != _this_dir]
_saved_path = sys.path[:]
try:
    sys.path = _orig_path
    # Remove ourselves from sys.modules so the real one can load
    _us = sys.modules.pop('sitecustomize', None)
    try:
        _imp.import_module('sitecustomize')
    except ImportError:
        pass  # No system sitecustomize – that's fine
    finally:
        # Restore our module entry so Python doesn't try to re-import us
        if _us is not None:
            sys.modules['sitecustomize'] = _us
finally:
    sys.path = _saved_path
""".trimIndent())

            val envs = config.envs.toMutableMap()
            val existingPath = envs["PYTHONPATH"] ?: ""
            envs["PYTHONPATH"] = if (existingPath.isEmpty()) {
                tempDir.absolutePath
            } else {
                "${tempDir.absolutePath}${File.pathSeparator}$existingPath"
            }
            envs["PYCHARM_WATCHPOINT_ACTIVE"] = "1"
            envs["PYDEVD_USE_CYTHON"] = "NO"
            project.basePath?.let { envs["PYCHARM_WATCHPOINT_USER_ROOTS"] = it }


            config.envs = envs
            logger.warn("Injected watchpoint at: ${tempDir.absolutePath}")
        } catch (e: Exception) {
            logger.error("Failed to inject sitecustomize", e)
        }
    }

    override fun update(e: AnActionEvent) {
        val project = e.project ?: return
        val session = XDebuggerManager.getInstance(project).currentSession

        // `update()` runs on every toolbar refresh (sub-second cadence) and
        // overwrites whatever the plugin.xml `icon=` attribute installed, so
        // any custom icon has to be re-applied here too – otherwise the very
        // first update() call after IDE start replaces the spectacles glyph
        // with whichever fallback is hard-coded below.
        if (session != null) {
            e.presentation.text = "Restart with Watchpoint"
            e.presentation.icon = AllIcons.Actions.Restart
        } else {
            e.presentation.text = "Debug with Watchpoint"
            e.presentation.icon = WatchpointIcons.DebugWatch
        }
    }

    companion object {
        /**
         * The submodules that make up the `_pycharm_watchpoint` runtime package,
         * under `src/main/resources/python/_pycharm_watchpoint/`. Single source of
         * truth for both injection paths (sitecustomize + evaluator fallback) –
         * add a line here when you add a submodule. Order is irrelevant (the files
         * are written to disk and imported via normal Python machinery).
         */
        val MODULE_FILES: List<String> = listOf(
            "__init__.py",
            "constants.py",
            "hit.py",
            "helpers.py",
            "caller.py",
            "pydevd_pause.py",
            "watch_data.py",
            "containers.py",
            "classpatch.py",
            "registry.py",
        )
    }
}
