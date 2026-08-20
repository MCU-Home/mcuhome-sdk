# scripts/

Development tooling and future custom west extension commands (registered
via `west-commands.yml` and the `self: west-commands:` key in
[west.yml](../west.yml) once the first command exists).

Current content:

| Entry | Purpose |
|---|---|
| `build_sdk_archive.py` | Builds `mcuhome-sdk-<version>.tar.zst` — the third artifact of a release (ADR 0017 §2) — from a **commit**, deterministically, with a `.sha256` sidecar and a static `index.json`. Runs in CI on `main`; the archive's contents are an explicit allowlist, argued entry by entry in the script's docstring |
| `compare_firmware.py` | Compares two builds of one device byte for byte and says **where** and **what** differs, decoding Intel HEX to the image it describes. A report and never a gate: CI builds the reference device on both architectures and prints this, so that "they differ" becomes "they differ in one build stamp" or "they differ everywhere" |
| `check_build_artifacts.py` | Asserts that a finished build left the flashable files behind and a well-formed §7.2.1 `build-report.json` beside them. The gate behind CI's Matter build job |
| `check_debug_output.py` | Lint against silently reduced diagnostics in config fragments ("Debug output is load-bearing"); a reduction passes only with a `# debug-output: approved <reason>` marker. Runs in CI |
| `pyshim/` | Stand-in for CHIP's `python_path` helper missing from the v1.5.1.0 release tarball (upstream candidate C1) — see `pyshim/README.md` for the `PYTHONPATH` requirement |
