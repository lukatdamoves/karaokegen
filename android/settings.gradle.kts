pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // NewPipeExtractor is published here, not on Maven Central.
        maven { url = uri("https://jitpack.io") }
    }
}
rootProject.name = "KaraokeGen"
include(":app")
