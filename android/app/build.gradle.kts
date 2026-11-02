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
        versionCode = 33
        // v0.3.6: match desktop 2nd-voice checkbox + volume slider
        // (drop the BV dropdown).
        // Bump versionCode on EVERY release — the in-app updater
        // compares it against the APK asset on GitHub Releases.
        versionName = "0.3.6-trial"
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
    }
}

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
    // DNS-over-HTTPS (Cloudflare) with a system-DNS fallback — the
    // phone's carrier DNS drops "Unable to resolve host" blips that
    // made the draft poll hiccup. Same 4.12.0 as okhttp.
    implementation("com.squareup.okhttp3:okhttp-dnsoverhttps:4.12.0")
    // YouTube audio extraction (JitPack — not on Maven Central).
    implementation("com.github.TeamNewPipe:NewPipeExtractor:v0.26.5")
}
