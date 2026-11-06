/* SPDX-License-Identifier: Apache-2.0 */
package com.android.server.wm;

import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.RemoteException;
import android.util.Slog;

import java.io.IOException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Fixed AOSP15 C06 launch adapter. A warm or hot launch binds its live process immediately; a cold
 * launch keeps a pending session bound to the exact ActivityRecord until the real attach. Every
 * entry point that reads or writes WM state requires the WM global lock, and JNI and socket I/O run
 * on one serial worker so no WM thread ever waits for IPC.
 */
final class MiMemEngineLaunch {
    private static final String TAG = "MiMemEngineLaunch";
    static final String ENABLED_PROPERTY = "sys.mi_mem_engine.enabled";
    private static final AtomicLong NEXT_LAUNCH = new AtomicLong();
    // The kernel rejects a BEGIN whose cookie is not strictly greater than the last accepted one.
    // A cookie is handed out only when the serial worker commits BEGIN, so the sequence follows
    // actual submission order across launches. The helper opens its controller per peer connection,
    // which resets that kernel mark, so an in-process counter is enough.
    private static final AtomicLong NEXT_COOKIE = new AtomicLong();

    // JNI stays in this class so the registered Binder identity bridge is unchanged.
    static native int[] binderIdentity(IBinder binder);

    /** JNI and controller transport seam; production uses {@link MiMemEngineSocketBackend}. */
    interface Backend {
        /** Duplicated Binder driver fd plus remote handle, or null when the node is unusable. */
        int[] binderIdentity(IBinder thread);

        boolean connected();

        /** Sends BEGIN for the captured identity; owns and closes the duplicated Binder driver fd. */
        void begin(Session session) throws IOException;

        void end(Session session) throws IOException;

        /** Closes a duplicated Binder driver fd that no BEGIN consumed. */
        void closeBinderFd(Session session);

        /** Drops the controller connection, which revokes the active scene. */
        void disconnect();
    }

    static final class Session {
        final long launch = NEXT_LAUNCH.incrementAndGet(); // Local log identity; never sent.
        long cookie; // 0 while pending; the controller cookie once the worker commits BEGIN.
        // 0 pending; 1 BEGIN committed or in flight; 2 cancelled. A single CAS at the BEGIN commit
        // point decides done against BEGIN, so cancellation that wins sends no BEGIN at all.
        final AtomicInteger state = new AtomicInteger();
        private final AtomicBoolean attachClaimed = new AtomicBoolean();
        volatile ActivityRecord activity; // Exact launch token; WM lock.
        volatile WindowProcessController process; // Expected at associate, captured at attach.
        volatile int pid;
        volatile int uid;
        int activityUid;
        volatile IBinder thread; // Strong until the worker finishes, preventing handle reuse.
        int handle;
        int binderFd = -1; // Transport-owned duplicate; -1 when absent; closed exactly once.
        boolean begun; // Worker only: BEGIN reached the controller.
        boolean endSent; // Worker only: the single revocation END was already sent.
        volatile MiMemEngineDeathRegistration death;

        boolean claimAttach() {
            return attachClaimed.compareAndSet(false, true);
        }
    }

    private final ActivityTaskSupervisor mSupervisor;
    private final Handler mWorker;
    private final Backend mBackend;

    MiMemEngineLaunch(ActivityTaskSupervisor supervisor, Backend backend) {
        mSupervisor = supervisor;
        mBackend = backend;
        HandlerThread thread = new HandlerThread(TAG);
        thread.start();
        mWorker = new Handler(thread.getLooper());
    }

    /** Cookie-only pending for a launch whose ActivityRecord is not resolved yet. */
    Session pending() {
        return new Session();
    }

    /**
     * Pending for a cold launch, or for a warm launch whose process died before bind. The previous
     * cookie is cancelled here and never reused, so its late BEGIN cannot cover this launch, while
     * this fresh cookie can still capture the process that actually attaches.
     */
    Session pending(Session previous, ActivityRecord activity, int activityUid) {
        end(previous);
        Session session = new Session();
        session.activity = activity;
        session.activityUid = activityUid;
        return session;
    }

    /**
     * Binds a pending session to the launched record. A live expected process begins right away,
     * which keeps the warm/hot path unchanged; otherwise the session stays pending for the real
     * attach. A replaced token ends the old cookie and returns a fresh one.
     */
    Session associate(Session session, ActivityRecord activity, WindowProcessController expected,
            int activityUid) {
        if (activity == null) return null;
        if (session != null && session.state.get() == 0 && session.activity == null) {
            session.activity = activity;
            session.activityUid = activityUid;
            session.process = liveProcess(expected);
            if (session.process != null) attached(session, activity, session.process);
            return session;
        }
        if (session != null && session.state.get() == 0 && session.activity == activity) {
            return session;
        }
        // Coalesced trampoline or reused LaunchingState: the old cookie must never BEGIN late.
        end(session);
        Session fresh = new Session();
        fresh.activity = activity;
        fresh.activityUid = activityUid;
        fresh.process = liveProcess(expected);
        if (fresh.process != null) attached(fresh, activity, fresh.process);
        return fresh;
    }

    /**
     * Captures the identity of the process that really got attached to this launch. Repeated
     * attaches, a stale ActivityRecord token, a replaced process and a UID mismatch are ignored, so
     * only the exact token and the process owning it can ever bind.
     */
    void attached(Session session, ActivityRecord activity, WindowProcessController process) {
        if (session == null || activity == null || process == null) return;
        if (session.activity != activity) return; // Stale ActivityRecord token.
        if (session.state.get() != 0) return; // Cancelled, or identity already committed.
        if (session.process != null && session.process != process) return; // Replaced identity.
        if (process.mUid != session.activityUid) return; // Exact application UID of the record.
        if (activity.app != process) return; // The record is hosted by another process.
        if (process.getThread() == null || process.getPid() <= 0) return;
        if (!android.os.SystemProperties.getBoolean(ENABLED_PROPERTY, false)) {
            end(session);
            return;
        }
        if (!session.claimAttach()) return; // At most one attach per cookie.
        session.process = process;
        session.pid = process.getPid();
        session.uid = process.mUid;
        session.thread = process.getThread().asBinder();
        session.death = new MiMemEngineDeathRegistration(session.thread);
        mWorker.post(() -> prepare(session));
    }

    /** Cancels the session. A queued BEGIN finds state 2 and sends nothing. */
    void end(Session session) {
        if (session == null || session.state.getAndSet(2) == 2) return;
        mWorker.post(() -> release(session));
    }

    private static WindowProcessController liveProcess(WindowProcessController process) {
        if (process == null || process.getThread() == null || process.getPid() <= 0) return null;
        return process;
    }

    private void release(Session session) {
        try {
            // A cookie cancelled before BEGIN never reached the controller, so it is not ENDed.
            if (session.begun && mBackend.connected() && !session.endSent) {
                mBackend.end(session);
                session.endSent = true;
                Slog.d(TAG, "END launch=" + session.launch + " cookie=" + session.cookie
                        + " tid=" + session.pid);
            }
        } catch (IOException e) {
            mBackend.disconnect();
        } finally {
            MiMemEngineDeathRegistration death = session.death;
            session.death = null;
            if (death != null) death.unlink();
        }
    }

    private void prepare(Session session) {
        try {
            if (session.state.get() == 2) return;
            // Kernel resolves the exact Binder node owner and verifies TID and UID. The session
            // holds the Binder strongly until the worker finishes, preventing handle reuse.
            int[] identity = mBackend.binderIdentity(session.thread);
            if (identity == null || identity.length < 2 || identity[0] < 0) {
                end(session);
                return;
            }
            session.handle = identity[1];
            session.binderFd = identity[0];
            synchronized (mSupervisor.mService.mGlobalLock) {
                if (session.process == null || session.process.getPid() != session.pid
                        || session.process.getThread() == null
                        || session.process.getThread().asBinder() != session.thread
                        || session.activity.app != session.process) {
                    // The process or hosting record was replaced after attach. Nothing was linked
                    // yet, so cancel now: an attach claim with state 0 could never bind again.
                    end(session);
                    return;
                }
                if (session.state.get() == 2) return;
            }
            MiMemEngineDeathRegistration death = session.death;
            if (death == null) {
                end(session);
                return;
            }
            death.link(() -> end(session));
            if (!session.state.compareAndSet(0, 1)) {
                death.unlink();
                return;
            }
            session.cookie = NEXT_COOKIE.incrementAndGet();
            mBackend.begin(session);
            session.begun = true;
            Slog.d(TAG, "BEGIN launch=" + session.launch + " cookie=" + session.cookie
                    + " tid=" + session.pid);
            // done racing a committed BEGIN is ordered behind it on this worker and stays bounded
            // by the kernel lease.
            if (session.state.get() == 2 && !session.endSent) {
                mBackend.end(session);
                session.endSent = true;
            }
        } catch (IOException | RemoteException e) {
            mBackend.disconnect();
            end(session);
        } finally {
            mBackend.closeBinderFd(session);
        }
    }
}
