/* SPDX-License-Identifier: Apache-2.0 */
package android.os;
/** Host boundary only. */
public class RemoteException extends android.util.AndroidException {
    public RemoteException() { super(); }
    public RemoteException(String message) { super(message); }
}
