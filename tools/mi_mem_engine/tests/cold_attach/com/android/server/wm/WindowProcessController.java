/* SPDX-License-Identifier: Apache-2.0 */
package com.android.server.wm;
import android.app.IApplicationThread;
/** Host boundary only; members mirror android-15.0.0_r1 WindowProcessController signatures. */
public final class WindowProcessController {
    public final int mUid;
    public IApplicationThread thread;
    private int mPid;
    public WindowProcessController(int uid) { mUid = uid; }
    public int getPid() { return mPid; }
    public void setPid(int pid) { mPid = pid; }
    public IApplicationThread getThread() { return thread; }
    public boolean hasThread() { return thread != null; }
}
