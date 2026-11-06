/* SPDX-License-Identifier: Apache-2.0 */
package android.os;
/** Host boundary only: no real thread or Android looper is created. */
public final class HandlerThread {
    private final Looper mLooper = new Looper();

    public HandlerThread(String name) {
    }

    public void start() {
    }

    public Looper getLooper() {
        return mLooper;
    }
}
