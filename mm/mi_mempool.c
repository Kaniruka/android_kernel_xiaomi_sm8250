// SPDX-License-Identifier: GPL-2.0
/* Independent dagu 4.19 adaptation; ordinary prepared, refcount-one pages. */
#include <linux/mi_mempool.h>
#include <linux/mm.h>
#include <linux/mmzone.h>
#include <linux/vmstat.h>
#include <linux/kthread.h>
#include <linux/shrinker.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/mutex.h>
#include <linux/wait.h>
#include <linux/sched.h>
#include <linux/interrupt.h>
#include <linux/atomic.h>
#include <linux/swap.h>
#include <linux/pid.h>
#include <linux/capability.h>
#include <linux/user_namespace.h>
#include <linux/binfmts.h>
#include <trace/events/sched.h>

#define RETRY_DELAY msecs_to_jiffies(300)
static DEFINE_MUTEX(control_lock);
static DEFINE_SPINLOCK(pool_lock);
struct reserve_pool {
	struct list_head pages;
	unsigned int order, high, low, count;
	unsigned long hits, misses, alloc_failures, pressure_returned, stop_drain;
};

static struct reserve_pool pools[] = {
	{ .pages = LIST_HEAD_INIT(pools[0].pages),
	  .order = 0, .high = 1024, .low = 512 },
	{ .pages = LIST_HEAD_INIT(pools[1].pages),
	  .order = 1, .high = 128, .low = 64 },
	{ .pages = LIST_HEAD_INIT(pools[2].pages),
	  .order = 4, .high = 16, .low = 8 },
	{ .pages = LIST_HEAD_INIT(pools[3].pages),
	  .order = 5, .high = 8, .low = 4 },
};
static DECLARE_WAIT_QUEUE_HEAD(refill_wait);
static struct task_struct *refill_task;
static bool enabled, init_ready;
static unsigned long retry_after, next_wake;
static unsigned long generation;

/* Only called with pool_lock held; ownership and vmstat move together. */
static struct page *take_page(struct reserve_pool *pool, struct page *page)
{
	list_del_init(&page->lru);
	pool->count--;
	__mod_node_page_state(page_pgdat(page), NR_KERNEL_MISC_RECLAIMABLE,
			      -(1L << pool->order));
	return page;
}

struct page *mi_mempool_borrow(gfp_t gfp, unsigned int order, struct zone *zone)
{
	struct page *page, *found = NULL;
	struct reserve_pool *pool = NULL;
	unsigned int i;
	unsigned long flags;

	if (!zone || zone_idx(zone) != ZONE_NORMAL ||
	    !(gfp & __GFP_DIRECT_RECLAIM) ||
	    (gfp & (__GFP_DMA | __GFP_DMA32 | __GFP_HIGHMEM | __GFP_MOVABLE |
		    __GFP_ATOMIC | __GFP_MEMALLOC | __GFP_NOFAIL)) ||
	    in_interrupt() || irqs_disabled() || in_atomic() ||
	    (current->flags & PF_MEMALLOC) || current == READ_ONCE(refill_task))
		return NULL;
	/* Supported orders are sparse; an order is never an array index. */
	for (i = 0; i < ARRAY_SIZE(pools); i++)
		if (pools[i].order == order) {
			pool = &pools[i];
			break;
		}
	if (!pool)
		return NULL;
	spin_lock_irqsave(&pool_lock, flags);
	if (enabled) {
		list_for_each_entry(page, &pool->pages, lru) {
			if (page_zone(page) == zone) {
				found = take_page(pool, page);
				pool->hits++;
				break;
			}
		}
		if (!found)
			pool->misses++;
	}
	spin_unlock_irqrestore(&pool_lock, flags);
	mi_mempool_free_event();
	return found;
}

/* Counts are blocks; all reclaim and vmstat quantities are base pages. */
static bool below_low(void)
{
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(pools); i++)
		if (READ_ONCE(pools[i].count) < pools[i].low)
			return true;
	return false;
}

static unsigned long remaining_pages(void)
{
	unsigned long nr = 0;
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(pools); i++)
		nr += (pools[i].high - pools[i].count) << pools[i].order;
	return nr;
}

void mi_mempool_free_event(void)
{
	unsigned long flags;

	/* A notification only: never inspect or retain a freed page. */
	if (!READ_ONCE(enabled))
		return;
	spin_lock_irqsave(&pool_lock, flags);
	if (enabled && below_low() &&
	    time_after_eq(jiffies, retry_after) &&
	    time_after_eq(jiffies, next_wake)) {
		next_wake = jiffies + RETRY_DELAY;
		wake_up(&refill_wait);
	}
	spin_unlock_irqrestore(&pool_lock, flags);
}

/*
 * C21 reclaim-before policy.
 *
 * Orders zero and one may borrow prepared reserve pages before direct
 * reclaim. Orders four and five keep their accepted failure-after path: their
 * strict process floors equal their high capacity, so no reachable early
 * branch exists and no separate high-order algorithm is introduced.
 */
#define C21_CACHE_ENTRIES	32
#define C21_GRANT_SLOTS		5
#define C21_AVAILABLE_HIGH	(11UL << (20 - PAGE_SHIFT))

enum c21_class_id {
	C21_CLASS_NONE = 0,
	C21_CLASS_COMPOSER = 1,
	C21_CLASS_SURFACEFLINGER = 2,
	C21_CLASS_MIUI_HOME = 3,
	C21_CLASS_SYSTEMUI = 4,
	C21_CLASS_SYSTEM_SERVER = 5,
	C21_CLASS_GLOBALLAUNCHER = 6,
	C21_CLASS_BLUETOOTH = 7,
	C21_CLASS_GRANT = 8,
};

static bool c21_comm_eq(const char *a, const char *b)
{
	unsigned int i;

	for (i = 0; i < TASK_COMM_LEN; i++) {
		if (a[i] != b[i])
			return false;
		if (!a[i])
			return true;
	}
	return false;
}

/*
 * Exact full TASK_COMM matching is a conservative target difference from the
 * donor's prefix matching. app_main is deliberately not eligible. Where the
 * donor omits the adj check the class is unrestricted; elsewhere the sampled
 * oom_score_adj must equal the frozen value exactly.
 */
static unsigned char c21_classify(const char *comm, int oom_score_adj)
{
	static const struct {
		const char *comm;
		int adj;
		bool check_adj;
		unsigned char class_id;
	} table[] = {
		{ "composer", -1000, true, C21_CLASS_COMPOSER },
		{ "surfaceflinger", -1000, true, C21_CLASS_SURFACEFLINGER },
		{ "com.miui.home", 0, false, C21_CLASS_MIUI_HOME },
		{ "ndroid.systemui", 0, false, C21_CLASS_SYSTEMUI },
		{ "system_server", -900, true, C21_CLASS_SYSTEM_SERVER },
		{ ".globallauncher", 0, false, C21_CLASS_GLOBALLAUNCHER },
		{ "droid.bluetooth", 0, false, C21_CLASS_BLUETOOTH },
	};
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(table); i++) {
		if (!c21_comm_eq(comm, table[i].comm))
			continue;
		if (table[i].check_adj && oom_score_adj != table[i].adj)
			return C21_CLASS_NONE;
		return table[i].class_id;
	}
	return C21_CLASS_NONE;
}

/* Strict process floors in blocks at the fixed 7 MiB target capacity. */
static unsigned int c21_process_floor(unsigned char class_id,
				      unsigned int order)
{
	static const struct {
		unsigned int order;
		unsigned int ordinary;
		unsigned int system_server;
	} table[] = {
		{ 0, 71, 153 },
		{ 1, 102, 102 },
		{ 4, 16, 16 },
		{ 5, 8, 8 },
	};
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(table); i++)
		if (table[i].order == order)
			return class_id == C21_CLASS_SYSTEM_SERVER ?
			       table[i].system_server : table[i].ordinary;
	return 0;
}

/* Strict direct floors; order zero scales the 64 MiB reference 17/64. */
static unsigned int c21_direct_floor(unsigned int order)
{
	static const struct {
		unsigned int order;
		unsigned int floor;
	} table[] = {
		{ 0, 272 },
		{ 1, 128 },
		{ 4, 16 },
		{ 5, 8 },
	};
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(table); i++)
		if (table[i].order == order)
			return table[i].floor;
	return 0;
}

/*
 * Above the available-page threshold the direct floor also applies; at or
 * below it the confirmed low-free branch waives that floor and keeps only the
 * strict process floor. Both comparisons are strict.
 */
static bool c21_floor_allows(unsigned char class_id, unsigned int order,
			     unsigned int count, unsigned long available)
{
	if (count <= c21_process_floor(class_id, order))
		return false;
	if (available > C21_AVAILABLE_HIGH)
		return count > c21_direct_floor(order);
	return true;
}

/*
 * Typed 4.19 sampling difference: node file pages, plus anonymous pages only
 * while swap exists, must be nonzero; available pages subtract protected CMA
 * and highatomic reserves from the exact eligible zone.
 */
static bool c21_pressure_sample(struct zone *zone, unsigned long *available)
{
	unsigned long file, anon = 0, free, reserved;

	/* The non-NUMA node_page_state() macro discards its node argument. */
	file = node_page_state(zone->zone_pgdat, NR_ACTIVE_FILE) +
	       node_page_state(zone->zone_pgdat, NR_INACTIVE_FILE);
	if (get_nr_swap_pages() > 0)
		anon = node_page_state(zone->zone_pgdat, NR_ACTIVE_ANON) +
		       node_page_state(zone->zone_pgdat, NR_INACTIVE_ANON);
	if (!file && !anon)
		return false;
	reserved = zone_page_state(zone, NR_FREE_CMA_PAGES) +
		   READ_ONCE(zone->nr_reserved_highatomic);
	free = zone_page_state(zone, NR_FREE_PAGES);
	*available = free > reserved ? free - reserved : 0;
	return true;
}

/*
 * Cached identity: a referenced struct pid keeps the TGID object stable, and
 * the entry never grants qualification by itself. Every attempt resamples and
 * recomputes classification; a changed comm, oom_score_adj or exec id rewrites
 * the entry instead of trusting the recorded class.
 */
struct c21_ident {
	struct pid *tgid;
	char comm[TASK_COMM_LEN];
	int oom_score_adj;
	u64 exec_id;
};

struct c21_cache_entry {
	struct pid *tgid;
	char comm[TASK_COMM_LEN];
	int oom_score_adj;
	u64 exec_id;
	unsigned char class_id;
};

static struct c21_cache_entry c21_cache[C21_CACHE_ENTRIES];
static struct pid *c21_grants[C21_GRANT_SLOTS];
static atomic_long_t c21_early_hits;
static atomic_long_t c21_early_denied_context;
static atomic_long_t c21_early_denied_state;
static atomic_long_t c21_early_denied_identity;
static atomic_long_t c21_early_denied_pressure;
static atomic_long_t c21_early_denied_cache;
static atomic_long_t c21_early_denied_floor;
static atomic_long_t c21_early_denied_pool;

/*
 * Sample the caller's TGID identity before pool_lock. The leader comm and the
 * leader exec id identify the group; oom_score_adj is shared. No lock other
 * than task_lock is taken here and nothing is allocated.
 */
static bool c21_take_ident(struct c21_ident *ident)
{
	struct task_struct *leader = current->group_leader;

	ident->tgid = get_task_pid(current, PIDTYPE_TGID);
	if (!ident->tgid)
		return false;
	task_lock(leader);
	memcpy(ident->comm, leader->comm, TASK_COMM_LEN);
	ident->exec_id = leader->self_exec_id;
	task_unlock(leader);
	ident->comm[TASK_COMM_LEN - 1] = '\0';
	ident->oom_score_adj = READ_ONCE(current->signal->oom_score_adj);
	return true;
}

/*
 * Final lifecycle recheck, only with pool_lock held and only through the
 * already referenced struct pid: no numeric PID lookup, task lock or
 * allocation happens here. A leader that exited early keeps the group alive
 * for its remaining threads, so group liveness is checked rather than the
 * leader's own mm.
 */
static bool c21_tgid_live(struct pid *tgid, u64 exec_id)
{
	struct task_struct *leader;
	bool live = false;

	rcu_read_lock();
	leader = pid_task(tgid, PIDTYPE_PID);
	if (leader && leader->group_leader == leader && leader->signal &&
	    atomic_read(&leader->signal->live) &&
	    READ_ONCE(leader->self_exec_id) == exec_id)
		live = true;
	rcu_read_unlock();
	return live;
}

static struct c21_cache_entry *c21_cache_find_locked(struct pid *tgid)
{
	unsigned int i;

	for (i = 0; i < C21_CACHE_ENTRIES; i++)
		if (c21_cache[i].tgid == tgid)
			return &c21_cache[i];
	return NULL;
}

/* Detach one slot; the caller drops the returned reference outside the lock. */
static struct pid *c21_cache_drop_locked(struct pid *tgid)
{
	struct c21_cache_entry *entry = c21_cache_find_locked(tgid);
	struct pid *drop;

	if (!entry)
		return NULL;
	drop = entry->tgid;
	memset(entry, 0, sizeof(*entry));
	return drop;
}

/*
 * Capacity exhaustion fails closed. On insertion the attempt's pid reference
 * moves into the cache, so the caller must not put it again.
 */
static struct c21_cache_entry *c21_cache_store_locked(struct c21_ident *ident,
						     unsigned char class_id)
{
	struct c21_cache_entry *entry = c21_cache_find_locked(ident->tgid);
	unsigned int i;

	if (!entry) {
		for (i = 0; i < C21_CACHE_ENTRIES; i++)
			if (!c21_cache[i].tgid) {
				entry = &c21_cache[i];
				break;
			}
		if (!entry)
			return NULL;
		entry->tgid = ident->tgid;
		ident->tgid = NULL;
	}
	memcpy(entry->comm, ident->comm, TASK_COMM_LEN);
	entry->oom_score_adj = ident->oom_score_adj;
	entry->exec_id = ident->exec_id;
	entry->class_id = class_id;
	return entry;
}

static bool c21_grant_locked(struct pid *tgid)
{
	unsigned int i;

	for (i = 0; i < C21_GRANT_SLOTS; i++)
		if (c21_grants[i] == tgid)
			return true;
	return false;
}

/*
 * Reclaim-before consumer. Returns a prepared pool page after removing it
 * under pool_lock, or NULL to keep the original direct reclaim path.
 */
struct page *mi_mempool_early_borrow(gfp_t gfp, unsigned int order,
				     struct zone *zone)
{
	struct c21_ident ident = { };
	struct c21_cache_entry *entry;
	struct reserve_pool *pool = NULL;
	struct page *page, *found = NULL;
	struct pid *drop = NULL;
	unsigned long flags, available = 0, epoch;
	unsigned char class_id;
	unsigned int i;

	/* Disabled is the default: return before any TGID or pressure sampling. */
	if (!READ_ONCE(enabled))
		return NULL;
	/*
	 * __GFP_HIGH is a conservative additional rejection for this new path
	 * only; the accepted failure-after mask is unchanged.
	 */
	if (!zone || zone_idx(zone) != ZONE_NORMAL ||
	    !(gfp & __GFP_DIRECT_RECLAIM) ||
	    (gfp & (__GFP_DMA | __GFP_DMA32 | __GFP_HIGHMEM | __GFP_MOVABLE |
		    __GFP_ATOMIC | __GFP_MEMALLOC | __GFP_NOFAIL | __GFP_HIGH)) ||
	    in_interrupt() || irqs_disabled() || in_atomic() ||
	    (current->flags & PF_MEMALLOC) || current == READ_ONCE(refill_task))
		return NULL;
	for (i = 0; i < ARRAY_SIZE(pools); i++)
		if (pools[i].order == order) {
			pool = &pools[i];
			break;
		}
	if (!pool)
		return NULL;
	if ((current->flags & (PF_KTHREAD | PF_EXITING)) || !current->mm ||
	    current->reclaim_state || fatal_signal_pending(current)) {
		atomic_long_inc(&c21_early_denied_context);
		return NULL;
	}
	/*
	 * Sample the lifecycle generation before the identity snapshot: a stop
	 * or pressure scan that lands during the snapshot must invalidate it.
	 */
	epoch = READ_ONCE(generation);
	if (!c21_take_ident(&ident))
		return NULL;
	class_id = c21_classify(ident.comm, ident.oom_score_adj);
	if (!c21_pressure_sample(zone, &available)) {
		atomic_long_inc(&c21_early_denied_pressure);
		put_pid(ident.tgid);
		return NULL;
	}
	spin_lock_irqsave(&pool_lock, flags);
	if (!enabled || epoch != generation || time_before(jiffies, retry_after)) {
		atomic_long_inc(&c21_early_denied_state);
		goto unlock;
	}
	/*
	 * A sample taken before an exec/exit callback or a stop must never
	 * reinsert itself or take a page afterwards.
	 */
	if (!c21_tgid_live(ident.tgid, ident.exec_id) || !current->mm ||
	    (current->flags & PF_EXITING)) {
		atomic_long_inc(&c21_early_denied_state);
		goto unlock;
	}
	if (!class_id) {
		if (!c21_grant_locked(ident.tgid)) {
			atomic_long_inc(&c21_early_denied_identity);
			/* A reclassified or revoked identity must not hold a slot. */
			drop = c21_cache_drop_locked(ident.tgid);
			goto unlock;
		}
		class_id = C21_CLASS_GRANT;
	}
	entry = c21_cache_store_locked(&ident, class_id);
	if (!entry) {
		atomic_long_inc(&c21_early_denied_cache);
		goto unlock;
	}
	if (!c21_floor_allows(class_id, order, pool->count, available)) {
		atomic_long_inc(&c21_early_denied_floor);
		goto unlock;
	}
	list_for_each_entry(page, &pool->pages, lru) {
		if (page_zone(page) == zone) {
			found = take_page(pool, page);
			pool->hits++;
			atomic_long_inc(&c21_early_hits);
			break;
		}
	}
	if (!found)
		atomic_long_inc(&c21_early_denied_pool);
unlock:
	spin_unlock_irqrestore(&pool_lock, flags);
	/* put_pid() may free a struct pid; it never runs under pool_lock. */
	put_pid(drop);
	put_pid(ident.tgid);
	if (found)
		mi_mempool_free_event();
	return found;
}

/* Detach one TGID from the cache and the explicit grants. */
static void c21_drop_tgid(struct pid *tgid)
{
	struct pid *cache_drop = NULL, *grant_drop = NULL;
	unsigned long flags;
	unsigned int i;

	spin_lock_irqsave(&pool_lock, flags);
	for (i = 0; i < C21_CACHE_ENTRIES; i++)
		if (c21_cache[i].tgid == tgid) {
			cache_drop = c21_cache[i].tgid;
			memset(&c21_cache[i], 0, sizeof(c21_cache[i]));
			break;
		}
	for (i = 0; i < C21_GRANT_SLOTS; i++)
		if (c21_grants[i] == tgid) {
			grant_drop = c21_grants[i];
			c21_grants[i] = NULL;
			break;
		}
	spin_unlock_irqrestore(&pool_lock, flags);
	put_pid(cache_drop);
	put_pid(grant_drop);
}

static void c21_process_exec(void *data, struct task_struct *task,
			     pid_t old_pid, struct linux_binprm *bprm)
{
	struct pid *tgid;

	if (!READ_ONCE(enabled))
		return;
	tgid = get_task_pid(task, PIDTYPE_TGID);
	if (!tgid)
		return;
	c21_drop_tgid(tgid);
	put_pid(tgid);
}

static void c21_process_exit(void *data, struct task_struct *task)
{
	struct pid *tgid;

	if (!READ_ONCE(enabled) || !task->signal)
		return;
	/* A single thread leaving keeps the TGID identity; only group death drops it. */
	if (atomic_read(&task->signal->live))
		return;
	tgid = get_task_pid(task, PIDTYPE_TGID);
	if (!tgid)
		return;
	c21_drop_tgid(tgid);
	put_pid(tgid);
}

static int c21_register_probes(void)
{
	int ret;

	ret = register_trace_sched_process_exec(c21_process_exec, NULL);
	if (ret)
		return ret;
	ret = register_trace_sched_process_exit(c21_process_exit, NULL);
	if (ret)
		unregister_trace_sched_process_exec(c21_process_exec, NULL);
	return ret;
}

/* Complete reverse order, with a barrier for callbacks already in flight. */
static void c21_unregister_probes(void)
{
	unregister_trace_sched_process_exit(c21_process_exit, NULL);
	unregister_trace_sched_process_exec(c21_process_exec, NULL);
	tracepoint_synchronize_unregister();
}

static bool c21_is_space(char c)
{
	return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

/*
 * A complete replacement: whitespace separated positive decimal
 * initial-namespace TGIDs, at most C21_GRANT_SLOTS of them. A single literal
 * zero clears. Anything else, including negatives, duplicates and trailing
 * junk, is rejected so the old configuration survives.
 */
static int c21_parse_pids(const char *buf, size_t len, int *ids,
			  unsigned int *nr)
{
	const char *p = buf, *end = buf + len;
	unsigned int i, j;

	*nr = 0;
	while (p < end) {
		unsigned long value = 0;

		while (p < end && c21_is_space(*p))
			p++;
		if (p == end)
			break;
		if (*p < '0' || *p > '9')
			return -EINVAL;
		while (p < end && *p >= '0' && *p <= '9') {
			value = value * 10 + (*p - '0');
			if (value > INT_MAX)
				return -EINVAL;
			p++;
		}
		if (p < end && !c21_is_space(*p))
			return -EINVAL;
		if (*nr == C21_GRANT_SLOTS)
			return -EINVAL;
		ids[(*nr)++] = value;
	}
	if (!*nr)
		return -EINVAL;
	for (i = 0; i < *nr; i++) {
		if (!ids[i])
			return *nr == 1 ? 1 : -EINVAL;
		for (j = 0; j < i; j++)
			if (ids[j] == ids[i])
				return -EINVAL;
	}
	return 0;
}

/*
 * Resolve one initial-namespace TGID and take a pid reference. TIDs, dead and
 * mm-less targets are rejected; nothing is retained on failure.
 */
static int c21_resolve_tgid(int nr, struct pid **out, u64 *exec_id)
{
	struct task_struct *task;
	struct pid *pid;
	bool ok = false;

	rcu_read_lock();
	pid = find_pid_ns(nr, &init_pid_ns);
	if (pid)
		get_pid(pid);
	rcu_read_unlock();
	if (!pid)
		return -ESRCH;
	rcu_read_lock();
	task = pid_task(pid, PIDTYPE_PID);
	if (task && task->group_leader == task &&
	    !(READ_ONCE(task->flags) & PF_EXITING) && READ_ONCE(task->mm)) {
		*exec_id = READ_ONCE(task->self_exec_id);
		ok = true;
	}
	rcu_read_unlock();
	if (!ok) {
		put_pid(pid);
		return -ESRCH;
	}
	*out = pid;
	return 0;
}

/*
 * Publish the replacement grant set under pool_lock. Revalidating there keeps
 * an exit/exec callback that already ran from being undone by a delayed
 * configuration write; old references are dropped outside the lock.
 */
static int c21_publish_grants(struct pid **new, const u64 *exec_ids,
			      unsigned int nr)
{
	struct pid *old[C21_GRANT_SLOTS + C21_CACHE_ENTRIES];
	unsigned long flags;
	unsigned int i, drops = 0;

	spin_lock_irqsave(&pool_lock, flags);
	for (i = 0; i < nr; i++)
		if (!c21_tgid_live(new[i], exec_ids[i])) {
			spin_unlock_irqrestore(&pool_lock, flags);
			return -EAGAIN;
		}
	for (i = 0; i < C21_GRANT_SLOTS; i++)
		if (c21_grants[i])
			old[drops++] = c21_grants[i];
	/*
	 * A replaced or cleared grant must not leave an entry that was admitted
	 * only by the old grant: it would keep both a cache slot and a reference.
	 */
	for (i = 0; i < C21_CACHE_ENTRIES; i++)
		if (c21_cache[i].tgid && c21_cache[i].class_id == C21_CLASS_GRANT) {
			old[drops++] = c21_cache[i].tgid;
			memset(&c21_cache[i], 0, sizeof(c21_cache[i]));
		}
	for (i = 0; i < C21_GRANT_SLOTS; i++)
		c21_grants[i] = i < nr ? new[i] : NULL;
	spin_unlock_irqrestore(&pool_lock, flags);
	while (drops)
		put_pid(old[--drops]);
	return 0;
}

/* Disable must not leave a cached or explicitly granted identity behind. */
static void c21_clear_identities(void)
{
	struct pid *drops[C21_CACHE_ENTRIES + C21_GRANT_SLOTS];
	unsigned long flags;
	unsigned int i, nr = 0;

	spin_lock_irqsave(&pool_lock, flags);
	for (i = 0; i < C21_CACHE_ENTRIES; i++) {
		if (c21_cache[i].tgid)
			drops[nr++] = c21_cache[i].tgid;
		memset(&c21_cache[i], 0, sizeof(c21_cache[i]));
	}
	for (i = 0; i < C21_GRANT_SLOTS; i++) {
		if (c21_grants[i])
			drops[nr++] = c21_grants[i];
		c21_grants[i] = NULL;
	}
	spin_unlock_irqrestore(&pool_lock, flags);
	while (nr)
		put_pid(drops[--nr]);
}

static int refill(void *unused)
{
	gfp_t gfp = (GFP_KERNEL | __GFP_NORETRY | __GFP_NOWARN) &
		    ~(__GFP_DIRECT_RECLAIM | __GFP_COMP | __GFP_ACCOUNT);
	struct zone *zone = &NODE_DATA(numa_node_id())->node_zones[ZONE_NORMAL];

	while (!kthread_should_stop()) {
		struct page *page;
		unsigned long flags, epoch;
		unsigned long remaining;
		unsigned int i;

		wait_event_interruptible_timeout(refill_wait,
			kthread_should_stop() || (READ_ONCE(enabled) &&
			below_low() &&
			time_after_eq(jiffies, READ_ONCE(retry_after))), RETRY_DELAY);
		if (kthread_should_stop())
			break;
		if (!below_low())
			continue;
		for (i = 0; i < ARRAY_SIZE(pools); i++) {
			struct reserve_pool *pool = &pools[i];

			for (;;) {
				spin_lock_irqsave(&pool_lock, flags);
				remaining = remaining_pages();
				epoch = generation;
				if (!enabled || pool->count == pool->high ||
				    time_before(jiffies, retry_after)) {
					spin_unlock_irqrestore(&pool_lock, flags);
					break;
				}
				spin_unlock_irqrestore(&pool_lock, flags);
				/* Reserve the entire remaining budget above high watermark. */
				if (!zone_watermark_ok(zone, pool->order,
						       high_wmark_pages(zone) + remaining,
						       ZONE_NORMAL, 0))
					page = NULL;
				else
					page = alloc_pages_node(zone_to_nid(zone), gfp,
							pool->order);
				spin_lock_irqsave(&pool_lock, flags);
				if (!page) {
					pool->alloc_failures++;
					retry_after = jiffies + RETRY_DELAY;
					spin_unlock_irqrestore(&pool_lock, flags);
					break;
				}
				if (enabled && epoch == generation && pool->count < pool->high &&
				    page_zone(page) == zone && !page_is_pfmemalloc(page)) {
					list_add(&page->lru, &pool->pages);
					pool->count++;
					__mod_node_page_state(page_pgdat(page),
						NR_KERNEL_MISC_RECLAIMABLE, 1L << pool->order);
					page = NULL;
				}
				spin_unlock_irqrestore(&pool_lock, flags);
				if (page) {
					__free_pages(page, pool->order);
					break;
				}
				cond_resched();
			}
		}
	}
	return 0;
}

static unsigned long pool_count(struct shrinker *s, struct shrink_control *sc)
{
	struct page *page;
	unsigned int i;
	unsigned long flags, nr = 0;

	spin_lock_irqsave(&pool_lock, flags);
	if (enabled)
		for (i = 0; i < ARRAY_SIZE(pools); i++) {
			struct reserve_pool *pool = &pools[i];

			list_for_each_entry(page, &pool->pages, lru)
				if (sc->nid == NUMA_NO_NODE || page_to_nid(page) == sc->nid)
					nr += 1UL << pool->order;
		}
	spin_unlock_irqrestore(&pool_lock, flags);
	return nr ? nr : SHRINK_EMPTY;
}

static unsigned long pool_scan(struct shrinker *s, struct shrink_control *sc)
{
	struct page *page, *next;
	unsigned int i;
	unsigned long flags, nr = 0;

	spin_lock_irqsave(&pool_lock, flags);
	generation++;
	retry_after = jiffies + RETRY_DELAY;
	for (i = 0; i < ARRAY_SIZE(pools); i++) {
		struct reserve_pool *pool = &pools[i];
		LIST_HEAD(dispose);
		unsigned long returned = 0;

		if (enabled)
			list_for_each_entry_safe(page, next, &pool->pages, lru) {
				if (nr >= sc->nr_to_scan)
					break;
				if (sc->nid != NUMA_NO_NODE && page_to_nid(page) != sc->nid)
					continue;
				list_add_tail(&take_page(pool, page)->lru, &dispose);
				returned += 1UL << pool->order;
				nr += 1UL << pool->order;
			}
		pool->pressure_returned += returned;
		spin_unlock_irqrestore(&pool_lock, flags);
		list_for_each_entry_safe(page, next, &dispose, lru) {
			list_del_init(&page->lru);
			__free_pages(page, pool->order);
		}
		spin_lock_irqsave(&pool_lock, flags);
	}
	spin_unlock_irqrestore(&pool_lock, flags);
	sc->nr_scanned = nr;
	/* A stale count snapshot must not leave reclaim with zero progress. */
	return nr ? nr : SHRINK_STOP;
}

static struct shrinker pool_shrinker = {
	.count_objects = pool_count,
	.scan_objects = pool_scan,
	.seeks = DEFAULT_SEEKS,
	.flags = SHRINKER_NUMA_AWARE,
};

static ssize_t enabled_show(struct kobject *kobj, struct kobj_attribute *attr,
			    char *buf)
{
	return scnprintf(buf, PAGE_SIZE, "%u\n", READ_ONCE(enabled));
}

static ssize_t enabled_store(struct kobject *kobj, struct kobj_attribute *attr,
			     const char *buf, size_t len)
{
	struct page *page;
	unsigned long flags;
	bool on;
	unsigned int i;
	unsigned long capacity = 0;
	int ret = kstrtobool(buf, &on);

	if (ret)
		return ret;
	mutex_lock(&control_lock);
	/*
	 * sysfs_create_group() publishes attributes one by one, so a writer can
	 * reach this store before the whole group exists. Only a complete
	 * publication may start, and the gate precedes the no-op shortcut.
	 */
	if (!init_ready) {
		ret = -ENODEV;
		goto out;
	}
	if (on == enabled)
		goto out;
	if (on) {
		for (i = 0; i < ARRAY_SIZE(pools); i++)
			capacity += pools[i].high << pools[i].order;
		if (capacity > totalram_pages / 512) {
			ret = -ENOMEM;
			goto out;
		}
		ret = allocator_reserve_acquire(RESERVE_C21);
		if (ret)
			goto out;
		ret = register_shrinker(&pool_shrinker);
		if (ret)
			goto release_owner;
		/* Start sleeping; publish ON only after all resources exist. */
		refill_task = kthread_run(refill, NULL, "mi_mempool");
		if (IS_ERR(refill_task)) {
			ret = PTR_ERR(refill_task);
			refill_task = NULL;
			unregister_shrinker(&pool_shrinker);
			goto release_owner;
		}
		spin_lock_irqsave(&pool_lock, flags);
		generation++;
		retry_after = jiffies;
		enabled = true;
		spin_unlock_irqrestore(&pool_lock, flags);
		wake_up(&refill_wait);
	} else {
		spin_lock_irqsave(&pool_lock, flags);
		enabled = false;
		generation++;
		spin_unlock_irqrestore(&pool_lock, flags);
		unregister_shrinker(&pool_shrinker);
		kthread_stop(refill_task);
		refill_task = NULL;
		spin_lock_irqsave(&pool_lock, flags);
		for (i = 0; i < ARRAY_SIZE(pools); i++) {
			struct reserve_pool *pool = &pools[i];

			while (!list_empty(&pool->pages)) {
				page = take_page(pool, list_first_entry(&pool->pages,
								 struct page, lru));
				pool->stop_drain += 1UL << pool->order;
				spin_unlock_irqrestore(&pool_lock, flags);
				__free_pages(page, pool->order);
				spin_lock_irqsave(&pool_lock, flags);
			}
		}
		spin_unlock_irqrestore(&pool_lock, flags);
		/* Callbacks must not recreate state after this point. */
		c21_clear_identities();
		allocator_reserve_release(RESERVE_C21);
	}
	goto out;
release_owner:
	allocator_reserve_release(RESERVE_C21);
out:
	mutex_unlock(&control_lock);
	return ret ? ret : len;
}

static unsigned int c21_cache_used_locked(void)
{
	unsigned int i, nr = 0;

	for (i = 0; i < C21_CACHE_ENTRIES; i++)
		if (c21_cache[i].tgid)
			nr++;
	return nr;
}

static unsigned int c21_grants_used_locked(void)
{
	unsigned int i, nr = 0;

	for (i = 0; i < C21_GRANT_SLOTS; i++)
		if (c21_grants[i])
			nr++;
	return nr;
}

static ssize_t pids_show(struct kobject *kobj, struct kobj_attribute *attr,
			 char *buf)
{
	unsigned long flags;
	ssize_t ret = 0;
	unsigned int i;

	spin_lock_irqsave(&pool_lock, flags);
	for (i = 0; i < C21_GRANT_SLOTS; i++)
		if (c21_grants[i])
			ret += scnprintf(buf + ret, PAGE_SIZE - ret, "%d\n",
					 pid_nr_ns(c21_grants[i], &init_pid_ns));
	spin_unlock_irqrestore(&pool_lock, flags);
	return ret;
}

/*
 * Root in the initial user namespace only. Each identity is resolved and
 * referenced before publication, and an exec/exit that already ran is not
 * undone by a delayed write.
 */
static ssize_t pids_store(struct kobject *kobj, struct kobj_attribute *attr,
			  const char *buf, size_t len)
{
	struct pid *resolved[C21_GRANT_SLOTS] = { };
	u64 exec_ids[C21_GRANT_SLOTS] = { };
	int ids[C21_GRANT_SLOTS];
	unsigned int nr = 0, i;
	int ret, clear;

	if (!ns_capable(&init_user_ns, CAP_SYS_ADMIN))
		return -EPERM;
	ret = c21_parse_pids(buf, len, ids, &nr);
	if (ret < 0)
		return ret;
	clear = ret;
	mutex_lock(&control_lock);
	if (!init_ready || !READ_ONCE(enabled)) {
		ret = -ENODEV;
		goto out;
	}
	for (i = 0; i < (clear ? 0U : nr); i++) {
		ret = c21_resolve_tgid(ids[i], &resolved[i], &exec_ids[i]);
		if (ret)
			goto put_out;
	}
	ret = c21_publish_grants(resolved, exec_ids, clear ? 0U : nr);
	if (ret)
		goto put_out;
	for (i = 0; i < C21_GRANT_SLOTS; i++)
		resolved[i] = NULL;
put_out:
	for (i = 0; i < C21_GRANT_SLOTS; i++)
		put_pid(resolved[i]);
out:
	mutex_unlock(&control_lock);
	return ret ? ret : len;
}

static ssize_t stats_show(struct kobject *kobj, struct kobj_attribute *attr,
			  char *buf)
{
	unsigned long flags;
	ssize_t ret = 0;
	unsigned int i;

	spin_lock_irqsave(&pool_lock, flags);
	for (i = 0; i < ARRAY_SIZE(pools); i++) {
		struct reserve_pool *pool = &pools[i];

		ret += scnprintf(buf + ret, PAGE_SIZE - ret,
			"order=%u blocks=%u pages=%u high=%u low=%u hits=%lu misses=%lu refill_failures=%lu pressure_returned=%lu stop_drain=%lu\n",
			pool->order, pool->count, pool->count << pool->order,
			pool->high, pool->low, pool->hits, pool->misses,
			pool->alloc_failures, pool->pressure_returned, pool->stop_drain);
	}
	ret += scnprintf(buf + ret, PAGE_SIZE - ret,
		"early hits=%ld orders=0,1 denied_context=%ld denied_state=%ld denied_identity=%ld denied_pressure=%ld denied_cache=%ld denied_floor=%ld denied_pool=%ld cache_entries=%u grants=%u\n",
		atomic_long_read(&c21_early_hits),
		atomic_long_read(&c21_early_denied_context),
		atomic_long_read(&c21_early_denied_state),
		atomic_long_read(&c21_early_denied_identity),
		atomic_long_read(&c21_early_denied_pressure),
		atomic_long_read(&c21_early_denied_cache),
		atomic_long_read(&c21_early_denied_floor),
		atomic_long_read(&c21_early_denied_pool),
		c21_cache_used_locked(), c21_grants_used_locked());
	spin_unlock_irqrestore(&pool_lock, flags);
	return ret;
}
static struct kobj_attribute enabled_attr = __ATTR_RW(enabled);
static struct kobj_attribute stats_attr = __ATTR_RO(stats);
static struct kobj_attribute pids_attr = __ATTR_RW(pids);
static struct attribute *pool_attrs[] = {
	&enabled_attr.attr, &stats_attr.attr, &pids_attr.attr, NULL,
};
static const struct attribute_group pool_group = { .attrs = pool_attrs };

static int __init mi_mempool_init(void)
{
	struct kobject *kobj;
	int ret;

	/*
	 * Register the lifecycle probes before publishing any control attribute.
	 * Otherwise a user could enable the pool and lose the exec/exit gate when
	 * registration later failed, leaving a worker and owner without a node.
	 */
	ret = c21_register_probes();
	if (ret)
		return ret;
	kobj = kobject_create_and_add("mi_mempool", kernel_kobj);
	if (!kobj) {
		ret = -ENOMEM;
		goto unregister;
	}
	ret = sysfs_create_group(kobj, &pool_group);
	if (ret) {
		kobject_put(kobj);
		goto unregister;
	}
	/* The whole group exists only now; control writes were refused until here. */
	mutex_lock(&control_lock);
	init_ready = true;
	mutex_unlock(&control_lock);
	return 0;
unregister:
	c21_unregister_probes();
	return ret;
}
late_initcall(mi_mempool_init);
