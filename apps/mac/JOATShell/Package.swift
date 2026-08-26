// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "JOAT",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "JOAT", path: "Sources/JOAT")
    ]
)
