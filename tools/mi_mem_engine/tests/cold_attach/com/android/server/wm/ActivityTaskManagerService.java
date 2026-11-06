/* SPDX-License-Identifier: Apache-2.0 */
package com.android.server.wm;
/** Host boundary only: the adapter only synchronizes on this lock object. */
public final class ActivityTaskManagerService {
    public final Object mGlobalLock = new Object();
}
