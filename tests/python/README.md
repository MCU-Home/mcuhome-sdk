# tests/python/

Python tests for the two packages under `mcuhome/`
(`mcuhome-model`, `mcuhome-compiler`), run with pytest:

```sh
pip install -e ./packaging/model -e ./packaging/compiler 'pytest>=8.0'
pytest
```

The installs are **editable** and the suite depends on it: the
whole-package invariants read the source of every module of every
package, and `conftest.package_modules()` refuses to run against modules
that are not the ones in this checkout.

**Why not `tests/`?** That directory holds Zephyr twister suites
(`testcase.yaml` per subdirectory) and twister recurses into whatever it
is pointed at, so a Python test tree living there would be walked by
twister and a C suite would be collected by pytest. Two runners, two
roots, no overlap. `pyproject.toml` pins pytest to this directory
(`testpaths`), so a bare `pytest` from the repo root does the right
thing.

These tests need neither Zephyr nor a west workspace and run in about a
second — they are the fast half of the strategy in
[`docs/design/builder-pipeline.md`](../docs/design/builder-pipeline.md)
§9.

| File | Covers |
|---|---|
| `test_pairing.py` | commissioning credentials: the CHIP vectors and the atomic Kconfig group |
| `test_registry.py` | the per-board update scheme and flash layout, and that no module branches on a board name |
| `test_generate.py` | stage 4: every generated artifact, byte-exact, plus its error paths |
| `test_workspace.py` | stage 5 on the host: workspace discovery, prerequisites, the sysbuild command, per-image artifacts and memory reports |
| `test_export.py` | the registry, exported as data for the dashboard: golden-tested byte for byte |
| `test_packaging.py` | the package layout: one distribution per subpackage, one version shared by both, and the two files outside Python that name an import path |
| `test_context.py` | the build context format and its normative, version-locked ID |
| `test_imageref.py` | parsing an external image reference: registry vs. path, port colon vs. tag colon |
| `test_jobs.py` | the compile-jobs heuristic and the precedence ladder above it |
| `test_ota.py` | the device version: the SemVer-to-SoftwareVersion mapping, and refusing one out of range |
| `test_userpaths.py` | per-user paths come from the stated environment, and that no other module reads process state instead |
| `test_abi.py` | the build environment's request/result ABI and the SDK entry point's own call into it, mostly through refusals |
| `test_buildenvironment.py` | parsing `build-environment.json` and `meta.json`, and what each refusal names |
| `test_container_closure.py` | the SDK entry point's import closure stays stdlib plus `mcuhome` |
| `test_env_image.py` | the thin build-environment image: what it may contain, and what its labels claim |
| `test_env_package.py` | the build-environment packages: determinism, the declared member set, and the pins that must agree |
| `test_builder_workspace.py` | the west workspace record a build environment writes: layer names, resolved commit and patch digest |
| `test_check_build_artifacts.py` | the CI artifact gate: a complete build passes, a broken one is named |
| `test_compare_firmware.py` | the firmware-comparison report: telling "one stamp" apart from "everywhere" |
| `test_sdk_archive.py` | the SDK package: the same bytes twice, the allowlist, and a real unpack through the orchestrator |
| `test_release.py` | the release act and release readiness: what a commit can already say, and what a release refuses |

The command-surface tests (exit codes, summary output, `-o json`
documents) live with the command: `tests/test_cli.py` in the
[mcu-home/mcuhome-cli](https://github.com/mcu-home/mcuhome-cli) repository, which is
where the `mcuhome` command itself moved. Driving a build environment —
docker included — is the workbench's job since the repository split, and
its own suite guards that; no module in this repository starts one.

**No build ever runs here, and no test touches the developer's own
signing key.** An autouse fixture in `conftest.py` points
`XDG_CONFIG_HOME` (and `HOME`) at `tmp_path`, because a real signing key
is a real, long-lived secret on the machine running the suite and a test
that reached it would either read a secret it has no business reading or
create one outside a temporary directory.

**No build ever runs here.** `test_workspace.py` (plus the `build` tests
in the mcuhome-cli repository) covers everything stage 5 decides *before*
the compiler starts: the command line, which prerequisite is missing, and
what the build log meant. The one test that needs a subprocess mocks it
directly, with `monkeypatch.setattr(subprocess, ...)`. Compiling a Matter
node takes minutes, a toolchain and a few gigabytes of image; that
belongs to twister and to hardware verification, not to a suite whose
whole value is running in a second.

## Golden files

`data/golden/` holds the byte-exact expected device model, devicetree
overlay, Kconfig fragment, application `CMakeLists.txt`,
`CHIPProjectConfig.h` wrapper and the three sysbuild artifacts
(`sysbuild.conf` plus the bootloader image's `.conf` and `.overlay`),
plus `registry.json`, the document `mcuhome.model.export` exports as its
contract with the dashboard. (The `main.yaml` JSON Schema is a workbench
export, tested in that repository.) The two generated C files are not
duplicated here: **the committed sample is the golden file** for those,
so `test_generate.py` compares fresh generator output against
`samples/matter-node/src/mcuhome_config.{c,h}` directly.

The device model golden itself is resolved from YAML in the workbench
repository and copied in from there. What this repository regenerates,
deliberately and never automatically, is everything it derives from that
model — from the repository root:

```sh
# the stage-4 artifacts, including the sample's C files
python - <<'PY'
import json
from pathlib import Path
from mcuhome.compiler.generate import write_tree
from mcuhome.model.model import DeviceModel

golden = Path("tests/python/data/golden")
model = DeviceModel.from_dict(
    json.loads((golden / "00-bmp180-two-endpoints.device-model.json").read_text()))
write_tree(model, out_dir=Path("/tmp/bmp180-node"), config_name="00-bmp180-two-endpoints.yaml")
PY
cp /tmp/bmp180-node/app/src/mcuhome_config.[ch] samples/matter-node/src/
cp /tmp/bmp180-node/app/prj.conf \
   tests/python/data/golden/00-bmp180-two-endpoints.prj.conf
cp /tmp/bmp180-node/app/CMakeLists.txt \
   tests/python/data/golden/00-bmp180-two-endpoints.CMakeLists.txt
cp /tmp/bmp180-node/app/boards/nrf7002dk_nrf5340_cpuapp.overlay \
   tests/python/data/golden/00-bmp180-two-endpoints.overlay
cp /tmp/bmp180-node/app/include/CHIPProjectConfig.h \
   tests/python/data/golden/00-bmp180-two-endpoints.CHIPProjectConfig.h
cp /tmp/bmp180-node/app/sysbuild.conf \
   tests/python/data/golden/00-bmp180-two-endpoints.sysbuild.conf
cp /tmp/bmp180-node/app/sysbuild/mcuboot.conf \
   tests/python/data/golden/00-bmp180-two-endpoints.mcuboot.conf
cp /tmp/bmp180-node/app/sysbuild/mcuboot.overlay \
   tests/python/data/golden/00-bmp180-two-endpoints.mcuboot.overlay

# the registry export — the builder version is stated as a placeholder,
# the same one test_export.py substitutes it for, so a release does not
# turn this golden red for a reason unrelated to its content
python - <<'PY'
from pathlib import Path
from mcuhome.model import export, __version__

data = export.to_json(export.registry_data()).replace(__version__, "0.1.0.dev0")
Path("tests/python/data/golden/registry.json").write_text(data)
PY
```

Regenerating the sample's C files is not optional bookkeeping: it is how
the runtime contract, the hardware-verified sample and the generator stay
in lockstep. Build the sample afterwards — the generated tables reference
the sensor's devicetree node, so a renamed peripheral breaks the board
overlay next to them.

Error **text** is asserted on purpose: validation messages are user
interface (builder-pipeline.md §9). If a message changes, that is a
deliberate UX change and the test is where it gets reviewed.

## Foreign vectors

`test_pairing.py` is the one suite whose expected values are not
MCUHome's own output: every SPAKE2+ verifier, QR payload and manual code
in it comes from the pinned connectedhomeip checkout, with the file it
was taken from named in a comment. Reproducing somebody else's numbers is
the only way to know that a code this builder prints is a code a real
controller accepts — a golden file of our own output would only prove we
are consistent. The last vector is stronger still: the two codes of the
nRF7002-DK that was commissioned into a production Home Assistant.
