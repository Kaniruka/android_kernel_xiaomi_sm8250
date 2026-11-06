/* SPDX-License-Identifier: Apache-2.0 */
package com.android.server.wm;

import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.os.IBinder;

import java.io.FileDescriptor;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

import libcore.io.IoUtils;

/**
 * Production JNI and controller-socket transport for {@link MiMemEngineLaunch}. Only the adapter's
 * serial worker uses it, and a closed socket revokes the controller scene. The wire protocol, the
 * SCM_RIGHTS transfer and the peer check are unchanged from the accepted 13a transport.
 */
final class MiMemEngineSocketBackend implements MiMemEngineLaunch.Backend {
    private static final String TAG = "MiMemEngineLaunch";
    private static final int WIRE_VERSION = 1;
    private static final int OP_BEGIN = 1;
    private static final int OP_END = 2;
    private static final int PACKET_SIZE = 32;
    private static final int REPLY_SIZE = 4;
    private static final int LEASE_MS = 1000;
    private static final int SO_TIMEOUT_MS = 200;

    private LocalSocket mSocket; // Worker only; disconnect revokes the controller.

    @Override
    public int[] binderIdentity(IBinder thread) {
        return MiMemEngineLaunch.binderIdentity(thread);
    }

    @Override
    public boolean connected() {
        return mSocket != null;
    }

    @Override
    public void begin(MiMemEngineLaunch.Session session) throws IOException {
        // dupDriverFD can legitimately return 0, so -1 is the only empty sentinel.
        if (session.binderFd < 0) throw new IOException("missing Binder driver fd");
        FileDescriptor binderFd = new FileDescriptor();
        binderFd.setInt$(session.binderFd);
        session.binderFd = -1; // Ownership moved to this wrapper, which closes it exactly once.
        try {
            ensureSocket();
            send(OP_BEGIN, session, binderFd);
        } finally {
            IoUtils.closeQuietly(binderFd);
        }
    }

    @Override
    public void end(MiMemEngineLaunch.Session session) throws IOException {
        send(OP_END, session, null);
    }

    @Override
    public void closeBinderFd(MiMemEngineLaunch.Session session) {
        int fdNumber = session.binderFd;
        if (fdNumber < 0) return;
        session.binderFd = -1;
        FileDescriptor binderFd = new FileDescriptor();
        binderFd.setInt$(fdNumber);
        IoUtils.closeQuietly(binderFd);
    }

    @Override
    public void disconnect() {
        LocalSocket socket = mSocket;
        mSocket = null;
        if (socket == null) return;
        try {
            socket.close();
        } catch (IOException ignored) {
        }
    }

    private void ensureSocket() throws IOException {
        if (mSocket != null) return;
        LocalSocket socket = new LocalSocket(LocalSocket.SOCKET_SEQPACKET);
        try {
            socket.connect(new LocalSocketAddress("mi_mem_engine",
                    LocalSocketAddress.Namespace.RESERVED));
            socket.setSoTimeout(SO_TIMEOUT_MS);
            if (socket.getPeerCredentials().getUid() != 0) {
                throw new IOException("untrusted mi_mem_engine peer");
            }
        } catch (IOException | RuntimeException e) {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
            throw e;
        }
        mSocket = socket;
    }

    private void send(int operation, MiMemEngineLaunch.Session session, FileDescriptor binderFd)
            throws IOException {
        ByteBuffer packet = ByteBuffer.allocate(PACKET_SIZE).order(ByteOrder.LITTLE_ENDIAN);
        packet.putInt(WIRE_VERSION).putInt(operation).putLong(session.cookie);
        packet.putInt(session.uid).putInt(LEASE_MS).putInt(session.pid).putInt(session.handle);
        mSocket.setFileDescriptorsForSend(binderFd == null ? null : new FileDescriptor[]{binderFd});
        try {
            mSocket.getOutputStream().write(packet.array());
        } finally {
            mSocket.setFileDescriptorsForSend(null);
        }
        // Every command receives a bounded status reply. No silent protocol drift.
        byte[] reply = new byte[REPLY_SIZE];
        if (mSocket.getInputStream().read(reply) != REPLY_SIZE
                || ByteBuffer.wrap(reply).order(ByteOrder.LITTLE_ENDIAN).getInt() != 0) {
            throw new IOException("mi_mem_engine request rejected");
        }
    }
}
