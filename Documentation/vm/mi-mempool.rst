Order-zero/one/four/five allocator reserve (experimental)
========================================================

CONFIG_MI_MEMPOOL is built in, defaults to n, and requires 4 KiB ARM64 pages,
no memory hotplug and CONFIG_TRACEPOINTS: the reclaim-before gate is dropped
through the ``sched_process_exec`` and ``sched_process_exit`` tracepoints, so
the option does not exist without them. Runtime enabled defaults to 0. It is
an independent 4.19 adaptation of the observed mi_mempool failure channel,
using the GPL-2.0 Linux allocator, shrinker, kthread and sysfs APIs in this
tree. No donor C or unknown vendor page fields are copied. This implements
orders zero, one, four and five. Orders zero and one also expose a bounded
reclaim-before consumer for typed whitelisted identities; orders four and
five keep only their failure-after path.

Root can write 1 or 0 to /sys/kernel/mi_mempool/enabled. Enabling refuses a
reserve-owner conflict or capacity above total RAM / 512. Order zero has
high/low 1024/512 blocks (4 MiB maximum); order one has
high/low 128/64 contiguous two-page blocks (1 MiB maximum). The combined
limit is 7 MiB, including order four high/low 16/8 contiguous sixteen-page
blocks (1 MiB maximum) and order five high/low 8/4 contiguous thirty-two-page
blocks (1 MiB maximum). One sleeping worker fills
the local NORMAL zone. An empty reserve is a valid enabled state.
/sys/kernel/mi_mempool/stats reports current blocks/pages, high/low,
cumulative hits/misses, refill failures, pressure returns and stop drains.
Hits are cumulative loans, not outstanding references.

The only allocation consumer is an unsuccessful __alloc_pages_slowpath,
before the original memcg charge and allocation trace. It must be a
sleepable order-zero, order-one, order-four or order-five request with
DIRECT_RECLAIM, without DMA, DMA32,
HIGHMEM, MOVABLE, ATOMIC, MEMALLOC or NOFAIL. Interrupt/atomic contexts,
PF_MEMALLOC and the refill worker are rejected. The preferred allocation
zone must be NORMAL and satisfy the allocation context's high_zoneidx,
nodemask and current cpuset. Only a reserve page from that exact zone can
be returned. Other requests retain the original failure result. ZERO and
init-on-alloc initialization run before the original memcg charge; charge
failure uses ordinary __free_pages with the original order. Higher-order
ZERO/init clears every base page (two, sixteen or thirty-two); caller COMP
constructs compound state once after leaving the pool. Without COMP the block stays non-compound.
Fast-path success is unchanged.

Stored pages are ordinary, prepared, refcount-one, uncharged, non-compound
allocations. Only pool ownership uses page->lru. Borrowing removes the list
entry and decrements NR_KERNEL_MISC_RECLAIMABLE under the pool lock. It
never repeats prep_new_page, changes the raw refcount or changes buddy free
statistics. The original page-owner allocation site remains the refill
site. A loan permanently leaves the reserve and follows normal free rules.

Refill removes DIRECT_RECLAIM, COMP and ACCOUNT from
GFP_KERNEL|NORETRY|NOWARN: no direct
reclaim or OOM is requested for refill. Before each allocation, the chosen
zone must exceed its high watermark plus the entire remaining reserve
budget. A failed allocation/watermark check delays further attempts by
300 ms. The post-allocation lock checks enabled, generation, capacity,
zone and pfmemalloc before accepting a page; a rejected page is freed.
Normal free_unref_page completion merely sends a throttled wakeup; it
never gives a page to the reserve.

The NUMA-aware, non-memcg-aware shrinker counts and returns only stored
pages, in base-page units. Higher-order blocks are indivisible, so a scan
can exceed its budget by up to thirty-one base pages. Orders two and three
have no pool and retain the ordinary allocation failure result. Stats
pressure_returned and stop_drain also use base-page units; high/low use
blocks. A pressure scan invalidates an in-progress
refill allocation and suppresses refill for at least 300 ms; the high
watermark is checked again thereafter. Detached pages are freed outside
the pool spinlock. No borrowed page is retained or reclaimed by the pool.

Control writes are serialized by a mutex. The shared allocator reserve
arbiter is acquired before startup and retained through complete drain;
C22 must use the same arbiter. Startup unwinds shrinker registration and
ownership in reverse order on failure. Shutdown closes the borrowing and
refill gate under the list lock, unregisters/synchronizes the shrinker,
stops the worker, then drains the list and releases ownership. The pool,
queue and counters are static built-in objects: borrowers finish list and
accounting work before releasing that same lock and never hold a pointer
to destructible pool metadata. Shutdown does not wait for ordinary users
to free pages already handed out. Runtime readers use static metadata;
stats take the pool lock for a coherent snapshot.

Object compilation and host reserve-owner checks do not establish actual
allocator, cpuset migration, memcg rejection, OOM, pressure, concurrent
shutdown or performance behavior. Target-device controlled allocation and
memory-pressure validation is required before enabling this experiment.

Reclaim-before consumer (orders zero and one)
---------------------------------------------

The early consumer runs in the same validated preferred-zone, nodemask and
cpuset block that already precedes ``__alloc_pages_direct_reclaim()``, after
the C22 consumer and before direct reclaim. A hit returns through the
slowpath ``got_pg`` label and still passes the original outer memcg charge,
charge-failure free and allocation trace. A miss changes nothing, so the
original direct reclaim runs. Fast-path success and the failure-after
consumer are untouched.

Eligibility is a typed target policy, not a copy of any donor source. Exactly
seven TGID-leader ``TASK_COMM`` values are recognised: ``composer`` and
``surfaceflinger`` (``oom_score_adj`` exactly -1000), ``system_server``
(exactly -900), and ``com.miui.home``, ``ndroid.systemui``,
``.globallauncher`` and ``droid.bluetooth`` with no adj requirement. The adj
check is an exact match, not a ceiling, because the donor records exact
values for the three restricted classes. ``app_main`` is deliberately
not eligible. Full comm comparison replaces donor prefix matching. Up to five
additional TGIDs are explicit root grants rather than numeric allocator
lookups. Only a live userspace caller with an ``mm`` may enter; kernel
threads, ``PF_EXITING``, ``PF_MEMALLOC``, fatal signals, nested
``reclaim_state``, interrupt/atomic/IRQ-disabled contexts, non-DIRECT_RECLAIM
and the privileged GFP/zone set are rejected. This new path additionally
rejects ``__GFP_HIGH``; the accepted failure-after mask is unchanged by that
constraint.

The consumer is inert while the pool is disabled, which is the default: it
returns before sampling a TGID, taking a pid reference, reading pressure
counters or updating any statistic, so a disabled feature costs one flag read
on the slow path.

Blocks, not base pages, drive the floors. Order zero has strict process
floors of 71 blocks for ordinary classes and 153 for ``system_server``; order
one uses 102 for every class. Direct floors are 272 blocks for order zero
(scaled from the 64 MiB reference) and 128 for order one. Orders four and
five have floors equal to their high capacity, so they cannot early-borrow
and keep their failure-after path.

The pressure sampler reads the allocation's preferred NORMAL zone and its
node. Node active plus inactive file pages, plus active plus inactive
anonymous pages only while swap exists, must be nonzero. Available pages are
the zone free count minus ``NR_FREE_CMA_PAGES`` and
``nr_reserved_highatomic``, saturated at zero. Above 11 MiB of available pages
a loan requires the count to be strictly above both the process floor and the
direct floor; at or below 11 MiB the direct floor is waived, as in the
confirmed low-free branch, and only the strict process floor applies. Order
one can therefore borrow only above 102 blocks, and a high-free order-one
pool can never borrow. This is a conservative typed 4.19 sampling difference;
it does not claim byte-equivalent donor throttling or diagnostics.

A fixed 32-entry cache keys a referenced ``struct pid`` TGID and stores the
sampled leader comm, shared ``oom_score_adj`` and leader exec id together
with the last class for reporting. Every attempt resamples and recomputes
classification, so a changed identity is rewritten rather than trusted, and
cache exhaustion fails closed. Each attempt and every configuration
publication rechecks under the pool lock, through the already referenced
``struct pid``, that the TGID is still the live group leader of an
exec-generation match. A sample that raced an exec or exit callback, or a
stop, cannot reinsert itself or take a page. Fork has a distinct pid object
and classifies anew. No numeric PID lookup, task lock or allocation happens
under the pool lock; references are detached there and ``put_pid()`` runs
outside.

A slot is never a qualification lease. When a resampled identity no longer
classifies and holds no grant, the entry is detached under the lock and its
reference is dropped outside, so a process that changes its comm or adj can
neither keep a slot occupied nor block a newly eligible identity. Publishing
a replacement or empty grant set also detaches every entry whose recorded
class came only from a grant; those identities must re-enter through a fresh
attempt.

Built-in ``sched_process_exec`` and ``sched_process_exit`` callbacks drop the
cache entry and any explicit grant for the TGID. Exec always drops; exit
drops only when the group is dead (``signal->live`` is zero), so one thread
leaving or an early leader exit keeps the identity. Disabling clears the
cache and all grants after the existing drain and before releasing the
allocator owner, and the callbacks never recreate state afterwards.

Initialisation registers both lifecycle probes before any control attribute
is published. A failed registration returns an error with no writable node,
no worker and no owner; a later kobject or sysfs failure unregisters both
probes in reverse order and waits for in-flight callbacks with
``tracepoint_synchronize_unregister()`` before giving up. The pool therefore
cannot be enabled while the exec/exit gate is missing.

``sysfs_create_group()`` publishes its attributes one at a time, so the group
is not an atomic publication. Both stores therefore take a separate
``init_ready`` flag under the control mutex and return ``-ENODEV`` while it is
clear, including for a write that would otherwise be a no-op. The flag is set
only after the whole group was created, so a write racing the publication, or
arriving after a failed one, cannot start the worker or take the allocator
owner.

``/sys/kernel/mi_mempool/pids`` is writable only by a task with
``CAP_SYS_ADMIN`` in the initial user namespace. A write is a complete
replacement of up to five positive decimal initial-namespace TGIDs separated
by whitespace; a single ``0`` clears. Negative numbers, extra or duplicate
IDs, trailing junk, TIDs, dead targets and targets without an ``mm`` are
rejected, and a rejected write leaves the previous configuration in place.
The attribute requires an enabled pool. ``stats`` reports early hits and each
denial reason, the current cache and grant counts, and keeps the cumulative
per-order loan counters.
