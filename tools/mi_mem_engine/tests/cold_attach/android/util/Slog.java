/* SPDX-License-Identifier: Apache-2.0 */
package android.util;
/** Host boundary only: log output is not part of the checked behaviour. */
public final class Slog {
    private Slog() { }
    public static int d(String tag, String msg) { return 0; }
    public static int i(String tag, String msg) { return 0; }
    public static int w(String tag, String msg) { return 0; }
}
