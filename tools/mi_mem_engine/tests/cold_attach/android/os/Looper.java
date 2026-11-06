/* SPDX-License-Identifier: Apache-2.0 */
package android.os;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/**
 * Host boundary only. Tasks stay queued until the test drains them explicitly, so the adapter's
 * real post ordering and its cancel-before-run paths are exercised without pretending to be a
 * thread.
 */
public final class Looper {
    private static final List<Looper> ALL = new ArrayList<>();
    private final ArrayDeque<Runnable> mQueue = new ArrayDeque<>();

    public Looper() {
        ALL.add(this);
    }

    /** Newest loop belongs to the adapter just constructed in this test case. */
    public static Looper newest() {
        return ALL.get(ALL.size() - 1);
    }

    void enqueue(Runnable task) {
        mQueue.add(task);
    }

    public int pending() {
        return mQueue.size();
    }

    public void drain() {
        Runnable task;
        while ((task = mQueue.poll()) != null) {
            task.run();
        }
    }
}
