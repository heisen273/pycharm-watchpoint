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
 * augments it with a sitecustomize.py that bootstraps watchpoint.py at interpreter
 * start-up, then launches a debug session on the clone. The original config is
 * left untouched so plain "Debug" still produces a clean session.
 *
 * Clones the selected run config so the original stays clean.
 */
class DebugWithWatchpointAction : AnAction(), DumbAware {
    private val logger = Logger.getInstance(DebugWithWatchpointAction::class.java)

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return

        // Defensive: scrub any leftover watchpoint environment from prior aborted runs.
        cleanAllConfigurations(project)

        val watchpointCode = loadWatchpointScript()
        if (watchpointCode == null) {
            logger.error("Failed to load watchpoint.py")
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

        injectViaSiteCustomize(project, clonedConfig, watchpointCode)

        val newSettings = runManager.createConfiguration(clonedConfig, selectedSettings.factory)
        newSettings.isTemporary = true

        WatchpointSessionManager.getInstance(project).startSession(watchpointCode)

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

        if (cleanedCount > 0) {
            logger.warn("Sanitized $cleanedCount configurations (removed stale watchpoint settings).")
        }
    }

    private fun loadWatchpointScript(): String? {
        return try {
            javaClass.getResourceAsStream("/python/watchpoint.py")
                ?.bufferedReader()
                ?.readText()
        } catch (e: Exception) {
            logger.error("Failed to read watchpoint.py", e)
            null
        }
    }

    /**
     * Writes a sitecustomize.py that decodes and execs watchpoint.py at interpreter
     * startup, then prepends its directory to PYTHONPATH and sets the activation env var.
     * Gated by PYCHARM_WATCHPOINT_ACTIVE so other interpreters that happen to inherit
     * the path don't accidentally boot watchpoint logic.
     */
    private fun injectViaSiteCustomize(
        project: Project,
        config: AbstractPythonRunConfiguration<*>,
        watchpointCode: String,
    ) {
        try {
            val tempDir = Files.createTempDirectory("pycharm_watchpoint_").toFile()
            val siteCustomize = File(tempDir, "sitecustomize.py")
            val encodedCode = Base64.getEncoder().encodeToString(watchpointCode.toByteArray())

            siteCustomize.writeText("""
import sys
import os
import base64
import types

if os.environ.get('PYCHARM_WATCHPOINT_ACTIVE') == '1':
    try:
        # Bootstrap as a real module so WatchpointHit.__module__ == '_pycharm_watchpoint',
        # which is what the IDE-side exception breakpoint matches against.
        # Using underscore-prefixed name to avoid colliding with user projects
        # that have their own top-level 'watchpoint' package.
        _wp_mod = types.ModuleType('_pycharm_watchpoint')
        sys.modules['_pycharm_watchpoint'] = _wp_mod
        _wp_code = base64.b64decode('$encodedCode').decode('utf-8')
        exec(_wp_code, _wp_mod.__dict__)
        print(f"[WATCHPOINT] Loaded in process {os.getpid()}", file=sys.stderr)
    except Exception as e:
        print(f"[WATCHPOINT] Boot failed: {e}", file=sys.stderr)
""".trimIndent())

            val envs = config.envs.toMutableMap()
            val existingPath = envs["PYTHONPATH"] ?: ""
            envs["PYTHONPATH"] = if (existingPath.isEmpty()) {
                tempDir.absolutePath
            } else {
                "${tempDir.absolutePath}${File.pathSeparator}$existingPath"
            }
            envs["PYCHARM_WATCHPOINT_ACTIVE"] = "1"
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
}
