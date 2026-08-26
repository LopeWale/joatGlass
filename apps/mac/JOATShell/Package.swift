// swift-tools-version: 5.10
//
// NOT the shipping build. JOAT.app is built by scripts/build_app.sh via
// xcodegen + xcodebuild archive (see project.yml) — SwiftPM cannot produce
// an .app, an asset catalog, entitlements, or an archive to notarize.
// This manifest is kept only so `swift build` can type-check the sources
// quickly during development.
import PackageDescription

let package = Package(
    name: "JOAT",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "JOAT", path: "Sources/JOAT")
    ]
)
