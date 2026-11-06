#!/bin/bash
# Linux host; requires a JDK. Compiles the actual production launch adapter against a minimal
# Android boundary and replaces only its JNI/transport seam.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
out=$(mktemp -d)
trap 'rm -rf -- "$out"' EXIT
mapfile -t boundary < <(find "$here/cold_attach" -name '*.java' | sort)
"${JAVAC:-javac}" -d "$out" "${boundary[@]}" \
    "$here/../aosp/services/core/java/com/android/server/wm/MiMemEngineLaunch.java" \
    "$here/../aosp/services/core/java/com/android/server/wm/MiMemEngineDeathRegistration.java"
"${JAVA:-java}" -cp "$out" com.android.server.wm.ColdAttachTest
