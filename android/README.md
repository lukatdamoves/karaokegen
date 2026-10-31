# KaraokeGen — Android (trial)

Kotlin Android app for KaraokeGen. Trial v0.1: paste a YouTube link, the app
downloads the audio (NewPipe Extractor), then sends it to the Modal backend
as a draft — with lyrics, or transcribe-only when no lyrics are given.
The editor (WebView) and render come later; they are not in this trial.

The Android app lives in this same repo (`android/`) and is released from
it, next to the Windows `.exe`. Nothing in the desktop version was changed
for this app.

## Build

Local (needs JDK 17 + Android SDK 35):

```sh
cd android
./gradlew assembleDebug        # unsigned debug build, for development
./gradlew assembleRelease      # signed release build — needs the keystore (below)
```

CI: `.github/workflows/build-android-trial.yml` (manual trigger,
`workflow_dispatch`) builds the signed release APK and uploads it as an
artifact. It does not run on push.

## Release signing — the keystore

OTA updates only install over the existing app if every build is signed
with the **same** release key. Never ship a debug-signed APK as a release,
and never commit the keystore or its passwords to the repo.

Secrets (repo Settings → Secrets and variables → Actions):

| Secret | What it is |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | The release keystore file, base64-encoded |
| `ANDROID_KEYSTORE_PASSWORD` | Keystore password |
| `ANDROID_KEY_PASSWORD` | Key password (same value as the keystore password) |
| `ANDROID_KEY_ALIAS` | Key alias (`karaokegen`) |

The workflow decodes `ANDROID_KEYSTORE_BASE64` to a temporary file in the
runner and passes the passwords to Gradle as injected signing properties.
The keystore file is never committed and never appears in logs.

The person who created the keystore holds the passwords — keep them
somewhere safe (password manager). If they are lost, the keystore cannot
be used for local signing, but the workflow keeps working because the
secrets are stored in GitHub.

### Rotating the keystore

Only do this if the key is compromised or lost. A rotated key is a
*different* signature, so existing installs **cannot** update in place —
users must uninstall and install the new build once.

1. Generate a new keystore:
   `keytool -genkeypair -keystore karaokegen-release.jks -alias karaokegen -keyalg RSA -keysize 2048 -validity 10000`
2. Base64 it: `base64 -w0 karaokegen-release.jks`
3. Update the four secrets above with the new values.
4. Bump `versionCode` (below) and rebuild. Note the reinstall in the
   release notes.

## Over-the-air (OTA) updates

On launch (and from Settings → Check for updates) the app checks the
GitHub Releases API for `lukatdamoves/karaokegen` and looks for the newest
release carrying an APK asset named
`karaokegen-<version>-vc<versionCode>.apk`. If that versionCode is higher
than the installed one, the app shows an "Update available" card. Tapping
Update downloads the APK in-app (with progress) and hands it to the
system installer, which updates the app in place and keeps all data and
settings. The user confirms the install once — that single tap is
expected. No browser, no uninstall, no reinstall.

Rules:

- **Bump `versionCode` on every release** (`app/build.gradle.kts`).
  The updater compares versionCode only; a release with the same
  versionCode as the installed app is never offered.
- The APK file name is set by Gradle from `versionName`/`versionCode`
  (`karaokegen-<versionName>-vc<versionCode>.apk`); do not rename it by
  hand, or the updater cannot read the version.
- Releases without an APK asset (desktop-only releases with just the
  `.exe`) are skipped by the updater.
- If there is no newer release, no network, or no APK asset, the check
  fails silently — the app just works normally and says nothing.

## Backend setup (for testing the draft)

The draft needs a deployed Modal backend. This app cannot deploy one.
Paste the backend URL + API key in the app (Backend settings) — the same
URL and key from the desktop app's settings. Stored in SharedPreferences
(trial only; a production build should use EncryptedSharedPreferences).

## Layout

```
android/
├── settings.gradle.kts / build.gradle.kts / gradle.properties
├── gradle/wrapper/                     # Gradle wrapper (8.10.2)
├── app/
│   ├── build.gradle.kts               # app id, versions, signing, deps
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/com/lukatdamoves/karaokegen/
│       │   ├── MainActivity.kt        # YouTube -> audio -> draft
│       │   ├── YoutubeAudio.kt        # NewPipe Extractor resolve + download
│       │   ├── ModalClient.kt         # POST /draft, /transcribe, poll /jobs
│       │   ├── UpdateManager.kt       # OTA check, download, install
│       │   ├── SettingsActivity.kt    # backend URL + key, update check
│       │   └── SettingsStore.kt       # SharedPreferences
│       └── res/                       # layouts, strings, theme, file_paths
└── README.md                          # this file
```
