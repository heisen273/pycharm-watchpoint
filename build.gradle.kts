import org.jetbrains.intellij.platform.gradle.TestFrameworkType
import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    id("java")
    // PyCharm 2026.1 ships platform classes compiled with Kotlin 2.3.x (metadata 2.3.0).
    // A compiler reads metadata up to N+1, so 2.2.0 is the minimum that can consume them.
    // Pinning to 2.2.0 (the floor) instead of 2.3.x keeps language-level changes small.
    id("org.jetbrains.kotlin.jvm") version "2.2.0"
    id("org.jetbrains.intellij.platform") version "2.16.0"   // was 2.2.1
}
group = "com.pythonwatchpoint"
version = "1.0.0"

repositories {
    mavenCentral()

    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        pycharm("2026.1")
//        pycharmCommunity("2025.1")
        bundledPlugin("PythonCore")

        pluginVerifier()
        testFramework(TestFrameworkType.Platform)
    }
}

kotlin {
    // Build with JBR 21 (matches org.gradle.java.home in gradle.properties).
    jvmToolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
        vendor.set(JvmVendorSpec.JETBRAINS)
    }
}

// ---- JVM bytecode target: 17 --------------------------------------------------
// JVM 17 class files (version 61.0) load in PyCharm 2023.x (JBR 17) through
// 2026.x (JBR 21) – forward-compatible with every supported build.
//
// We configure BOTH tasks here because:
//  - jvmToolchain(21) overrides the kotlin {} compilerOptions block in KGP 2.x.
//  - jvmToolchain(21) also drives compileJava, ignoring java { targetCompatibility }.
//  - KGP 2.x validates that compileJava and compileKotlin agree; if they differ
//    it refuses to build ("Inconsistent JVM-target compatibility"). Configuring
//    both tasks in the same place keeps them in sync.
// -------------------------------------------------------------------------------

tasks.withType<KotlinCompile>().configureEach {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

// options.release hard-pins the javac output regardless of toolchain JDK version.
tasks.withType<JavaCompile>().configureEach {
    options.release.set(17)
}

// ── Python watchpoint minification ─────────────────────────────────────────
// Runs pyminify on watchpoint.py before packaging so the plain source never
// ends up in the distribution JAR. Docstrings, comments, and local variable
// names are all stripped; public names (_pycharm_watch_at, WatchpointHit,
// etc.) are intentionally preserved because the Kotlin side calls them by
// string via the pydevd evaluator.
//
// Configure `pyminifyPath` in gradle.properties (or pass -PpyminifyPath=...)
// to point at the pyminify binary. Defaults to "pyminify" on the system PATH.

val pyminifyPath: String = findProperty("pyminifyPath")?.toString() ?: "pyminify"

val minifyWatchpointFile = layout.buildDirectory.file("obfuscated/python/watchpoint.py")

val minifyWatchpointPy by tasks.registering(Exec::class) {
    description = "Minifies watchpoint.py via pyminify before it is packaged into the JAR."
    group = "build"

    val sourceFile = layout.projectDirectory.file("src/main/resources/python/watchpoint.py")
    inputs.file(sourceFile)
    outputs.file(minifyWatchpointFile)

    doFirst {
        minifyWatchpointFile.get().asFile.parentFile.mkdirs()
    }

    commandLine(
        pyminifyPath,
        "--remove-literal-statements",   // strips all docstrings
        "--output", minifyWatchpointFile.get().asFile.absolutePath,
        sourceFile.asFile.absolutePath,
    )
}

// Swap out the plain watchpoint.py for the minified copy in processResources.
tasks.named<ProcessResources>("processResources") {
    dependsOn(minifyWatchpointPy)
    exclude("python/watchpoint.py")
    from(minifyWatchpointFile.map { it.asFile.parentFile }) {
        into("python")
        include("watchpoint.py")
    }
}

// ──────────────────────────────────────────────────────────────────────────

intellijPlatform {
    buildSearchableOptions = false

    pluginConfiguration {
        ideaVersion {
            sinceBuild = "233"
            untilBuild = "261.*"
        }
    }

    pluginVerification {
        ides {
            recommended()
        }
    }
}