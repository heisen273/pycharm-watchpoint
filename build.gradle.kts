import org.jetbrains.intellij.platform.gradle.TestFrameworkType

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
        pycharmCommunity("2025.1")
        bundledPlugin("PythonCore")

        pluginVerifier()
        testFramework(TestFrameworkType.Platform)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

kotlin {
    jvmToolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
        vendor.set(JvmVendorSpec.JETBRAINS)
    }
}

intellijPlatform {
    buildSearchableOptions = false

    pluginConfiguration {
        ideaVersion {
            sinceBuild = "243"
            untilBuild = "261.*"
        }
    }

    pluginVerification {
        ides {
            recommended()
        }
    }
}