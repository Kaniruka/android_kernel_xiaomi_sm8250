# C06 warm/hot and cold production plan

Fixed producer baseline: AOSP frameworks/base android-15.0.0_r1; target kernel SM8250-family 4.19.325. Runtime is disabled by default. No Android build/device validation is available in this worktree.

## Public boundaries and design change

Real ActivityMetricsLogger launch and done/abort events drive a per-TransitionInfo cookie. A dedicated serial worker performs socket and JNI I/O outside the WM global lock. done atomically cancels pending work before queuing END. The worker CAS from pending to committed-BEGIN linearizes against cancellation: cancellation that wins suppresses BEGIN; an already committed in-flight BEGIN is followed by END and remains bounded by the 1000ms lease. This is the explicit in-flight allowance; no WM thread waits for an IPC operation. Coalesced launches replace the tracked record/session, and warm-to-cold restarts revoke it.

The contract originally named private Binder transport. This slice substitutes an init-created AF_UNIX SOCK_SEQPACKET socket. The root helper checks SO_PEERCRED UID 1000 and exact SELinux peer context system_server; policy restricts socket access to system_server. Producer checks server UID 0. SCM_RIGHTS transfers a duplicated system_server Binder driver descriptor. The helper alone holds the exclusive CAP_SYS_ADMIN kernel controller. Default-disabled init service and trusted `sys.mi_mem_engine.enabled` opt-in gate runtime use.

`MiMemEngineLaunch` now owns only the launch/attach state machine; `MiMemEngineSocketBackend` owns the JNI and socket transport and is the single production implementation of the `Backend` seam. The wire format, SCM_RIGHTS transfer, peer-UID check and JNI registration class are unchanged from 13a, so no kernel or helper change is involved.

## Cold pending to real attach

Pinned call order on android-15.0.0_r1 (ActivityStarter.execute 735/818, ActivityTaskSupervisor 790/828, RootWindowContainer 1872, ActivityTaskManagerService 6688):

- `notifyActivityLaunching` runs before request resolution, so a launch identity exists before any `TransitionInfo`.
- `notifyActivityLaunched` runs after `executeRequest`. A warm launch already went through `realStartActivityLocked`, so `associate` begins it directly from the live `WindowProcessController`; a cold launch has no process, so the launch stays pending bound to the exact `ActivityRecord` and commits a cookie only at its own BEGIN.
- `notifyBindApplication` (`preBindApplication`, AMS attachApplication) runs before the later attach and converts a warm launch whose process died into a cold one; the warm launch is cancelled and a fresh pending launch keeps coverage for the same record.
- `ActivityTaskSupervisor.realStartActivityLocked` calls `notifyLaunchProfileAttached(r, proc)` right after `r.setProcess(proc)`, for warm starts and for the cold `attachApplication` path. The adapter binds that process only when the record token matches, the launch is still pending, the expected process was not replaced and the UID is the record's application UID. Only the worker re-validates under the WM lock and captures the Binder identity.

`attached` claims a single attach per launch, so repeat hooks and a stale record cannot bind twice. `done`/`abort` remove the transition before a late attach can see it, and the cancelled state independently suppresses BEGIN. If the worker finds a replaced process or hosting record when it revalidates under the WM lock, it cancels the session: a claim with state 0 could never bind again. `end` sends exactly one END, and only for a cookie that reached the controller.

The controller cookie is not the launch identity. `pending()` allocates only a local `launch` value for logs, and the worker assigns the cookie from one static sequence at the BEGIN commit point, after the CAS, so cookies increase in actual submission order. This matters because the kernel rejects any BEGIN whose cookie is not strictly greater than `mme_last_cookie`; an older pending launch attaching after a newer warm BEGIN would otherwise be rejected and its error path would disconnect a healthy controller. A cookie consumed by a BEGIN that then fails is skipped, which the kernel accepts. `mi_mem_engine_helper.c` opens `/dev/mi_mem_engine` per accepted peer and closes it when that connection ends, and `mme_open` resets `mme_last_cookie`, so an in-process counter covers in-connection ordering without any persistent or cross-process sequence. `dupDriverFD` can return fd 0, so `-1` is the empty sentinel and `>= 0` is a valid descriptor everywhere.

## Exact identity

Do not convert /proc stat ticks to invented exact birth nanoseconds. A pidfd-open followed by IApplicationThread.pingBinder would also be insufficient: fixed AOSP attachApplication validates the caller PID/UID/startSeq but does not prove the supplied IApplicationThread Binder belongs to that caller. A process can supply a different living Binder.

JNI exports a dup of ProcessState's real Binder driver FD plus the remote IApplicationThread handle. The Java Session holds the original Binder strongly until the worker has finished, preventing handle reuse, including cancellation in flight. Kernel binder_mme_get_task validates the driver-file origin UID1000 (transport separately authenticates system_server SELinux), resolves that exact strong-reference node under Binder locks, obtains its owning task and checks expected WPC main TID/UID. MME pins struct pid and exact task birth time; PID reuse and a forged remote-thread owner both fail closed. No new public Binder ioctl is added. Native local/RPC Binders are rejected.

## Verification and limits

Pre-agreed seams are real ActivityMetricsLogger→helper socket→kernel controller and allocator trace; no fake Java model tests. Locally checked: the delivered patch applies cleanly to the pinned android-15.0.0_r1 bytes of the four touched files, `bash -n` on the scripts, and the focused cold-attach check `bash tests/cold_attach_test.sh` compiles the actual `MiMemEngineLaunch` with an explicit FIFO worker boundary and covers cold pending attach, done while prepare is queued, an older pending attach committing a strictly greater cookie after a live warm BEGIN without disconnecting it, late END of only its own cookie, in-flight cancellation, repeated attach, stale token/foreign UID/replaced identity, a hosting record replaced before revalidation, coalesced replacement, Binder death through the production recipient, warm regression, warm-to-cold handover and the default-off gate. Assertions compare cookies and launch identities, not just counts. Red was the pre-change adapter failing that seam. `MiMemEngineSocketBackend` was type-checked with a throwaway `/tmp` boundary only.

Android platform build (Soong, JNI link, hidden-API use), platform SELinux policy compilation and warm/cold launch→allocator→revocation E2E remain required downstream, together with the HyperOS mapping of the same launch/attach/done hooks. SurfaceFlinger input and the remaining scene inputs remain 13c. This is source integration for a modifiable fixed AOSP ROM, not evidence that stock HyperOS invokes it.
