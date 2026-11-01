plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.lukatdamoves.karaokegen"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.lukatdamoves.karaokegen"
        minSdk = 26
        targetSdk = 35
        versionCode = 21
        // Full version (0.2.0): the editor + render, not just the trial
        // draft. Bump versionCode on EVERY release — the in-app
        // updater compares it against the APK asset on GitHub Releases.
        versionName = "0.2.4-trial"
    }

    // Release signing comes from CI-injected properties (see android/README.md).
    // The keystore lives in GitHub Actions secrets, never in the repo.
    // A release build without these properties fails at package time.
    signingConfigs {
        create("release") {
            storeFile = project.findProperty("android.injected.signing.store.file")
                ?.let { file(it as String) }
            storePassword = project.findProperty("android.injected.signing.store.password") as String?
            keyAlias = project.findProperty("android.injected.signing.key.alias") as String?
            keyPassword = project.findProperty("android.injected.signing.key.password") as String?
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        // LocalApiServer stamps the editor with the app version.
        buildConfig = true
    }
}

// ------------------------------------------------------------------
// Editor assets: single-sourced from the desktop editor (local_app/).
// The copy runs before every build; if the desktop editor files are
// missing, the build FAILS here instead of shipping a stale copy.
// ------------------------------------------------------------------
val editorAssetsDir = layout.buildDirectory.dir("generated/editorAssets")
val copyEditorAssets by tasks.registering(Copy::class) {
    val localApp = rootProject.projectDir.resolve("../local_app")
    val editorHtml = localApp.resolve("templates/editor.html")
    val editorStatic = localApp.resolve("static")
    val requiredStatic = listOf(
        "editor.js", "style.css", "wavesurfer.min.js", "regions.min.js", "favicon.ico"
    )
    doFirst {
        require(editorHtml.isFile) {
            "Desktop editor missing: $editorHtml — local_app/templates/editor.html must exist."
        }
        for (name in requiredStatic) {
            require(editorStatic.resolve(name).isFile) {
                "Desktop editor file missing: ${editorStatic.resolve(name)}."
            }
        }
    }
    from(editorHtml) { into("editor") }
    from(editorStatic) {
        include(requiredStatic)
        into("editor")
    }
    into(editorAssetsDir)
    doLast {
        val out = editorAssetsDir.get().asFile.resolve("editor")
        val missing = (listOf("editor.html") + requiredStatic)
            .filterNot { out.resolve(it).isFile }
        check(missing.isEmpty()) {
            "Editor assets missing after copy: ${missing.joinToString()}."
        }
    }
}
android {
    sourceSets {
        named("main") {
            assets.srcDir(editorAssetsDir)
        }
    }
}
tasks.named("preBuild").configure { dependsOn(copyEditorAssets) }

// The APK is renamed by the CI workflow after the build (see
// .github/workflows/build-android-trial.yml) to
// karaokegen-<versionName>-vc<versionCode>.apk, so the in-app updater
// can read the versionCode from the release asset name.

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Tiny localhost HTTP server for the editor (serves editor.html and
    // proxies /api/* to the Modal backend). Much lighter than Ktor.
    implementation("org.nanohttpd:nanohttpd:2.3.1")
    // YouTube audio extraction (JitPack — not on Maven Central).
    implementation("com.github.TeamNewPipe:NewPipeExtractor:v0.26.5")
}
