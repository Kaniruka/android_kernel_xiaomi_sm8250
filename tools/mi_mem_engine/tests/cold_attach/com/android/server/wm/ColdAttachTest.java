/* SPDX-License-Identifier: Apache-2.0 */
package com.android.server.wm;

import android.os.IBinder;
import android.os.Looper;

import java.util.ArrayList;
import java.util.List;

/**
 * Focused host check for the fixed AOSP15 C06 launch adapter. It compiles the real
 * {@link MiMemEngineLaunch} state machine and replaces only its JNI/transport seam plus the Android
 * types that cannot link on a host. Worker tasks sit on an explicit FIFO until the test drains them,
 * so post ordering and cancel-before-run paths are visible; this is not a real thread, kernel, WM or
 * Android runtime and it proves adapter decisions only.
 */
public final class ColdAttachTest {
    private static final int APP_UID = 10123;
    private static final String BEGIN = "BEGIN";
    private static final String END = "END";

    private static final class LocalBinder implements IBinder {
        private DeathRecipient mRecipient;
        private boolean mDead;
        int unlinkCalls;

        public void linkToDeath(DeathRecipient recipient, int flags) {
            mRecipient = recipient;
        }

        public boolean unlinkToDeath(DeathRecipient recipient, int flags) {
            if (mDead) return true; // AOSP maps the already-dead recipient to success.
            unlinkCalls++;
            if (mRecipient != recipient) throw new java.util.NoSuchElementException();
            mRecipient = null;
            return true;
        }

        /** Fires the production death callback exactly like a Binder death notification. */
        void die() {
            mDead = true;
            DeathRecipient recipient = mRecipient;
            if (recipient != null) recipient.binderDied();
        }
    }

    private static final class TestThread implements android.app.IApplicationThread {
        final LocalBinder binder = new LocalBinder();

        public IBinder asBinder() {
            return binder;
        }
    }

    /** The only replaced production seam: records real BEGIN/END decisions instead of doing IPC. */
    private static final class RecordingBackend implements MiMemEngineLaunch.Backend {
        static final class Op {
            final String kind;
            final long launch;
            final long cookie;
            final int pid;
            final int uid;
            final int handle;

            Op(String kind, MiMemEngineLaunch.Session session) {
                this.kind = kind;
                this.launch = session.launch;
                this.cookie = session.cookie;
                this.pid = session.pid;
                this.uid = session.uid;
                this.handle = session.handle;
            }
        }

        final List<Op> ops = new ArrayList<>();
        final int[] identity = {31, 0xd1};
        boolean connected = true;
        int disconnects;
        Runnable duringBegin;

        public int[] binderIdentity(IBinder thread) {
            return identity;
        }

        public boolean connected() {
            return connected;
        }

        public void begin(MiMemEngineLaunch.Session session) {
            ops.add(new Op(BEGIN, session));
            if (duringBegin != null) duringBegin.run();
        }

        public void end(MiMemEngineLaunch.Session session) {
            ops.add(new Op(END, session));
        }

        public void closeBinderFd(MiMemEngineLaunch.Session session) {
            session.binderFd = -1;
        }

        public void disconnect() {
            disconnects++;
            connected = false;
        }

        List<Op> of(String kind) {
            List<Op> result = new ArrayList<>();
            for (Op op : ops) if (op.kind.equals(kind)) result.add(op);
            return result;
        }
    }

    private static final class Case {
        final ActivityTaskSupervisor supervisor = new ActivityTaskSupervisor();
        final RecordingBackend backend = new RecordingBackend();
        final MiMemEngineLaunch engine = new MiMemEngineLaunch(supervisor, backend);
        final Looper worker = Looper.newest();

        WindowProcessController process(int pid) {
            WindowProcessController process = new WindowProcessController(APP_UID);
            process.setPid(pid);
            process.thread = new TestThread();
            return process;
        }

        int queued() {
            return worker.pending();
        }

        void drain() {
            worker.drain();
        }
    }

    public static void main(String[] args) throws Exception {
        android.os.SystemProperties.enabled = true;
        coldAttachBindsRealIdentityThenRevokes();
        attachQueuedThenDoneBeforeDrain();
        crossLaunchCookiesStayMonotonic();
        oldEndAfterNewBeginKeepsNewSession();
        cancelDuringCommittedBeginEndsOnce();
        repeatedAttachBeginsOnce();
        staleTokenAndReplacedProcessNeverBind();
        prepareCancelsReplacedHostingRecord();
        coalescedReplacementGetsFreshCookie();
        binderDeathEndsSession();
        warmAssociateBeginsImmediately();
        coldRestartHandoverKeepsFreshCookie();
        disabledPropertyNeverBegins();
        System.out.println("ALL PASS");
    }

    private static void coldAttachBindsRealIdentityThenRevokes() {
        Case test = new Case();
        MiMemEngineLaunch.Session session = test.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        if (test.engine.associate(session, activity, null, APP_UID) != session) {
            throw new AssertionError("pending cookie must survive association");
        }
        if (session.cookie != 0 || session.launch == 0) {
            throw new AssertionError("a pending session holds a launch id, not a controller cookie");
        }
        expectOps(test, 0, 0);
        WindowProcessController process = test.process(4242);
        activity.app = process;
        test.engine.attached(session, activity, process);
        expectQueued(test, 1, "attach must queue one prepare");
        expectOps(test, 0, 0);
        test.drain();
        expectOps(test, 1, 0);
        RecordingBackend.Op begin = test.backend.of(BEGIN).get(0);
        if (begin.cookie <= 0 || begin.launch != session.launch || begin.pid != 4242
                || begin.uid != APP_UID || begin.handle != 0xd1) {
            throw new AssertionError("BEGIN must carry the committed cookie and captured identity");
        }
        test.engine.end(session);
        test.drain();
        expectOps(test, 1, 1);
        RecordingBackend.Op end = test.backend.of(END).get(0);
        if (end.cookie != begin.cookie || end.launch != begin.launch) {
            throw new AssertionError("END must match the BEGIN cookie and launch");
        }
        expectDisconnects(test, 0);
        System.out.println("PASS cold pending attach commits one monotonic cookie and revocation "
                + "ends that exact cookie");
    }

    private static void attachQueuedThenDoneBeforeDrain() {
        Case test = new Case();
        MiMemEngineLaunch.Session session = test.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        test.engine.associate(session, activity, null, APP_UID);
        WindowProcessController process = test.process(5001);
        activity.app = process;
        test.engine.attached(session, activity, process);
        expectQueued(test, 1, "prepare stays queued before the worker runs");
        test.engine.end(session); // done wins while prepare is still queued
        expectQueued(test, 2, "cancellation queues behind the pending prepare");
        test.drain();
        expectOps(test, 0, 0);
        expectQueued(test, 0, "worker queue drains");
        System.out.println("PASS done delivered while prepare is queued suppresses the BEGIN");
    }

    private static void crossLaunchCookiesStayMonotonic() {
        Case test = new Case();
        ActivityRecord coldActivity = new ActivityRecord();
        MiMemEngineLaunch.Session cold = test.engine.pending();
        test.engine.associate(cold, coldActivity, null, APP_UID);

        ActivityRecord warmActivity = new ActivityRecord();
        WindowProcessController warmProcess = test.process(6001);
        warmActivity.app = warmProcess;
        MiMemEngineLaunch.Session warm = test.engine.pending();
        test.engine.associate(warm, warmActivity, warmProcess, APP_UID);
        test.drain(); // warm launch gets the first controller cookie
        long warmCookie = test.backend.of(BEGIN).get(0).cookie;

        WindowProcessController coldProcess = test.process(6002);
        coldActivity.app = coldProcess;
        test.engine.attached(cold, coldActivity, coldProcess); // attaches after the warm BEGIN
        test.drain();
        expectOps(test, 2, 0);
        long coldCookie = test.backend.of(BEGIN).get(1).cookie;
        if (coldCookie <= warmCookie) {
            throw new AssertionError("BEGIN cookies must increase in submission order: "
                    + warmCookie + " then " + coldCookie);
        }
        expectDisconnects(test, 0);
        System.out.println("PASS a later-attaching cold launch gets a strictly greater BEGIN cookie "
                + "without disconnecting the live session");
    }

    private static void oldEndAfterNewBeginKeepsNewSession() {
        Case test = new Case();
        MiMemEngineLaunch.Session first = warmBegin(test, 7101);
        test.drain();
        long firstCookie = test.backend.of(BEGIN).get(0).cookie;
        MiMemEngineLaunch.Session second = warmBegin(test, 7102);
        test.drain();
        long secondCookie = test.backend.of(BEGIN).get(1).cookie;
        if (secondCookie <= firstCookie) throw new AssertionError("second BEGIN must be greater");

        test.engine.end(first); // late END of the older launch
        test.drain();
        List<RecordingBackend.Op> ends = test.backend.of(END);
        if (ends.size() != 1 || ends.get(0).cookie != firstCookie) {
            throw new AssertionError("only the old cookie may be ended: " + ends);
        }
        expectDisconnects(test, 0);
        System.out.println("PASS a late END carries the old cookie and leaves the newer session alone");
    }

    private static void cancelDuringCommittedBeginEndsOnce() {
        Case test = new Case();
        MiMemEngineLaunch.Session session = test.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        test.engine.associate(session, activity, null, APP_UID);
        WindowProcessController process = test.process(7201);
        activity.app = process;
        test.backend.duringBegin = () -> test.engine.end(session); // done races the in-flight BEGIN
        test.engine.attached(session, activity, process);
        test.drain();
        expectOps(test, 1, 1);
        RecordingBackend.Op begin = test.backend.of(BEGIN).get(0);
        RecordingBackend.Op end = test.backend.of(END).get(0);
        if (begin.cookie != end.cookie) {
            throw new AssertionError("the revocation must END the committed cookie");
        }
        System.out.println("PASS cancellation queued during an in-flight BEGIN ends that cookie once");
    }

    private static void repeatedAttachBeginsOnce() {
        Case test = new Case();
        MiMemEngineLaunch.Session session = test.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        test.engine.associate(session, activity, null, APP_UID);
        WindowProcessController process = test.process(7301);
        activity.app = process;
        test.engine.attached(session, activity, process);
        test.drain();
        test.engine.attached(session, activity, process);
        test.engine.attached(session, activity, test.process(7302));
        test.drain();
        expectOps(test, 1, 0);
        System.out.println("PASS repeated attach keeps a single BEGIN");
    }

    private static void staleTokenAndReplacedProcessNeverBind() {
        Case stale = new Case();
        MiMemEngineLaunch.Session session = stale.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        stale.engine.associate(session, activity, null, APP_UID);
        ActivityRecord other = new ActivityRecord();
        WindowProcessController process = stale.process(7401);
        other.app = process;
        stale.engine.attached(session, other, process);
        stale.drain();
        expectOps(stale, 0, 0);

        Case uid = new Case();
        MiMemEngineLaunch.Session uidSession = uid.engine.pending();
        ActivityRecord uidActivity = new ActivityRecord();
        uid.engine.associate(uidSession, uidActivity, null, APP_UID);
        WindowProcessController wrongUid = new WindowProcessController(APP_UID + 1);
        wrongUid.setPid(7402);
        wrongUid.thread = new TestThread();
        uidActivity.app = wrongUid;
        uid.engine.attached(uidSession, uidActivity, wrongUid);
        uid.drain();
        expectOps(uid, 0, 0);

        Case replaced = new Case();
        MiMemEngineLaunch.Session expected = replaced.engine.pending();
        ActivityRecord hostless = new ActivityRecord();
        WindowProcessController first = replaced.process(7403);
        replaced.engine.associate(expected, hostless, first, APP_UID);
        WindowProcessController second = replaced.process(7404);
        replaced.engine.attached(expected, hostless, second);
        replaced.drain();
        expectOps(replaced, 0, 0);
        System.out.println("PASS a stale token, a foreign UID and a replaced process never bind");
    }

    private static void prepareCancelsReplacedHostingRecord() {
        Case test = new Case();
        MiMemEngineLaunch.Session session = test.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        test.engine.associate(session, activity, null, APP_UID);
        WindowProcessController process = test.process(7501);
        activity.app = process;
        test.engine.attached(session, activity, process);
        // The record is hosted by another process before the queued prepare revalidates.
        activity.app = test.process(7502);
        test.drain();
        expectOps(test, 0, 0);
        // The claim was consumed, so the session cannot silently stay pending and bind later.
        WindowProcessController late = test.process(7503);
        activity.app = late;
        test.engine.attached(session, activity, late);
        test.drain();
        expectOps(test, 0, 0);
        expectQueued(test, 0, "the cancelled session leaves no queued work");
        System.out.println("PASS a hosting record replaced before prepare cancels the session "
                + "instead of leaving a claimed pending cookie");
    }

    private static void coalescedReplacementGetsFreshCookie() {
        Case test = new Case();
        ActivityRecord first = new ActivityRecord();
        WindowProcessController firstProcess = test.process(7601);
        first.app = firstProcess;
        MiMemEngineLaunch.Session session = test.engine.pending();
        test.engine.associate(session, first, firstProcess, APP_UID);
        test.drain();
        long firstCookie = test.backend.of(BEGIN).get(0).cookie;

        ActivityRecord second = new ActivityRecord();
        WindowProcessController secondProcess = test.process(7602);
        second.app = secondProcess;
        MiMemEngineLaunch.Session replaced = test.engine.associate(session, second, secondProcess,
                APP_UID);
        if (replaced == session || replaced.launch == session.launch || replaced.cookie != 0) {
            throw new AssertionError("coalesced replacement must use a fresh launch identity");
        }
        test.drain();
        if (test.backend.of(END).size() != 1
                || test.backend.of(END).get(0).cookie != firstCookie) {
            throw new AssertionError("the replaced launch must be ended with its own cookie");
        }
        test.engine.attached(replaced, second, secondProcess);
        test.drain();
        expectOps(test, 2, 1);
        long secondCookie = test.backend.of(BEGIN).get(1).cookie;
        if (secondCookie <= firstCookie) {
            throw new AssertionError("the replacement BEGIN must be greater");
        }
        expectDisconnects(test, 0);
        System.out.println("PASS coalesced Activity replacement revokes the old cookie and begins "
                + "a fresh strictly greater one");
    }

    private static void binderDeathEndsSession() {
        Case test = new Case();
        MiMemEngineLaunch.Session session = test.engine.pending();
        ActivityRecord activity = new ActivityRecord();
        test.engine.associate(session, activity, null, APP_UID);
        WindowProcessController process = test.process(7701);
        activity.app = process;
        TestThread thread = (TestThread) process.thread;
        test.engine.attached(session, activity, process);
        test.drain();
        expectOps(test, 1, 0);
        thread.binder.die(); // the real production DeathRecipient calls end(session)
        test.drain();
        expectOps(test, 1, 1);
        if (test.backend.of(END).get(0).cookie != test.backend.of(BEGIN).get(0).cookie) {
            throw new AssertionError("death must END the committed cookie");
        }
        System.out.println("PASS Binder death through the production recipient ends the session");
    }

    private static void warmAssociateBeginsImmediately() {
        Case test = new Case();
        ActivityRecord activity = new ActivityRecord();
        WindowProcessController process = test.process(7801);
        activity.app = process;
        MiMemEngineLaunch.Session session = test.engine.pending();
        test.engine.associate(session, activity, process, APP_UID);
        test.drain();
        expectOps(test, 1, 0);
        test.engine.attached(session, activity, process); // attach seam also fires for warm launches
        test.drain();
        expectOps(test, 1, 0);
        System.out.println("PASS warm launch begins at association and the attach seam adds nothing");
    }

    private static void coldRestartHandoverKeepsFreshCookie() {
        Case test = new Case();
        ActivityRecord activity = new ActivityRecord();
        WindowProcessController warm = test.process(7901);
        activity.app = warm;
        MiMemEngineLaunch.Session previous = test.engine.pending();
        test.engine.associate(previous, activity, warm, APP_UID);
        test.drain();
        long warmCookie = test.backend.of(BEGIN).get(0).cookie;

        MiMemEngineLaunch.Session takeover = test.engine.pending(previous, activity, APP_UID);
        test.drain();
        if (takeover == previous || takeover.launch == previous.launch || takeover.cookie != 0) {
            throw new AssertionError("cold restart must not reuse a cancelled cookie or identity");
        }
        if (test.backend.of(END).size() != 1
                || test.backend.of(END).get(0).cookie != warmCookie) {
            throw new AssertionError("the warm cookie must be ended");
        }
        WindowProcessController cold = test.process(7902);
        activity.app = cold;
        test.engine.attached(takeover, activity, cold);
        test.drain();
        expectOps(test, 2, 1);
        long coldCookie = test.backend.of(BEGIN).get(1).cookie;
        if (coldCookie <= warmCookie) throw new AssertionError("cold BEGIN must be greater");
        test.engine.end(previous); // late END of the replaced launch
        test.drain();
        expectOps(test, 2, 1);
        expectDisconnects(test, 0);
        System.out.println("PASS warm-to-cold handover revokes the old cookie and begins a fresh "
                + "strictly greater one");
    }

    private static void disabledPropertyNeverBegins() {
        android.os.SystemProperties.enabled = false;
        try {
            Case test = new Case();
            MiMemEngineLaunch.Session session = test.engine.pending();
            ActivityRecord activity = new ActivityRecord();
            WindowProcessController process = test.process(8001);
            activity.app = process;
            test.engine.associate(session, activity, null, APP_UID);
            test.engine.attached(session, activity, process);
            test.drain();
            expectOps(test, 0, 0);
        } finally {
            android.os.SystemProperties.enabled = true;
        }
        System.out.println("PASS the default-off property still suppresses every BEGIN");
    }

    private static MiMemEngineLaunch.Session warmBegin(Case test, int pid) {
        ActivityRecord activity = new ActivityRecord();
        WindowProcessController process = test.process(pid);
        activity.app = process;
        MiMemEngineLaunch.Session session = test.engine.pending();
        test.engine.associate(session, activity, process, APP_UID);
        return session;
    }

    private static void expectOps(Case test, int begins, int ends) {
        if (test.backend.of(BEGIN).size() != begins || test.backend.of(END).size() != ends) {
            throw new AssertionError("expected BEGIN=" + begins + " END=" + ends + " but saw "
                    + test.backend.ops.size() + " operations");
        }
    }

    private static void expectQueued(Case test, int queued, String what) {
        if (test.queued() != queued) {
            throw new AssertionError(what + ": expected " + queued + " queued tasks but saw "
                    + test.queued());
        }
    }

    private static void expectDisconnects(Case test, int disconnects) {
        if (test.backend.disconnects != disconnects) {
            throw new AssertionError("unexpected controller disconnect: "
                    + test.backend.disconnects);
        }
    }
}
