/* SPDX-License-Identifier: Apache-2.0 */
package android.os;
/** Host boundary only: posts are queued on the explicit FIFO and never run implicitly. */
public final class Handler {
    private final Looper mLooper;

    public Handler(Looper looper) {
        mLooper = looper;
    }

    public boolean post(Runnable task) {
        mLooper.enqueue(task);
        return true;
    }
}
