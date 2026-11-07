Kernel release identity
=======================

Supported Xiaomi device configurations use ``CONFIG_LOCALVERSION="-perf"``
and disable automatic SCM suffixes. The release build script passes
``LOCALVERSION=-g<commit>`` using the source HEAD abbreviated to 12 hexadecimal
characters (or more if needed to disambiguate), suppresses the fallback ``+``
suffix and defaults the build
identity to ``android-build@localhost``. Environment overrides for the build
user and host remain supported.

With the current Makefile version, the release is ``4.19.325-perf-g<commit>``.
The hash identifies HEAD; uncommitted changes are not represented by it. The
previous project, CIP and ST suffixes are no longer appended; this changes
the visible release string, not the code or patch contents. Provenance remains
in Git history and the source licensing and documentation.

The release identity applies to both MIUI and AOSP release-script builds.
Direct make invocations must pass the same LOCALVERSION explicitly to include
the hash; otherwise the configuration retains its existing suffix policy.
The separate MIUI
feature fragment remains restricted to the MIUI build path. Package filenames
and branch names are independent of the kernel release string.
