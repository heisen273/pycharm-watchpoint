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

intellijPlatform {
    buildSearchableOptions = false

    pluginConfiguration {
        ideaVersion {
            sinceBuild = "231"
            untilBuild = "261.*"
        }
    }

    pluginVerification {
        ides {
            recommended()
        }
    }
}