# Agent Instructions

## Architecture and Context
- **Upstream Origin:** This application is a slimmed-down version of [livekit-cli](https://github.com/livekit/livekit-cli/). Refer to the upstream repository for reference implementations or when diagnosing issues.
- **Dependencies:** When updating packages or adding features, refer to the upstream `livekit-cli` `go.mod` to align versions (especially for core LiveKit and Pion WebRTC libraries).

## Commands
- **Entrypoint:** The main CLI entrypoint is `cmd/publisher`. Run it via `go run ./cmd/publisher`.

## Build & Workflow
- **Build constraints:** Binaries must be built statically using `CGO_ENABLED=0` to match the `.gitlab-ci.yml` build matrix.
- **Workflow:** The repository follows standard Go 1.26 conventions. `go test ./...` and `go fmt ./...` work as expected. No complex task runners (like `Make` or `Task`) are used in the root.