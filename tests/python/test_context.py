# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context format and its normative ID.

The model half of the subject: :mod:`mcuhome.model.context` is the
format, the canonical encoding and the ID rule — the vocabulary a build
server recomputes an ID with while carrying no build logic at all. The
directory that rule is applied to is
:mod:`mcuhome.workbench.contextdir`, and it is tested next door in
``test_context_workbench.py``.

The context ID rule is locked with ``context`` format version 4 and can
never change afterwards — every archived context, every artifact
attribution and every server-side integrity check depends on the same
inputs hashing to the same ID forever. That makes :data:`GOLDEN_ID` the
contract of this file, not a regression convenience: if it ever fails,
the fix is in the code, never in the constant.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_ID_VECTORS,
    CONTEXT_VERSION,
    DEVELOPER_ENVIRONMENT,
    MANIFEST_FILE,
    ContextFile,
    ContextManifest,
    ContextRequest,
    DeveloperEnvironment,
    EnvironmentPin,
    GeneratorEntry,
    PackagePin,
    SdkPin,
    canonical_json,
    context_id,
    format_generator_chain,
    parse_environment,
    parse_generator_chain,
    validate_manifest,
    validate_request,
    vector_id,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.toolchain import line_of, normalize_release, satisfies_line

# The fixed synthetic inputs of the golden vector.
SDK_SHA = "cd" * 32
ENVIRONMENT = EnvironmentPin(
    workspace=PackagePin(name="mcuhome-build-workspace", version="2.4.0", sha256="4d" * 32),
    tools=PackagePin(name="mcuhome-build-tools", version="1.2.0", sha256="4e" * 32),
)
BOARD = "nrf7002dk/nrf5340/cpuapp"
FILES = (
    ContextFile(path="model/device-model.json", sha256="11" * 32),
    ContextFile(path="patches/zephyr/0001-fix.patch", sha256="22" * 32),
)

#: The golden vector: the inputs above hash to exactly this ID, in this
#: builder and in every builder that will ever exist. NEVER update this
#: constant — a change here is a change to a frozen contract, and the
#: bug is in the code that made it necessary.
#:
#: It moved with the bump to context format 4, which is the only thing
#: that may move it: the environment member stopped being one digest and
#: became two ``(name, version, sha256)`` triples, so the same files and
#: the same SDK hash to a different number under the new format version.
#: A frozen rule is frozen per format version, and version 3 no longer
#: exists to disagree with this. It is the "model and one patch" vector.
GOLDEN_ID = "sha256:40c5066b0891e91aedd299cfd21cbb81b12e028228f7b0007e7cf92f8522bca7"


# --------------------------------------------------------------------------
# Canonical JSON — the encoding under the hash
# --------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_uses_minimal_separators() -> None:
    value = {"b": "1", "a": {"d": "2", "c": "3"}, "list": ["x", "y"]}
    assert canonical_json(value) == '{"a":{"c":"3","d":"2"},"b":"1","list":["x","y"]}'


def test_canonical_json_emits_non_ascii_literally() -> None:
    """RFC 8785 forbids \\u escapes for characters that need none."""
    assert canonical_json({"board": "nrf–ü"}) == '{"board":"nrf–ü"}'
    assert canonical_json({"a": "ü"}).encode("utf-8") == b'{"a":"\xc3\xbc"}'


def test_canonical_json_escapes_strings_the_ecmascript_way() -> None:
    """Two-character escapes where they exist, lowercase \\u00xx otherwise."""
    assert canonical_json({"a": 'x"\\\n\t\x1f'}) == '{"a":"x\\"\\\\\\n\\t\\u001f"}'


# --------------------------------------------------------------------------
# The context ID — the normative rule, frozen
# --------------------------------------------------------------------------


def test_the_golden_vector_never_changes() -> None:
    """The regression anchor of the whole format. See GOLDEN_ID."""
    computed = context_id(
        sdk_sha256=SDK_SHA,
        environment=ENVIRONMENT,
        board=BOARD,
        files=FILES,
    )
    assert computed == GOLDEN_ID


@pytest.mark.parametrize("vector", CONTEXT_ID_VECTORS, ids=lambda vector: vector["name"])
def test_the_conformance_vectors_hold(vector) -> None:
    """The suite a *second* implementation is checked against.

    The golden vector above proves this builder does not drift. It cannot
    prove anything about the build server, which ADR 0019 §8 obliges to
    recompute the same ID from the bytes it received — and which, per ADR
    0020 decision 4, is entitled to do so with nothing but the model
    package. :data:`CONTEXT_ID_VECTORS` ships inside that package so the
    obligation is checkable rather than asserted, and this test is the
    Python side running it.

    A failure here is never fixed in the data: version 3's vectors are
    frozen exactly as :data:`GOLDEN_ID` is.
    """
    assert vector_id(vector) == vector["id"]


def test_the_golden_vector_is_one_of_the_conformance_vectors() -> None:
    """Two frozen constants that disagree would be worse than one.

    The vector this file has pinned since the format was written is in
    the package's own suite, so a second implementation is checked
    against the same value the test suite is — not a second one that
    happens to look like it.
    """
    assert GOLDEN_ID in {vector["id"] for vector in CONTEXT_ID_VECTORS}


def test_an_implementation_sorting_by_utf16_code_units_fails_the_suite() -> None:
    """The suite's job, checked against the mistake it exists to catch.

    The hashed document *is* RFC 8785, and RFC 8785 orders object keys by
    UTF-16 code units. An implementation that reached for its JCS
    library's comparator for the ``files`` array would sort a different
    way — the two orders agree across the whole BMP and disagree the
    moment an astral path meets a BMP one — and would then compute a
    different context ID forever, for a context nobody could tell apart
    from a correct one.

    So: replay every vector with a UTF-16 sort in place of the code-point
    sort, and *some* vector must come out wrong. All but one do not (they
    are ASCII, or single-file, or below U+D800); that one is there so
    this assertion has something to stand on. Deleting it makes
    this test fail, which is the whole point of writing it as a check on
    the table rather than as a sixth assertion inside it.
    """

    def utf16_id(vector: dict) -> str:
        inputs = vector["inputs"]
        document = {
            "files": [
                {"path": path, "sha256": sha256}
                for path, sha256 in sorted(
                    inputs["files"], key=lambda entry: entry[0].encode("utf-16-be")
                )
            ],
            "sdk": {"sha256": inputs["sdk_sha256"]},
            "target": {"board": inputs["board"]},
            "build_environment": (
                inputs["environment"]
                if isinstance(inputs["environment"], str)
                else {
                    half: {
                        "name": entry["name"],
                        "sha256": entry["sha256"],
                        "version": entry["version"],
                    }
                    for half, entry in inputs["environment"].items()
                }
            ),
        }
        return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()

    wrong = [vector["name"] for vector in CONTEXT_ID_VECTORS if utf16_id(vector) != vector["id"]]
    assert wrong, "no vector distinguishes code-point order from UTF-16 code-unit order"


def test_the_golden_vectors_canonical_form_never_changes() -> None:
    """The exact bytes under the hash, spelled out — nesting, order, all."""
    expected = (
        '{"build_environment":'
        '{"tools":{"name":"'
        + ENVIRONMENT.tools.name
        + '","sha256":"'
        + ENVIRONMENT.tools.sha256
        + '","version":"'
        + ENVIRONMENT.tools.version
        + '"},'
        '"workspace":{"name":"'
        + ENVIRONMENT.workspace.name
        + '","sha256":"'
        + ENVIRONMENT.workspace.sha256
        + '","version":"'
        + ENVIRONMENT.workspace.version
        + '"}},'
        '"files":['
        '{"path":"model/device-model.json","sha256":"' + "11" * 32 + '"},'
        '{"path":"patches/zephyr/0001-fix.patch","sha256":"' + "22" * 32 + '"}],'
        '"sdk":{"sha256":"' + SDK_SHA + '"},'
        '"target":{"board":"' + BOARD + '"}}'
    )
    assert "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest() == GOLDEN_ID


def test_the_order_files_are_given_in_does_not_matter() -> None:
    """The sort is part of the rule: the list is a set with an encoding."""
    computed = context_id(
        sdk_sha256=SDK_SHA,
        environment=ENVIRONMENT,
        board=BOARD,
        files=reversed(FILES),
    )
    assert computed == GOLDEN_ID


def test_every_hashed_field_changes_the_id() -> None:
    variants = [
        {"sdk_sha256": "dc" * 32},
        # Each of the six members of the two environment triples on its own.
        {
            "environment": replace(
                ENVIRONMENT, workspace=replace(ENVIRONMENT.workspace, sha256="5e" * 32)
            )
        },
        {
            "environment": replace(
                ENVIRONMENT, workspace=replace(ENVIRONMENT.workspace, name="other-workspace")
            )
        },
        {
            "environment": replace(
                ENVIRONMENT, workspace=replace(ENVIRONMENT.workspace, version="9.9.9")
            )
        },
        {"environment": replace(ENVIRONMENT, tools=replace(ENVIRONMENT.tools, sha256="6f" * 32))},
        {
            "environment": replace(
                ENVIRONMENT,
                tools=replace(ENVIRONMENT.tools, name="mcuhome-build-tools_linux-amd64"),
            )
        },
        {"environment": replace(ENVIRONMENT, tools=replace(ENVIRONMENT.tools, version="9.9.9"))},
        {"board": "nrf52840dk/nrf52840"},
        # A file's content, a file's path, one file more, one file less.
        {"files": (FILES[0], replace(FILES[1], sha256="33" * 32))},
        {"files": (FILES[0], replace(FILES[1], path="patches/sdk/0001-fix.patch"))},
        {"files": (*FILES, ContextFile(path="patches/sdk/0001-more.patch", sha256="44" * 32))},
        {"files": FILES[:1]},
    ]
    ids = {
        context_id(
            **{
                "sdk_sha256": SDK_SHA,
                "environment": ENVIRONMENT,
                "board": BOARD,
                "files": FILES,
                **variant,
            }
        )
        for variant in variants
    }
    assert GOLDEN_ID not in ids
    assert len(ids) == len(variants), "two different inputs collided on one ID"


def test_a_duplicate_path_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        context_id(
            sdk_sha256=SDK_SHA,
            environment=ENVIRONMENT,
            board=BOARD,
            files=(FILES[0], replace(FILES[0], sha256="33" * 32)),
        )
    assert "twice" in caught.value.message


def test_a_malformed_file_hash_is_refused() -> None:
    with pytest.raises(BuildError):
        context_id(
            sdk_sha256=SDK_SHA,
            environment=ENVIRONMENT,
            board=BOARD,
            files=(replace(FILES[0], sha256="not-a-hash"),),
        )


def test_a_missing_board_is_refused() -> None:
    with pytest.raises(BuildError):
        context_id(
            sdk_sha256=SDK_SHA,
            environment=ENVIRONMENT,
            board="  ",
            files=FILES,
        )


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "patches/../escape.patch", "patches\\zephyr\\0001.patch", "", "a//b"],
)
def test_an_unusable_path_is_refused(path: str) -> None:
    with pytest.raises(BuildError):
        context_id(
            sdk_sha256=SDK_SHA,
            environment=ENVIRONMENT,
            board=BOARD,
            files=(ContextFile(path=path, sha256="11" * 32),),
        )


@pytest.mark.parametrize("path", [MANIFEST_FILE, CONTEXT_FILE, f"{BACKEND_DIR}/command.json"])
def test_the_integrity_list_may_not_name_what_is_not_content(path: str) -> None:
    """Both context documents and the backend directory stay out of the ID.

    ``context.yaml`` is the one that went unenforced for a while: §3.2
    excludes it "as a statement about the hash rather than about layout"
    — its never-hashed fields (constraint, url, created) would leak
    into an identity §6 computes from resolved values alone — but the
    shared vocabulary accepted it anyway, leaving the exclusion to every
    caller separately. The build server recomputes IDs from received
    bytes (ADR 0019 §8), so the one implementation both sides share must
    be the place that refuses.
    """
    with pytest.raises(BuildError) as caught:
        context_id(
            sdk_sha256=SDK_SHA,
            environment=ENVIRONMENT,
            board=BOARD,
            files=(ContextFile(path=path, sha256="11" * 32),),
        )
    assert "must not name" in caught.value.message


# --------------------------------------------------------------------------
# Context format 4: the pinned package set and identity
# --------------------------------------------------------------------------


def test_the_format_version_is_four() -> None:
    """Version 3 is gone rather than supported alongside this one.

    Pinned as a number because everything else in this file is written
    against it: the golden ID, the vectors, and the refusal a document of
    another version gets. Nothing is published, so the bump cost nothing
    — and this assertion is what makes the next bump a deliberate act.
    """
    assert CONTEXT_VERSION == 4


@pytest.mark.parametrize(
    ("version", "line", "expected"),
    [
        # A line is a prefix, component by component.
        ("4.4", "4.4", True),
        ("4.4.0", "4.4", True),
        ("4.4.12", "4.4", True),
        ("4.4.0", "4", True),
        ("4.4.0", "4.4.0", True),
        # …and only component by component: 4.40 merely starts with the
        # same digits, and a longer line is not satisfied by a shorter
        # release.
        ("4.40.0", "4.4", False),
        ("4.5.0", "4.4", False),
        ("4.4", "4.4.0", False),
        # A pre-release satisfies no line, including its own (§2.1.1:
        # "not ordered at all" — and a line is a range).
        ("4.5.0-rc1", "4.5", False),
        ("4.5.0-rc1", "4.5.0", False),
        # Absence, and nonsense, are never read as compatible.
        ("", "4.4", False),
        ("v4.4.0", "4.4", False),
        ("latest", "4.4", False),
        ("4.4.0", "", False),
    ],
)
def test_which_container_serves_a_line(version: str, line: str, expected: bool) -> None:
    """The match both backends make, in ``mcuhome-model`` so they make one.

    The local build method asks it of the image on a developer's host and
    the build server asks it of every image in its inventory; two
    spellings of "this container serves 4.4" is how the two start
    disagreeing about one container.
    """
    assert satisfies_line(version, line=line) is expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("4.4", "4.4"),
        ("4.4.0", "4.4"),
        ("4.4.12", "4.4"),
        ("4.5.0", "4.5"),
        # A one-component release is its own line; there is no second
        # component to reduce to.
        ("4", "4"),
        # A pre-release serves no line, so it *is* no line — which is the
        # whole reason a backend must not report one as "available".
        ("4.5.0-rc1", None),
        # …and neither is anything that is not a release at all.
        ("", None),
        ("v4.4.0", None),
        ("latest", None),
    ],
)
def test_the_line_a_release_belongs_to(version: str, expected: str | None) -> None:
    """``satisfies_line``'s inverse, for telling a client what is served.

    A backend that cannot answer a context reports what it *could*
    answer, and both ADR 0019 and the build server's error registry call
    those values "the lines available" — while the values they are read
    off, ``org.mcuhome.zephyr`` labels, are releases. The reduction lives
    beside the match so that "serves 4.4" and "offers 4.4" cannot drift
    apart.
    """
    assert line_of(version) == expected


@pytest.mark.parametrize("version", ["4.4", "4.4.0", "4.4.12", "4", "4.5.0"])
def test_a_reported_line_is_always_one_the_release_satisfies(version: str) -> None:
    """The invariant that makes the reduction usable rather than merely
    tidy: a client that echoes back a reported line gets an image."""
    line = line_of(version)
    assert line is not None
    assert satisfies_line(version, line=line)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # West's own spelling: the one case this exists for.
        ("v4.4.0", "4.4.0"),
        # No leading v — nothing to strip, unchanged.
        ("4.4.0", "4.4.0"),
        # Exactly one v, however many there are: not west's doing beyond
        # the first, and this is not a loop.
        ("vv4.4.0", "v4.4.0"),
        # The whole string can be the v.
        ("v", ""),
        # Absence stays absence.
        ("", ""),
    ],
)
def test_normalize_release_strips_one_leading_v(version: str, expected: str) -> None:
    """West states every pinned revision with a ``v`` no release grammar
    carries; this is the one place that strip happens, for every reader
    of a ``describe`` answer or a west revision alike."""
    assert normalize_release(version) == expected


# --------------------------------------------------------------------------
# The pin itself
# --------------------------------------------------------------------------


#: A pin whose tools entry names one platform's package rather than the
#: family — the second of the two shapes §4 allows.
CONCRETE = replace(
    ENVIRONMENT,
    tools=replace(ENVIRONMENT.tools, name="mcuhome-build-tools_linux-amd64"),
)


@pytest.mark.parametrize(
    "broken",
    [
        # The name: empty, uppercase, a second underscore, a slash.
        PackagePin(name="", version="1.0", sha256="ab" * 32),
        PackagePin(name="MCUHome-Build-Tools", version="1.0", sha256="ab" * 32),
        PackagePin(name="a_b_c", version="1.0", sha256="ab" * 32),
        PackagePin(name="build-tools/mcuhome-build-tools", version="1.0", sha256="ab" * 32),
        PackagePin(name="-leading-dash", version="1.0", sha256="ab" * 32),
        # The version: empty, and something that is not one.
        PackagePin(name="mcuhome-build-tools", version="", sha256="ab" * 32),
        PackagePin(name="mcuhome-build-tools", version="1 0", sha256="ab" * 32),
        # The hash: uppercase, short, prefixed, not a string.
        PackagePin(name="mcuhome-build-tools", version="1.0", sha256="AB" * 32),
        PackagePin(name="mcuhome-build-tools", version="1.0", sha256="ab" * 16),
        PackagePin(name="mcuhome-build-tools", version="1.0", sha256="sha256:" + "ab" * 32),
        PackagePin(name="mcuhome-build-tools", version="1.0", sha256=None),
    ],
)
def test_an_environment_entry_that_is_not_a_pin_is_refused(broken) -> None:
    """All three members have exactly one spelling, and all three are hashed.

    Uppercase hex is refused rather than normalized for the reason every
    other identity input is: two spellings of one value are two values as
    far as a hash is concerned. The **name** is checked as strictly,
    because a family name and a per-platform name are different pins with
    different meanings, and one of them resolving to the other would let
    two builds share an identity.
    """
    with pytest.raises(BuildError):
        context_id(
            sdk_sha256=SDK_SHA,
            environment=replace(ENVIRONMENT, tools=broken),
            board=BOARD,
            files=FILES,
        )


def test_a_manifest_is_only_valid_when_both_its_packages_are_pinned() -> None:
    """The check that used to be the image digest's is the two triples'."""
    unpinned = ContextManifest(
        sdk=SdkPin(constraint="", version="0.1.0", url="", sha256=SDK_SHA),
        build_environment=replace(ENVIRONMENT, workspace=replace(ENVIRONMENT.workspace, sha256="")),
        board=BOARD,
        files=FILES,
        id=GOLDEN_ID,
    )
    with pytest.raises(BuildError):
        validate_manifest(unpinned)


def test_a_manifest_and_its_request_round_trip_through_their_documents() -> None:
    """Both halves carry the pin, and both read back exactly what was written.

    The one difference between them is the location hint: the request
    carries it, the lock does not — where the bytes were found is not part
    of what is in the context.
    """
    hinted = replace(
        ENVIRONMENT,
        workspace=replace(ENVIRONMENT.workspace, url="https://example.invalid/w.tar.zst"),
    )
    sdk = SdkPin(constraint="~=0.1", version="0.1.0", url="", sha256=SDK_SHA)
    request = ContextRequest(
        sdk=sdk, build_environment=hinted, board=BOARD, created="2026-08-18T00:00:00Z"
    )
    assert ContextRequest.from_dict(request.to_dict()) == request
    assert request.to_dict()["build_environment"]["workspace"] == {
        "name": hinted.workspace.name,
        "version": hinted.workspace.version,
        "sha256": hinted.workspace.sha256,
        "url": hinted.workspace.url,
    }

    manifest = ContextManifest(
        sdk=sdk,
        build_environment=ENVIRONMENT,
        board=BOARD,
        files=FILES,
        id=context_id(
            sdk_sha256=SDK_SHA,
            environment=ENVIRONMENT,
            board=BOARD,
            files=FILES,
        ),
    )
    assert ContextManifest.from_dict(manifest.to_dict()) == manifest
    assert manifest.compute_id() == manifest.id
    assert "url" not in manifest.to_dict()["build_environment"]["tools"]
    assert manifest.compute_id() == GOLDEN_ID
    validate_manifest(manifest)


def test_a_developer_context_round_trips_as_one_word() -> None:
    """The second form of ``build_environment``, through both documents.

    A build against a workspace somebody maintains themselves has no
    package set to name, so the field is the word — written as the word,
    read back as the object that means it, in the request and in the lock
    alike.
    """
    sdk = SdkPin(constraint="", version="", url="", sha256="")
    request = ContextRequest(
        sdk=sdk,
        build_environment=DeveloperEnvironment(),
        board=BOARD,
        created="2026-09-07T00:00:00Z",
    )
    assert request.to_dict()["build_environment"] == DEVELOPER_ENVIRONMENT
    assert ContextRequest.from_dict(request.to_dict()) == request

    manifest = ContextManifest(
        sdk=sdk,
        build_environment=DeveloperEnvironment(),
        board=BOARD,
        files=FILES,
        id=context_id(sdk_sha256="", environment=DeveloperEnvironment(), board=BOARD, files=FILES),
    )
    assert manifest.to_dict()["build_environment"] == DEVELOPER_ENVIRONMENT
    assert ContextManifest.from_dict(manifest.to_dict()) == manifest
    assert manifest.compute_id() == manifest.id
    validate_manifest(manifest)


def test_the_developer_form_hashes_the_word_and_an_empty_sdk_hash() -> None:
    """The exact bytes under the hash of the second form, spelled out.

    The member is the JSON string, not an object with empty members and
    not a member left out, and ``sdk`` keeps its shape around an empty
    value — a reader of another implementation has to be able to build
    this document from the specification alone.
    """
    files = (ContextFile(path="model/device-model.json", sha256="c" * 64),)
    expected = (
        '{"build_environment":"developer",'
        '"files":[{"path":"model/device-model.json","sha256":"' + "c" * 64 + '"}],'
        '"sdk":{"sha256":""},'
        '"target":{"board":"nrf52840dk/nrf52840"}}'
    )
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert (
        context_id(
            sdk_sha256="",
            environment=DeveloperEnvironment(),
            board="nrf52840dk/nrf52840",
            files=files,
        )
        == f"sha256:{digest}"
    )
    # The same inputs in the package form are a different context, which
    # is the point of hashing the word at all.
    assert (
        context_id(
            sdk_sha256="b" * 64,
            environment=ENVIRONMENT,
            board="nrf52840dk/nrf52840",
            files=files,
        )
        != f"sha256:{digest}"
    )


def test_the_two_forms_cannot_be_mixed() -> None:
    """One form or the other: the SDK hash travels with the environment.

    A document that names no environment and pins an SDK, or names the
    packages and pins nothing, would be half a claim — and half a claim
    about the bytes a firmware was built from is worse than either whole
    one.
    """
    with pytest.raises(BuildError, match="empty package hash"):
        context_id(sdk_sha256=SDK_SHA, environment=DeveloperEnvironment(), board=BOARD, files=FILES)
    with pytest.raises(BuildError, match="not a SHA-256 hash"):
        context_id(sdk_sha256="", environment=ENVIRONMENT, board=BOARD, files=FILES)


@pytest.mark.parametrize("written", ["dev", "", "DEVELOPER", None, [], 4])
def test_only_the_one_word_is_the_developer_form(written) -> None:
    """The form is one literal string, and everything else is refused.

    Not only the wrong word: a ``build_environment:`` with nothing after
    it reads as YAML's null, and a reader that took "not a mapping" for
    "the developer form" would turn a truncated document into a context
    that builds.
    """
    assert parse_environment(DEVELOPER_ENVIRONMENT) == DeveloperEnvironment()
    assert parse_environment(ENVIRONMENT.to_dict()) == ENVIRONMENT
    with pytest.raises(BuildError):
        parse_environment(written)


def test_a_developer_environment_has_no_hints_to_drop() -> None:
    """What the lock does to a request's environment, in the second form.

    The lock is written from the request with the location hints dropped,
    so every form has to survive that — and this one has nothing to drop
    and must come back as itself rather than as nothing.
    """
    assert DeveloperEnvironment().without_urls() == DeveloperEnvironment()
    assert DeveloperEnvironment().to_dict(url=False) == DEVELOPER_ENVIRONMENT
    assert DeveloperEnvironment().described()


def test_a_request_is_checked_as_strictly_as_a_lock() -> None:
    """The pair rule holds for the request document too.

    A reader that checked only the environment would accept a document
    half-claiming to be pinned — the word and a real SDK hash — and every
    party downstream would then read one half of it and believe the
    other.
    """
    mixed = ContextRequest(
        sdk=SdkPin(constraint="", version="", url="", sha256=SDK_SHA),
        build_environment=DeveloperEnvironment(),
        board=BOARD,
        created="2026-09-07T00:00:00Z",
    )
    with pytest.raises(BuildError, match="empty package hash"):
        validate_request(mixed)
    with pytest.raises(BuildError, match="not a SHA-256 hash"):
        validate_request(
            replace(mixed, build_environment=ENVIRONMENT, sdk=replace(mixed.sdk, sha256=""))
        )
    validate_request(replace(mixed, sdk=replace(mixed.sdk, sha256="")))


def test_a_reader_of_package_pins_alone_refuses_the_developer_form() -> None:
    """The developer form is opt-in for a reader, and this is how.

    A party that can only act on a package set — a build server resolves
    an environment from one — reads the pin itself instead of the field,
    and a developer context is then refused rather than half-understood.
    """
    with pytest.raises(BuildError, match="names no build environment"):
        EnvironmentPin.from_dict(DEVELOPER_ENVIRONMENT)


def test_a_request_that_carried_hints_locks_without_them() -> None:
    """``without_urls`` is what makes a written lock equal a read-back one."""
    hinted = replace(
        ENVIRONMENT,
        workspace=replace(ENVIRONMENT.workspace, url="https://example.invalid/w.tar.zst"),
        tools=replace(ENVIRONMENT.tools, url="https://example.invalid/t/"),
    )
    assert hinted.without_urls() == ENVIRONMENT
    # And the hint never reaches the identity either way.
    assert context_id(sdk_sha256=SDK_SHA, environment=hinted, board=BOARD, files=FILES) == GOLDEN_ID


def test_a_meta_pin_and_a_concrete_pin_are_two_contexts() -> None:
    """The reason ``name`` is inside the hashed triple.

    A family pin resolves per platform; a per-platform pin does not. Same
    version, same bytes on this host, two different statements about what
    may run the context — so two identities. An implementation that
    hashed only version and hash would give them one.
    """
    assert context_id(
        sdk_sha256=SDK_SHA, environment=ENVIRONMENT, board=BOARD, files=FILES
    ) != context_id(sdk_sha256=SDK_SHA, environment=CONCRETE, board=BOARD, files=FILES)


def test_two_contexts_differing_only_in_their_environment_are_two_contexts() -> None:
    """The whole reason the pin is hashed.

    Under the format this replaced, the same sources built in two
    different environments produced one identity — which made the
    document that names a build name two of them.
    """
    other = replace(ENVIRONMENT, tools=replace(ENVIRONMENT.tools, sha256="cd" * 32))
    assert context_id(
        sdk_sha256=SDK_SHA,
        environment=ENVIRONMENT,
        board=BOARD,
        files=FILES,
    ) != context_id(
        sdk_sha256=SDK_SHA,
        environment=other,
        board=BOARD,
        files=FILES,
    )


def test_a_malformed_environment_document_is_refused_on_read() -> None:
    """A reader refuses a document that does not describe a package set."""
    for broken in ({}, {"workspace": "x"}, {"workspace": {}, "tools": {}}, "a string", None):
        with pytest.raises((BuildError, KeyError, TypeError)):
            EnvironmentPin.from_dict(broken)


# --------------------------------------------------------------------------
# The generator declaration
# --------------------------------------------------------------------------


def test_a_generator_chain_round_trips() -> None:
    entries = (
        GeneratorEntry("custom-tool", "4.2.3"),
        GeneratorEntry("mcuhome-workbench", "0.1.0.dev0"),
    )
    chain = format_generator_chain(entries)
    assert chain == "custom-tool:4.2.3;mcuhome-workbench:0.1.0.dev0"
    assert parse_generator_chain(chain) == entries


def test_the_leftmost_entry_is_the_tool_that_wrote_the_context_last() -> None:
    """The whole ordering rule, as the one assertion that can enforce it.

    A build environment's default check believes this entry and no other,
    so a chain read the other way round would hand the decision to the
    tool that merely created the context and let anything downstream
    modify it unchecked.
    """
    created = parse_generator_chain("mcuhome-workbench:1.0.0")
    modified = format_generator_chain((GeneratorEntry("custom-tool", "0.2.0"), *created))
    assert modified == "custom-tool:0.2.0;mcuhome-workbench:1.0.0"
    assert parse_generator_chain(modified)[0].product == "custom-tool"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "mcuhome-workbench",  # no version at all
        "MCUHome-Workbench:1.0.0",  # a product name is lowercase
        ":1.0.0",  # no product
        "mcuhome-workbench:",  # no version
        "-tool:1.0.0",  # a product starts with a letter or digit
        "custom-tool:1.0.0;",  # a trailing separator is an empty entry
    ],
)
def test_an_unreadable_generator_is_refused(value: object) -> None:
    """Refused, never skipped.

    Skipping an entry that does not parse would silently move the check
    to the entry behind it — which is a different tool, with different
    versions, and a claim rather than a fact.
    """
    with pytest.raises(BuildError):
        parse_generator_chain(value)


def test_an_empty_chain_cannot_be_written() -> None:
    with pytest.raises(BuildError) as caught:
        format_generator_chain(())
    assert "empty generator chain" in caught.value.message


def test_a_developer_context_reads_back_off_disk(tmp_path) -> None:
    """The form through a real ``context.yaml``/``manifest.yaml`` on disk.

    Everything else about this form is checked as objects, and objects
    never touch the one thing the on-disk form depends on: that a YAML
    scalar comes back as the string ``developer`` and not as something a
    parser decided it looked like. This is the builder's own reader
    (:mod:`mcuhome.compiler.contextread`) over a directory, which is how
    a context reaches a build.
    """
    from conftest import lock_context, write_context_request

    from mcuhome.compiler.contextread import read_context_manifest, verify_context

    root = tmp_path / "ctx"
    root.mkdir()
    (root / "model").mkdir()
    (root / "model" / "device-model.json").write_text('{"device": 1}\n', encoding="utf-8")
    request = ContextRequest(
        sdk=SdkPin(constraint="", version="", url="", sha256=""),
        build_environment=DeveloperEnvironment(),
        board=BOARD,
        created="2026-09-07T00:00:00Z",
    )
    write_context_request(request, out_dir=root)
    written = lock_context(root, request=request, build_environment=DeveloperEnvironment())

    manifest = read_context_manifest(root / MANIFEST_FILE)
    assert isinstance(manifest.build_environment, DeveloperEnvironment)
    assert manifest.sdk.sha256 == ""
    assert manifest.id == written.id
    assert verify_context(root).ok
