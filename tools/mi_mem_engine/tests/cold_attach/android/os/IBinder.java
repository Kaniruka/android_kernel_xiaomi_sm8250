/* SPDX-License-Identifier: Apache-2.0 */
package android.os;
/** Host boundary only; mirrors the AOSP15 IBinder members the production adapter uses. */
public interface IBinder {
    interface DeathRecipient { void binderDied(); }
    void linkToDeath(DeathRecipient recipient, int flags) throws RemoteException;
    boolean unlinkToDeath(DeathRecipient recipient, int flags);
}
