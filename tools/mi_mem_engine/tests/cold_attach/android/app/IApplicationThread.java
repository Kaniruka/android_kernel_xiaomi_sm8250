/* SPDX-License-Identifier: Apache-2.0 */
package android.app;
import android.os.IBinder;
/** Host boundary only; mirrors the asBinder() member the adapter uses. */
public interface IApplicationThread {
    IBinder asBinder();
}
