# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome's build environment, delivered as a container image — the
# container profile of the build environment specification
# (../../docs/spec/build-environment-specification.md §2).
#
# **This image is an assembly, not a build.** Everything it contains beyond
# the host baseline arrives in the two packages
# `scripts/build_env_package.py` produces: the source world in
# `mcuhome-build-workspace`, the host tools in
# `mcuhome-build-tools_<os>-<arch>`. Nothing here fetches a source tree,
# runs `west update`, or reaches an index: the archives are the input and
# the wheel set installs with `--no-index`. That is what makes the image a
# *delivery* of a package set rather than a second, subtly different
# environment — "two images built from the same set are the same
# environment" (§2).
#
# **Build it through `scripts/build_env_image.py`, not by hand.** §5.2 asks
# an image to repeat every member of its packages' declaration as an OCI
# label, and a Dockerfile cannot read a file into a `LABEL`. The script
# reads the declaration the workspace package carries and passes the labels
# on the command line, which is also where it checks that the archives it
# was handed really are the set the declaration names. An image built
# straight from this file carries the labels below and none of §5.2's — no
# orchestrator will ever match it.
#
# The image the baked `containers/build-container/` produces is untouched
# by all of this and continues in parallel until the switchover.

# The pinned Debian base, digest-pinned exactly as
# `containers/build-container/Dockerfile` pins it — `trixie-slim` moves with
# every point release. It is this base's Python that decides the image: the
# wheel set in the tools package was built by the same interpreter, so the
# build virtual environment below can only be created by this one.
# `tests/python/test_env_image.py` holds the three statements of that pin
# together.
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

# --------------------------------------------------------------------------
# Stage 1: unpack the package set
# --------------------------------------------------------------------------
#
# A stage of its own so that the archives never reach a layer of the final
# image: they are 800 MB that nothing at run time reads, and a `RUN` that
# deletes them again would only hide them one layer down.
#
# The build context is a directory holding exactly the two archives — the
# script stages it — so the two wildcards below can each match one file.
FROM ${DEBIAN_IMAGE} AS packages

# zstd, because `.tar.zst` is the package file format
# (`packaging/build-environment/README.md`) and tar shells out for it.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        zstd \
 && rm -rf /var/lib/apt/lists/*

COPY mcuhome-build-workspace-*.tar.zst /archives/workspace.tar.zst
COPY mcuhome-build-tools_*.tar.zst /archives/tools.tar.zst

# The west workspace is made readable and traversable for every user, and
# the mode is `a+rwX` rather than `a+rX` only because that is what the
# image was built with before the builder stopped writing into it at all:
# a step now assembles a view of this workspace under `work` and patches
# copies inside it, so nothing here is written whether or not it could be.
# What every UID does need is to read it — the orchestrator chooses the
# UID and the image cannot know it.
#
# It happens here rather than in the final stage on purpose: a `chmod -R`
# after the `COPY` below would write a second copy of every one of those
# files into a new layer and double the image.
RUN mkdir -p /environment/workspace /environment/tools \
 && tar --zstd -xf /archives/workspace.tar.zst -C /environment/workspace \
 && tar --zstd -xf /archives/tools.tar.zst -C /environment/tools \
 && chmod -R a+rwX /environment/workspace/workspace

# --------------------------------------------------------------------------
# Stage 2: the image itself
# --------------------------------------------------------------------------
FROM ${DEBIAN_IMAGE}

LABEL org.opencontainers.image.title="MCUHome build environment" \
      org.opencontainers.image.description="MCUHome's build environment, assembled from its two packages: Zephyr and the Matter SDK at the pinned revisions, the Zephyr SDK toolchain, and the entry point of the build environment specification." \
      org.opencontainers.image.source="https://github.com/mcu-home/mcuhome-sdk" \
      org.opencontainers.image.licenses="Apache-2.0"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The host baseline (`docs/design/build-environment.md` section 6) and
# nothing more. Everything a build actually compiles with comes out of the
# tools package; this list is what the base has to bring so that the package
# can be a package and not a Linux distribution.
#
#   build system   what Zephyr's CMake glue calls out to, plus the host
#                  compiler the Matter SDK's inner build and native_sim need
#   runtime        git for west's manifest resolution and Zephyr's version
#                  stamping, python3 to create the build virtual environment
#                  from the packaged wheels, ccache because the design makes
#                  the compiler cache a hard requirement, xz/unzip/file for
#                  the odd unpacking step in a module's CMake
#
# **No cmake and no ninja.** The tools package carries both, pinned and
# measured against the host baseline, and the entry point puts them first on
# `PATH`. A second pair from the distribution would differ from what the
# subprocess profile uses on the same package set — and an environment that
# behaves differently in one profile is broken, not clever (§2).
#
# **No zap and none of the desktop libraries it needs.** Its output is a
# release constant and the workspace package carries it pre-generated
# (`packaging/build-environment/README.md`).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        device-tree-compiler \
        g++ \
        gcc \
        gperf \
        libc6-dev \
        make \
        python3 \
        python3-venv \
        ca-certificates \
        ccache \
        file \
        git \
        unzip \
        xz-utils \
 && rm -rf /var/lib/apt/lists/*

# The package set, at the paths the two environment variables at the bottom
# of this file name. Where an environment keeps its own content is
# deliberately its own business (§2 and §4: "Everything outside `mcuhome/`
# is your environment as its packages define it"), so these paths are stated
# in the image rather than assumed by anybody.
COPY --from=packages /environment /opt/mcuhome/build-environment

# Provisioning's finalization, baked in (`docs/design/build-environment.md`
# section 5, step 3). The tools package ships wheels rather than a ready
# virtual environment because a virtual environment is not relocatable, so
# the one thing every profile has to do for itself is create it at its final
# location — here, once, at image build time.
#
# `--no-index` is the point and not a precaution: the wheel set is complete
# by construction (`pip wheel` over the pinned requirements, transitive
# dependencies included), so a build that reached an index would be
# installing something the package does not contain.
RUN /usr/bin/python3 -m venv /opt/mcuhome/build-environment/tools/venv \
 && /opt/mcuhome/build-environment/tools/venv/bin/pip install \
        --no-cache-dir --disable-pip-version-check --no-index \
        --find-links /opt/mcuhome/build-environment/tools/wheels \
        /opt/mcuhome/build-environment/tools/wheels/*.whl

# §4's tree. `mcuhome/bin/build-environment-entry` is the environment's own
# content and the only thing in here the image provides; everything else is
# a directory the orchestrator mounts over — created empty, and left
# world-writable so that a container started without a mount still works
# whichever UID it runs as.
#
# The link is **relative** on purpose: it stays correct when the whole tree
# is relocated — copied out of this image, or unpacked at another prefix by
# the subprocess profile — which is exactly the case the entry point's
# fallback to its own location is for. The variable below makes that
# fallback moot in this image; the relative link keeps the layout portable
# for anyone who copies it.
RUN mkdir -p /mcuhome/bin \
 && ln -s ../../opt/mcuhome/build-environment/tools/bin/build-environment-entry \
        /mcuhome/bin/build-environment-entry \
 && mkdir -p /mcuhome/work /mcuhome/out /mcuhome/sdk /mcuhome/build-context \
        /mcuhome/cache/local /mcuhome/cache/session /mcuhome/cache/project \
        /mcuhome/cache/shared \
 && chmod 1777 /mcuhome/work /mcuhome/out /mcuhome/sdk /mcuhome/build-context \
        /mcuhome/cache /mcuhome/cache/local /mcuhome/cache/session \
        /mcuhome/cache/project /mcuhome/cache/shared

# Git refuses to operate in a repository owned by another user (the
# CVE-2022-24765 hardening), and an orchestrator that runs this container as
# the calling user's UID is exactly that situation: the workspace was
# unpacked as root. Both things a build reads out of `.git` go through git,
# and they fail differently — west's manifest resolution raises and stops
# the CMake configure, while Zephyr's `git describe` silently leaves
# `BUILD_VERSION` unset and changes the firmware without an error anywhere.
#
# Scoped to the workspace prefix rather than the global '*': the glob
# matches nested repositories too, the Matter SDK's submodules included.
# `--system`, because the calling user has no home directory here and a
# per-user configuration would never be read.
RUN git config --system --add safe.directory \
        '/opt/mcuhome/build-environment/workspace/workspace/*'

# The two ccache settings a Zephyr build is unusable without. Where the
# cache *is* deliberately stays out of this file: §8 gives the step four
# tiers and the builder points ccache at them per step, and an environment
# variable overrides this file, so a `cache_dir` here would be a value that
# is never the one in force.
#
#   compiler_check = content   the compilers in this image never change
#                              without the image changing, and mtimes do not
#                              survive a pull.
#   ignore_options = -specs=*  without this, ccache caches nothing at all:
#                              Zephyr's picolibc integration passes
#                              `-specs=picolibc.specs`, a bare file name the
#                              linker resolves against the toolchain and
#                              ccache resolves against the working
#                              directory — every compile is then rejected as
#                              "bad compiler arguments". Excluding it from
#                              the hash is safe for the same reason as
#                              compiler_check: the specs file is part of the
#                              toolchain, and the toolchain is part of the
#                              package set the image declares.
RUN printf '%s\n' \
        'compiler_check = content' \
        'ignore_options = -specs=*' \
        > /etc/ccache.conf

# A container run as the calling user's UID usually has no /etc/passwd entry
# here, so HOME has to be somewhere world-writable or the first tool that
# wants one fails.
RUN mkdir -p /tmp/mcuhome-home && chmod 1777 /tmp/mcuhome-home

# Where this environment's own two packages are. The entry point takes the
# tools root from its own location when it has to, but it can only *check*
# the workspace root and never derive it — the builder needs it to find the
# source world and the pre-generated Matter code, so the image is the one
# that has to say where it put them.
#
# Deliberately not here: `PATH`, `VIRTUAL_ENV`, `ZEPHYR_SDK_INSTALL_DIR` and
# `ZEPHYR_TOOLCHAIN_VARIANT`. The entry point sets all four, so that both
# profiles get the same environment out of the same file.
ENV MCUHOME_BUILD_ENV_TOOLS=/opt/mcuhome/build-environment/tools \
    MCUHOME_BUILD_ENV_WORKSPACE=/opt/mcuhome/build-environment/workspace \
    HOME=/tmp/mcuhome-home

# A default command, and no entry point. §6 has the orchestrator run the
# entry point by its absolute path with no arguments, which needs neither;
# what the default buys is that `docker run <image>` does the same thing the
# orchestrator does, and leaving ENTRYPOINT unset keeps `docker run <image>
# ccache -s` working for whoever is looking into the image.
CMD ["/mcuhome/bin/build-environment-entry"]
