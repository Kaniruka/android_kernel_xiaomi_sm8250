/* SPDX-License-Identifier: Apache-2.0 */
package android.os;
/** Host boundary only: one switch covers the default-off property the adapter reads. */
public final class SystemProperties {
    public static volatile boolean enabled;
    private SystemProperties() { }
    public static boolean getBoolean(String key, boolean def) { return enabled; }
}
