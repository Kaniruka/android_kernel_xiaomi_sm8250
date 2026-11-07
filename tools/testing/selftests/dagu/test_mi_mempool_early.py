#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Actual C21 reclaim-before policy, floors, lifecycle and configuration source.

The policy, cache, grant, probe, init and sysfs transaction functions are
extracted from mm/mi_mempool.c and compiled against host stubs. Allocator,
RCU, PID, task and kobject behaviour are stubbed: this is a boundary check,
not a concurrency, allocator or device proof.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from c_helpers import function, read_source

SRC = 'mm/mi_mempool.c'
source = read_source(SRC)
defines = '\n'.join(re.findall(r'^#define C21_[A-Z_]+.*$', source, re.M))
classes = re.search(r'enum c21_class_id \{.*?\};', source, re.S).group(0)
structs = '\n'.join(re.findall(r'struct c21_\w+ \{.*?\};', source, re.S))
state = '\n'.join(re.findall(
    r'^static (?:struct c21_cache_entry |struct pid \*|atomic_long_t )'
    r'c21_\w+[^;()]*;$', source, re.M))

# The reclaim-before gate is useless without the sched lifecycle tracepoints,
# so the option must keep depending on TRACEPOINTS.
kconfig = read_source('mm/Kconfig')
mi_block = re.search(r'^config MI_MEMPOOL$.*?(?=^config |\Z)', kconfig,
                     re.M | re.S)
if mi_block is None or 'TRACEPOINTS' not in mi_block.group(0):
    raise SystemExit('MI_MEMPOOL must depend on TRACEPOINTS')

prologue = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <sys/types.h>

#define PAGE_SHIFT 12
typedef unsigned int gfp_t;
typedef int pid_t;
typedef unsigned long long u64;
typedef long atomic_long_t;
typedef int spinlock_t;

#define atomic_read(p) (*(p))
#define atomic_long_inc(p) (++*(p))
#define atomic_long_read(p) (*(p))
#define READ_ONCE(x) (x)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define TASK_COMM_LEN 16
#define ZONE_NORMAL 2

#define __GFP_DIRECT_RECLAIM 1U
#define __GFP_DMA 2U
#define __GFP_DMA32 4U
#define __GFP_HIGHMEM 8U
#define __GFP_MOVABLE 16U
#define __GFP_ATOMIC 32U
#define __GFP_MEMALLOC 64U
#define __GFP_NOFAIL 128U
#define __GFP_HIGH 256U

#define PF_MEMALLOC 1U
#define PF_KTHREAD 2U
#define PF_EXITING 4U

#define NR_FREE_PAGES 0
#define NR_FREE_CMA_PAGES 1
#define NR_ACTIVE_FILE 2
#define NR_INACTIVE_FILE 3
#define NR_ACTIVE_ANON 4
#define NR_INACTIVE_ANON 5
#define NR_KERNEL_MISC_RECLAIMABLE 6

#define PIDTYPE_PID 0
#define PIDTYPE_TGID 1
#define CAP_SYS_ADMIN 21

#define time_before(a, b) ((long)((b) - (a)) > 0)
#define rcu_read_lock() do { } while (0)
#define rcu_read_unlock() do { } while (0)
#define __init

struct pglist_data { unsigned long stats[8]; };
struct zone {
	int index;
	struct pglist_data *zone_pgdat;
	unsigned long nr_reserved_highatomic;
};
struct list_head { struct list_head *next, *prev; };
struct page { struct list_head lru; struct zone *zone; };
struct mm_struct { int dummy; };
struct reclaim_state { int dummy; };
struct signal_struct { int oom_score_adj; int live; };
struct pid { int nr; int refs; struct task_struct *task; };
struct linux_binprm { int dummy; };
struct task_struct {
	unsigned int flags;
	struct mm_struct *mm;
	struct reclaim_state *reclaim_state;
	struct task_struct *group_leader;
	struct pid *thread_pid;
	char comm[TASK_COMM_LEN];
	u64 self_exec_id;
	struct signal_struct *signal;
};
struct reserve_pool {
	struct list_head pages;
	unsigned int order, high, low, count;
	unsigned long hits, misses, alloc_failures, pressure_returned, stop_drain;
};
struct kobject { int dummy; };
struct kobj_attribute { int dummy; };
struct attribute_group { int dummy; };

static spinlock_t pool_lock;
static int control_lock;
static unsigned long jiffies = 1000;
static bool enabled;
static unsigned long generation = 7;
static unsigned long retry_after;
static struct task_struct *refill_task;
static struct task_struct *current;
static bool unsafe, fatal_pending;
static int swap_pages;
static unsigned long zone_stats[8];
static struct reserve_pool pools[4];
static struct page item;
static void (*race_hook)(void);

static struct kobject kernel_kobj_storage;
static bool init_ready;
static struct kobject *kernel_kobj = &kernel_kobj_storage;
static const struct attribute_group pool_group;
static int kobj_calls, kobj_put_calls, sysfs_calls;
static struct kobject *kobj_ret;
static void (*sysfs_group_hook)(void);
static int sysfs_ret;
static int reg_exec_calls, reg_exit_calls, unreg_exec_calls, unreg_exit_calls;
static int sync_calls;
static int trace_exec_ret, trace_exit_ret;
static bool cap_allowed;
static int init_user_ns;
static int init_pid_ns;

#define init_list_head(h) do { (h)->next = (h); (h)->prev = (h); } while (0)
static void list_add_tail(struct list_head *node, struct list_head *head)
{
	node->prev = head->prev;
	node->next = head;
	head->prev->next = node;
	head->prev = node;
}
#define list_del_init(node) do { \
	(node)->prev->next = (node)->next; \
	(node)->next->prev = (node)->prev; \
	init_list_head(node); \
} while (0)
#define list_for_each_entry(p, h, m) \
	for (p = (struct page *)((char *)(h)->next - offsetof(struct page, m)); \
	     &p->m != (h); \
	     p = (struct page *)((char *)p->m.next - offsetof(struct page, m)))

#define page_zone(p) ((p)->zone)
#define page_pgdat(p) ((p)->zone->zone_pgdat)
#define zone_idx(z) ((z)->index)
#define zone_page_state(z, i) (zone_stats[i])
#define node_page_state(p, i) ((p)->stats[i])
#define get_nr_swap_pages() (swap_pages)
#define __mod_node_page_state(p, i, d) \
	do { (void)(p); (void)(i); (void)(d); } while (0)

#define spin_lock_irqsave(l, f) do { (void)(l); (f) = 0; } while (0)
#define spin_unlock_irqrestore(l, f) do { (void)(l); (void)(f); } while (0)
#define mutex_lock(l) ((void)(l))
#define mutex_unlock(l) ((void)(l))

#define in_interrupt() (unsafe)
#define irqs_disabled() (unsafe)
#define in_atomic() (unsafe)
#define fatal_signal_pending(t) ((void)(t), fatal_pending)

#define task_lock(t) ((void)(t))
#define task_unlock(t) do { (void)(t); if (race_hook) race_hook(); } while (0)

static int registry_nr;
static struct pid *registry[8];

static struct pid *get_pid(struct pid *pid)
{
	pid->refs++;
	return pid;
}
static void put_pid(struct pid *pid)
{
	if (!pid)
		return;
	assert(pid->refs > 0);
	pid->refs--;
}
static struct task_struct *pid_task(struct pid *pid, int type)
{
	(void)type;
	return pid ? pid->task : NULL;
}
static struct pid *find_pid_ns(int nr, int *ns)
{
	int i;

	(void)ns;
	for (i = 0; i < registry_nr; i++)
		if (registry[i]->nr == nr)
			return registry[i];
	return NULL;
}
static struct pid *get_task_pid(struct task_struct *task, int type)
{
	(void)type;
	return task->thread_pid ? get_pid(task->thread_pid) : NULL;
}
static bool ns_capable(int *ns, int cap)
{
	(void)ns;
	(void)cap;
	return cap_allowed;
}
static struct kobject *kobject_create_and_add(const char *name,
					      struct kobject *parent)
{
	(void)name;
	(void)parent;
	kobj_calls++;
	return kobj_ret;
}
static int sysfs_create_group(struct kobject *kobj,
			      const struct attribute_group *group)
{
	(void)kobj;
	(void)group;
	sysfs_calls++;
	if (sysfs_group_hook)
		sysfs_group_hook();
	return sysfs_ret;
}
static void kobject_put(struct kobject *kobj)
{
	(void)kobj;
	kobj_put_calls++;
}
static int register_trace_sched_process_exec(
	void (*probe)(void *, struct task_struct *, pid_t, struct linux_binprm *),
	void *data)
{
	(void)probe;
	(void)data;
	if (trace_exec_ret)
		return trace_exec_ret;
	reg_exec_calls++;
	return 0;
}
static void unregister_trace_sched_process_exec(
	void (*probe)(void *, struct task_struct *, pid_t, struct linux_binprm *),
	void *data)
{
	(void)probe;
	(void)data;
	unreg_exec_calls++;
}
static int register_trace_sched_process_exit(
	void (*probe)(void *, struct task_struct *), void *data)
{
	(void)probe;
	(void)data;
	if (trace_exit_ret)
		return trace_exit_ret;
	reg_exit_calls++;
	return 0;
}
static void unregister_trace_sched_process_exit(
	void (*probe)(void *, struct task_struct *), void *data)
{
	(void)probe;
	(void)data;
	unreg_exit_calls++;
}
static void tracepoint_synchronize_unregister(void)
{
	sync_calls++;
}
static void mi_mempool_free_event(void) { }

struct shrinker { int dummy; };
enum allocator_reserve_owner { RESERVE_NONE, RESERVE_C21, RESERVE_C22 };
static unsigned long totalram_pages = 1UL << 20;
static int refill_wait, owner_held, acquire_ret;
static int acquire_calls, release_calls, kthread_run_calls, kthread_stop_calls;
static int shrinker_reg_calls, shrinker_unreg_calls, shrinker_ret, wake_calls;
static struct task_struct fake_refill_task;
static struct shrinker pool_shrinker;

static int allocator_reserve_acquire(int owner)
{
	acquire_calls++;
	if (acquire_ret)
		return acquire_ret;
	if (owner_held != RESERVE_NONE)
		return -EBUSY;
	owner_held = owner;
	return 0;
}
static void allocator_reserve_release(int owner)
{
	release_calls++;
	if (owner_held == owner)
		owner_held = RESERVE_NONE;
}
static int register_shrinker(struct shrinker *shrinker)
{
	(void)shrinker;
	shrinker_reg_calls++;
	return shrinker_ret;
}
static void unregister_shrinker(struct shrinker *shrinker)
{
	(void)shrinker;
	shrinker_unreg_calls++;
}
static int refill(void *unused)
{
	(void)unused;
	return 0;
}
static struct task_struct *kthread_run(int (*fn)(void *), void *arg,
				       const char *name)
{
	(void)fn;
	(void)arg;
	(void)name;
	kthread_run_calls++;
	return &fake_refill_task;
}
static int kthread_stop(struct task_struct *task)
{
	(void)task;
	kthread_stop_calls++;
	return 0;
}
static void wake_up(int *queue)
{
	(void)queue;
	wake_calls++;
}
static void __free_pages(struct page *page, unsigned int order)
{
	(void)page;
	(void)order;
}
static int kstrtobool(const char *buf, bool *res)
{
	if (buf[0] == '1' && !buf[1]) {
		*res = true;
		return 0;
	}
	if (buf[0] == '0' && !buf[1]) {
		*res = false;
		return 0;
	}
	return -EINVAL;
}
#define IS_ERR(x) ((unsigned long)(x) >= (unsigned long)-4095)
#define PTR_ERR(x) ((long)(x))
#define list_empty(h) ((h)->next == (h))
#define list_first_entry(h, t, m) ((t *)((char *)(h)->next - offsetof(t, m)))
'''

code = prologue + '\n' + defines + '\n\n' + classes + '\n\n' + structs + '\n\n' + state + '\n\n'
for name in ('c21_comm_eq', 'c21_classify', 'c21_process_floor',
             'c21_direct_floor', 'c21_floor_allows', 'c21_pressure_sample',
             'c21_take_ident', 'c21_tgid_live', 'c21_cache_find_locked',
             'c21_cache_drop_locked', 'c21_cache_store_locked',
             'c21_grant_locked', 'take_page', 'mi_mempool_borrow',
             'mi_mempool_early_borrow', 'c21_drop_tgid', 'c21_process_exec',
             'c21_process_exit', 'c21_register_probes',
             'c21_unregister_probes', 'c21_is_space', 'c21_parse_pids',
             'c21_resolve_tgid', 'c21_publish_grants', 'c21_clear_identities',
             'c21_cache_used_locked', 'c21_grants_used_locked',
             'enabled_store', 'pids_store', 'mi_mempool_init'):
    code += function(SRC, name) + '\n'

code += r'''
static struct pglist_data node;
static struct zone normal_zone, dma_zone;
static struct mm_struct caller_mm;
static struct signal_struct signal_a, signal_b;
static struct pid pid_a, pid_b;
static struct task_struct leader_a, leader_b;
static struct page page_pool[160];
static struct pid extra_pid[40];
static struct task_struct extra_task[40];
static struct signal_struct extra_signal;

static void reset_world(void)
{
	static const unsigned int orders[4] = { 0, 1, 4, 5 };
	static const unsigned int highs[4] = { 1024, 128, 16, 8 };
	unsigned int i;

	c21_clear_identities();
	for (i = 0; i < 4; i++) {
		init_list_head(&pools[i].pages);
		pools[i].order = orders[i];
		pools[i].high = highs[i];
		pools[i].low = highs[i] / 2;
		pools[i].count = 0;
		pools[i].hits = 0;
		pools[i].misses = 0;
	}
	c21_early_hits = 0;
	c21_early_denied_context = 0;
	c21_early_denied_state = 0;
	c21_early_denied_identity = 0;
	c21_early_denied_pressure = 0;
	c21_early_denied_cache = 0;
	c21_early_denied_floor = 0;
	c21_early_denied_pool = 0;
	enabled = true;
	init_ready = true;
	generation = 7;
	race_hook = NULL;
	jiffies = 1000;
	retry_after = 1000;
	refill_task = NULL;
	unsafe = false;
	fatal_pending = false;
	swap_pages = 0;
	cap_allowed = true;
	memset(zone_stats, 0, sizeof(zone_stats));
	memset(node.stats, 0, sizeof(node.stats));
	memset(&normal_zone, 0, sizeof(normal_zone));
	normal_zone.index = ZONE_NORMAL;
	normal_zone.zone_pgdat = &node;
	dma_zone.index = 0;
	dma_zone.zone_pgdat = &node;
	memset(&signal_a, 0, sizeof(signal_a));
	memset(&signal_b, 0, sizeof(signal_b));
	signal_a.oom_score_adj = -1000;
	signal_a.live = 1;
	signal_b.oom_score_adj = 0;
	signal_b.live = 1;
	memset(&leader_a, 0, sizeof(leader_a));
	leader_a.mm = &caller_mm;
	leader_a.group_leader = &leader_a;
	leader_a.thread_pid = &pid_a;
	strcpy(leader_a.comm, "composer");
	leader_a.self_exec_id = 5;
	leader_a.signal = &signal_a;
	memset(&leader_b, 0, sizeof(leader_b));
	leader_b.mm = &caller_mm;
	leader_b.group_leader = &leader_b;
	leader_b.thread_pid = &pid_b;
	strcpy(leader_b.comm, "droid.bluetooth");
	leader_b.self_exec_id = 9;
	leader_b.signal = &signal_b;
	memset(&pid_a, 0, sizeof(pid_a));
	pid_a.nr = 4242;
	pid_a.task = &leader_a;
	memset(&pid_b, 0, sizeof(pid_b));
	pid_b.nr = 5353;
	pid_b.task = &leader_b;
	registry[0] = &pid_a;
	registry[1] = &pid_b;
	registry_nr = 2;
	current = &leader_a;
	/* High-free branch by default: available is one page above 11 MiB. */
	node.stats[NR_ACTIVE_FILE] = 1;
	zone_stats[NR_FREE_PAGES] = 2817;
}

static void stock(struct zone *zone, unsigned int idx, unsigned int count)
{
	init_list_head(&pools[idx].pages);
	item.zone = zone;
	init_list_head(&item.lru);
	list_add_tail(&item.lru, &pools[idx].pages);
	pools[idx].count = count;
}

static void stock_many(struct zone *zone, unsigned int idx, unsigned int count)
{
	unsigned int i;

	init_list_head(&pools[idx].pages);
	for (i = 0; i < count && i < ARRAY_SIZE(page_pool); i++) {
		page_pool[i].zone = zone;
		init_list_head(&page_pool[i].lru);
		list_add_tail(&page_pool[i].lru, &pools[idx].pages);
	}
	pools[idx].count = count;
}

static void reset_trace_state(void)
{
	kobj_calls = 0;
	kobj_put_calls = 0;
	sysfs_calls = 0;
	sysfs_group_hook = NULL;
	reg_exec_calls = 0;
	reg_exit_calls = 0;
	unreg_exec_calls = 0;
	unreg_exit_calls = 0;
	sync_calls = 0;
	trace_exec_ret = 0;
	trace_exit_ret = 0;
	kobj_ret = &kernel_kobj_storage;
	sysfs_ret = 0;
}

static void exec_race(void)
{
	leader_a.self_exec_id++;
	c21_drop_tgid(&pid_a);
}

static void stop_race(void)
{
	enabled = false;
	generation++;
	c21_clear_identities();
}

static void generation_race(void)
{
	generation++;
}

static ssize_t concurrent_enable_result;

/* A control write that lands while the sysfs group is still being created. */
static void concurrent_enable(void)
{
	concurrent_enable_result = enabled_store(NULL, NULL, "1", 1);
}

int main(void)
{
	struct page *page;
	struct pid *resolved[5];
	u64 exec_ids[5];
	unsigned long available;
	int ids[5];
	unsigned int nr, i;

	/* --- typed classification: adj is an exact match, not a limit --- */
	assert(c21_classify("composer", -1000) == C21_CLASS_COMPOSER);
	assert(c21_classify("composer", -999) == C21_CLASS_NONE);
	assert(c21_classify("composer", -1001) == C21_CLASS_NONE);
	assert(c21_classify("composer", 0) == C21_CLASS_NONE);
	assert(c21_classify("surfaceflinger", -1000) == C21_CLASS_SURFACEFLINGER);
	assert(c21_classify("surfaceflinger", -900) == C21_CLASS_NONE);
	assert(c21_classify("surfaceflinger", 0) == C21_CLASS_NONE);
	assert(c21_classify("system_server", -900) == C21_CLASS_SYSTEM_SERVER);
	assert(c21_classify("system_server", -899) == C21_CLASS_NONE);
	assert(c21_classify("system_server", -901) == C21_CLASS_NONE);
	assert(c21_classify("system_server", -1000) == C21_CLASS_NONE);
	assert(c21_classify("com.miui.home", 1000) == C21_CLASS_MIUI_HOME);
	assert(c21_classify("com.miui.home", -1000) == C21_CLASS_MIUI_HOME);
	assert(c21_classify("ndroid.systemui", 500) == C21_CLASS_SYSTEMUI);
	assert(c21_classify(".globallauncher", -1) == C21_CLASS_GLOBALLAUNCHER);
	assert(c21_classify("droid.bluetooth", 900) == C21_CLASS_BLUETOOTH);
	assert(c21_classify("droid.bluetooth", -1000) == C21_CLASS_BLUETOOTH);
	assert(c21_classify("app_main", -1000) == C21_CLASS_NONE);
	assert(c21_classify("android.systemui", 0) == C21_CLASS_NONE);
	assert(c21_classify("", 0) == C21_CLASS_NONE);
	assert(C21_CLASS_GRANT != C21_CLASS_NONE);

	/* --- floors: both branches, exact boundaries --- */
	assert(C21_AVAILABLE_HIGH == 2816);
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 0, 71, 2816));
	assert(c21_floor_allows(C21_CLASS_COMPOSER, 0, 72, 2816));
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 0, 272, 2817));
	assert(c21_floor_allows(C21_CLASS_COMPOSER, 0, 273, 2817));
	/* The low-free branch waives the direct floor but keeps the process floor. */
	assert(c21_floor_allows(C21_CLASS_COMPOSER, 0, 272, 2816));
	assert(!c21_floor_allows(C21_CLASS_SYSTEM_SERVER, 0, 153, 2816));
	assert(c21_floor_allows(C21_CLASS_SYSTEM_SERVER, 0, 154, 2816));
	assert(!c21_floor_allows(C21_CLASS_SYSTEM_SERVER, 0, 272, 2817));
	assert(c21_floor_allows(C21_CLASS_SYSTEM_SERVER, 0, 273, 2817));
	assert(!c21_floor_allows(C21_CLASS_MIUI_HOME, 1, 102, 2816));
	assert(c21_floor_allows(C21_CLASS_MIUI_HOME, 1, 103, 2816));
	assert(c21_floor_allows(C21_CLASS_MIUI_HOME, 1, 128, 2816));
	assert(!c21_floor_allows(C21_CLASS_MIUI_HOME, 1, 128, 2817));
	assert(!c21_floor_allows(C21_CLASS_MIUI_HOME, 1, 102, 2817));
	/* Orders 4/5 equal their high capacity and can never early-borrow. */
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 4, 15, 2816));
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 4, 16, 2816));
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 4, 16, 2817));
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 5, 7, 2816));
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 5, 8, 2816));
	assert(!c21_floor_allows(C21_CLASS_COMPOSER, 5, 8, 2817));

	/* --- pressure sampler: file/anon gate and saturated available --- */
	reset_world();
	node.stats[NR_ACTIVE_FILE] = 0;
	node.stats[NR_INACTIVE_FILE] = 0;
	node.stats[NR_ACTIVE_ANON] = 4096;
	node.stats[NR_INACTIVE_ANON] = 4096;
	swap_pages = 0;
	assert(!c21_pressure_sample(&normal_zone, &available));
	swap_pages = 1;
	assert(c21_pressure_sample(&normal_zone, &available));
	node.stats[NR_ACTIVE_ANON] = 0;
	node.stats[NR_INACTIVE_ANON] = 0;
	assert(!c21_pressure_sample(&normal_zone, &available));
	node.stats[NR_ACTIVE_FILE] = 0;
	assert(!c21_pressure_sample(&normal_zone, &available));
	node.stats[NR_ACTIVE_FILE] = 1;
	normal_zone.nr_reserved_highatomic = 60;
	zone_stats[NR_FREE_CMA_PAGES] = 60;
	zone_stats[NR_FREE_PAGES] = 100;
	assert(c21_pressure_sample(&normal_zone, &available));
	assert(available == 0);			/* saturated, never negative */
	normal_zone.nr_reserved_highatomic = 0;
	zone_stats[NR_FREE_CMA_PAGES] = 0;
	zone_stats[NR_FREE_PAGES] = 2816;
	assert(c21_pressure_sample(&normal_zone, &available));
	assert(available == 2816);
	zone_stats[NR_FREE_PAGES] = 2817;
	assert(c21_pressure_sample(&normal_zone, &available));
	assert(available == 2817);

	/* --- pids transaction grammar --- */
	assert(c21_parse_pids("0", 1, ids, &nr) == 1 && nr == 1);
	assert(c21_parse_pids(" 4242\n", 6, ids, &nr) == 0 && nr == 1);
	assert(ids[0] == 4242);
	assert(c21_parse_pids("1 2\t3\n4 5", 9, ids, &nr) == 0 && nr == 5);
	assert(c21_parse_pids("1 2 3 4 5 6", 11, ids, &nr) == -EINVAL);
	assert(c21_parse_pids("7 7", 3, ids, &nr) == -EINVAL);
	assert(c21_parse_pids("-7", 2, ids, &nr) == -EINVAL);
	assert(c21_parse_pids("7x", 2, ids, &nr) == -EINVAL);
	assert(c21_parse_pids("7 0", 3, ids, &nr) == -EINVAL);
	assert(c21_parse_pids("", 0, ids, &nr) == -EINVAL);
	assert(c21_parse_pids(" \t\n", 3, ids, &nr) == -EINVAL);
	assert(c21_parse_pids("99999999999", 11, ids, &nr) == -EINVAL);

	/* --- publication, revalidation and old-configuration survival --- */
	reset_world();
	assert(c21_resolve_tgid(4242, &resolved[0], &exec_ids[0]) == 0);
	assert(resolved[0] == &pid_a && pid_a.refs == 1 && exec_ids[0] == 5);
	assert(c21_publish_grants(resolved, exec_ids, 1) == 0);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_a);
	assert(c21_resolve_tgid(9999, &resolved[1], &exec_ids[1]) == -ESRCH);
	assert(c21_resolve_tgid(5353, &resolved[1], &exec_ids[1]) == 0);
	assert(resolved[1] == &pid_b);
	exec_ids[1] = 5;			/* stale: resolved before an exec */
	leader_b.self_exec_id = 6;
	assert(c21_publish_grants(&resolved[1], &exec_ids[1], 1) == -EAGAIN);
	put_pid(resolved[1]);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_a);
	assert(pid_b.refs == 0);

	/* TIDs, dead and mm-less targets are rejected. */
	leader_b.group_leader = &leader_a;
	assert(c21_resolve_tgid(5353, &resolved[1], &exec_ids[1]) == -ESRCH);
	leader_b.group_leader = &leader_b;
	leader_b.mm = NULL;
	assert(c21_resolve_tgid(5353, &resolved[1], &exec_ids[1]) == -ESRCH);
	leader_b.mm = &caller_mm;
	leader_b.flags = PF_EXITING;
	assert(c21_resolve_tgid(5353, &resolved[1], &exec_ids[1]) == -ESRCH);
	leader_b.flags = 0;
	assert(pid_b.refs == 0);

	/* --- actual pids_store transaction: capability, state and rollback --- */
	reset_world();
	assert(pids_store(NULL, NULL, "4242", 4) == 4);
	assert(c21_grants_used_locked() == 1 && pid_a.refs == 1);
	cap_allowed = false;
	assert(pids_store(NULL, NULL, "5353", 4) == -EPERM);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_a);
	cap_allowed = true;
	enabled = false;
	assert(pids_store(NULL, NULL, "5353", 4) == -ENODEV);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_a);
	enabled = true;
	/* Malformed input and unknown TGIDs keep the previous configuration. */
	assert(pids_store(NULL, NULL, "5353 5353", 9) == -EINVAL);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_a);
	assert(pids_store(NULL, NULL, "-5353", 5) == -EINVAL);
	assert(pids_store(NULL, NULL, "1 2 3 4 5 6", 11) == -EINVAL);
	assert(pids_store(NULL, NULL, "9999", 4) == -ESRCH);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_a);
	assert(pids_store(NULL, NULL, "4242 5353", 9) == 9);
	assert(c21_grants_used_locked() == 2);
	assert(pids_store(NULL, NULL, "5353", 4) == 4);
	assert(c21_grants_used_locked() == 1 && c21_grants[0] == &pid_b);
	assert(pid_a.refs == 0 && pid_b.refs == 1);
	assert(pids_store(NULL, NULL, "0", 1) == 1);
	assert(c21_grants_used_locked() == 0 && pid_b.refs == 0);

	/* --- exec/exit callbacks --- */
	reset_world();
	enabled = true;
	assert(c21_resolve_tgid(4242, &resolved[0], &exec_ids[0]) == 0);
	assert(c21_publish_grants(resolved, exec_ids, 1) == 0);
	c21_process_exec(NULL, &leader_a, 0, NULL);
	assert(c21_grants_used_locked() == 0 && pid_a.refs == 0);
	assert(c21_resolve_tgid(5353, &resolved[0], &exec_ids[0]) == 0);
	assert(c21_publish_grants(resolved, exec_ids, 1) == 0);
	signal_b.live = 2;
	c21_process_exit(NULL, &leader_b);	/* one thread leaves */
	assert(c21_grants_used_locked() == 1);
	signal_b.live = 0;
	c21_process_exit(NULL, &leader_b);	/* group dead */
	assert(c21_grants_used_locked() == 0 && pid_b.refs == 0);
	/* A callback never recreates state after disable cleared it. */
	enabled = false;
	c21_process_exit(NULL, &leader_b);
	assert(c21_grants_used_locked() == 0 && c21_cache_used_locked() == 0);

	/* --- early loan: high-free direct floor --- */
	reset_world();
	stock(&normal_zone, 0, 273);
	page = mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone);
	assert(page == &item);
	assert(pools[0].count == 272 && pools[0].hits == 1);
	assert(atomic_long_read(&c21_early_hits) == 1);
	assert(c21_cache_used_locked() == 1 && pid_a.refs == 1);
	/* Same count no longer clears the strict direct floor. */
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_floor) == 1);

	/* --- early loan: low-free process floor only --- */
	reset_world();
	zone_stats[NR_FREE_PAGES] = 2816;
	stock(&normal_zone, 0, 72);
	assert(mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone) == &item);
	reset_world();
	zone_stats[NR_FREE_PAGES] = 2816;
	stock(&normal_zone, 0, 71);
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_floor) == 1);
	assert(pools[0].count == 71);

	/* --- order one: only the low-free branch is reachable --- */
	reset_world();
	zone_stats[NR_FREE_PAGES] = 2816;
	stock(&normal_zone, 1, 103);
	assert(mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 1, &normal_zone) == &item);
	reset_world();
	zone_stats[NR_FREE_PAGES] = 2816;
	stock(&normal_zone, 1, 102);
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 1, &normal_zone));
	reset_world();
	stock(&normal_zone, 1, 128);		/* high free, full pool */
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 1, &normal_zone));

	/* --- orders four and five keep only the failure-after path --- */
	reset_world();
	zone_stats[NR_FREE_PAGES] = 2816;
	stock(&normal_zone, 2, 16);
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 4, &normal_zone));
	stock(&normal_zone, 3, 8);
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 5, &normal_zone));

	/* --- eligibility rejections keep direct reclaim --- */
	reset_world();
	stock(&normal_zone, 0, 300);
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &dma_zone));
	assert(!mi_mempool_early_borrow(0, 0, &normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_DMA, 0,
					&normal_zone));
	/* The new path additionally rejects __GFP_HIGH. */
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_HIGH, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_DMA32, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_HIGHMEM, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_MOVABLE, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_ATOMIC, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_MEMALLOC, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM | __GFP_NOFAIL, 0,
					&normal_zone));
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 2, &normal_zone));
	unsafe = true;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	unsafe = false;
	leader_a.flags = PF_MEMALLOC;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	leader_a.flags = PF_KTHREAD;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_context) == 1);
	leader_a.flags = PF_EXITING;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	leader_a.flags = 0;
	leader_a.mm = NULL;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	leader_a.mm = &caller_mm;
	leader_a.reclaim_state = (struct reclaim_state *)&caller_mm;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	leader_a.reclaim_state = NULL;
	fatal_pending = true;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	fatal_pending = false;
	refill_task = &leader_a;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	refill_task = NULL;
	/* app_main is deliberately not eligible. */
	strcpy(leader_a.comm, "app_main");
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_identity) == 1);
	/* A granted TGID is eligible regardless of comm. */
	strcpy(leader_a.comm, "android.systemui");
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(c21_resolve_tgid(4242, &resolved[0], &exec_ids[0]) == 0);
	assert(c21_publish_grants(resolved, exec_ids, 1) == 0);
	assert(mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone) == &item);
	/* The failure-after mask still accepts __GFP_HIGH. */
	reset_world();
	stock(&normal_zone, 0, 273);
	assert(mi_mempool_borrow(__GFP_DIRECT_RECLAIM | __GFP_HIGH, 0,
				 &normal_zone) == &item);

	/* --- disabled is the default: no sampling, reference or counter --- */
	reset_world();
	stock(&normal_zone, 0, 300);
	enabled = false;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(pid_a.refs == 0 && c21_cache_used_locked() == 0);
	assert(atomic_long_read(&c21_early_hits) == 0);
	assert(atomic_long_read(&c21_early_denied_context) == 0);
	assert(atomic_long_read(&c21_early_denied_state) == 0);
	assert(atomic_long_read(&c21_early_denied_identity) == 0);
	assert(atomic_long_read(&c21_early_denied_pressure) == 0);
	assert(pools[0].count == 300);

	/* --- state, cooldown and the untouched failure-after path --- */
	reset_world();
	stock(&normal_zone, 0, 273);
	retry_after = jiffies + 300;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_state) == 1);
	assert(c21_cache_used_locked() == 0 && pools[0].count == 273);
	/* The accepted post-failure borrowing is unchanged by the cooldown. */
	assert(mi_mempool_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone) == &item);
	assert(pools[0].count == 272 && pools[0].hits == 1);

	reset_world();
	stock(&normal_zone, 0, 273);
	/* A stop/start between sample and pool_lock changes the generation. */
	race_hook = generation_race;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	race_hook = NULL;
	assert(atomic_long_read(&c21_early_denied_state) == 1);
	assert(c21_cache_used_locked() == 0 && pools[0].count == 273);

	/* --- sample raced an exec callback: no reinsert, no page --- */
	reset_world();
	stock(&normal_zone, 0, 273);
	race_hook = exec_race;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	race_hook = NULL;
	assert(c21_cache_used_locked() == 0);
	assert(atomic_long_read(&c21_early_hits) == 0);
	assert(pools[0].count == 273);

	/* --- sample raced a stop: no reinsert, no page --- */
	reset_world();
	stock(&normal_zone, 0, 273);
	race_hook = stop_race;
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	race_hook = NULL;
	assert(!enabled);
	assert(c21_cache_used_locked() == 0 && c21_grants_used_locked() == 0);
	assert(pools[0].count == 273);

	/* --- cache capacity fails closed --- */
	reset_world();
	zone_stats[NR_FREE_PAGES] = 2816;
	stock_many(&normal_zone, 0, 128);
	memset(&extra_signal, 0, sizeof(extra_signal));
	extra_signal.live = 1;
	extra_signal.oom_score_adj = -1000;
	for (i = 0; i < ARRAY_SIZE(extra_pid); i++) {
		memset(&extra_pid[i], 0, sizeof(extra_pid[i]));
		extra_pid[i].nr = 6000 + i;
		memset(&extra_task[i], 0, sizeof(extra_task[i]));
		extra_task[i].mm = &caller_mm;
		extra_task[i].group_leader = &extra_task[i];
		extra_task[i].thread_pid = &extra_pid[i];
		strcpy(extra_task[i].comm, "composer");
		extra_task[i].self_exec_id = 1;
		extra_task[i].signal = &extra_signal;
		extra_pid[i].task = &extra_task[i];
	}
	for (i = 0; i < 32; i++) {
		current = &extra_task[i];
		assert(mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0,
					       &normal_zone));
	}
	assert(c21_cache_used_locked() == C21_CACHE_ENTRIES);
	for (i = 0; i < 32; i++)
		assert(extra_pid[i].refs == 1);
	/* An eligible identity beyond the capacity fails closed and returns its ref. */
	current = &extra_task[32];
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_cache) == 1);
	assert(extra_pid[32].refs == 0);
	/* Slot 0 stopped classifying and holds no grant: it must be released. */
	strcpy(extra_task[0].comm, "app_main");
	current = &extra_task[0];
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(atomic_long_read(&c21_early_denied_identity) == 1);
	assert(c21_cache_used_locked() == C21_CACHE_ENTRIES - 1);
	assert(extra_pid[0].refs == 0);
	/* A newly eligible identity can take the freed slot and the pool. */
	current = &leader_a;
	assert(mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(c21_cache_used_locked() == C21_CACHE_ENTRIES);
	assert(pid_a.refs == 1);
	/* 32 long-lived identities cannot monopolise every slot. */
	for (i = 0; i < 32; i++)
		strcpy(extra_task[i].comm, "android.systemui");
	current = &extra_task[1];
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(c21_cache_used_locked() == C21_CACHE_ENTRIES - 1);
	assert(extra_pid[1].refs == 0);
	reset_world();
	for (i = 0; i < ARRAY_SIZE(extra_pid); i++)
		assert(extra_pid[i].refs == 0);

	/* --- a revoked grant must not keep a cache slot --- */
	reset_world();
	stock(&normal_zone, 0, 300);
	strcpy(leader_a.comm, "android.systemui");
	assert(!mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone));
	assert(c21_resolve_tgid(4242, &resolved[0], &exec_ids[0]) == 0);
	assert(c21_publish_grants(resolved, exec_ids, 1) == 0);
	assert(mi_mempool_early_borrow(__GFP_DIRECT_RECLAIM, 0, &normal_zone) == &item);
	assert(c21_cache_used_locked() == 1);
	assert(pid_a.refs == 2);		/* cache plus grant */
	assert(c21_resolve_tgid(5353, &resolved[0], &exec_ids[0]) == 0);
	assert(c21_publish_grants(resolved, exec_ids, 1) == 0);
	/* The replaced grant drops the grant and every grant-only entry. */
	assert(c21_cache_used_locked() == 0 && pid_a.refs == 0);
	assert(c21_grants_used_locked() == 1 && pid_b.refs == 1);
	assert(c21_publish_grants(resolved, exec_ids, 0) == 0);
	assert(c21_grants_used_locked() == 0 && pid_b.refs == 0);

	/* --- initialisation publishes attributes only after probes --- */
	reset_trace_state();
	trace_exec_ret = -ENOSYS;
	assert(mi_mempool_init() == -ENOSYS);
	assert(kobj_calls == 0 && sysfs_calls == 0);
	assert(reg_exec_calls == 0 && unreg_exec_calls == 0 && sync_calls == 0);

	reset_trace_state();
	trace_exit_ret = -ENOSYS;
	assert(mi_mempool_init() == -ENOSYS);
	assert(kobj_calls == 0 && sysfs_calls == 0);
	assert(reg_exec_calls == 1 && unreg_exec_calls == 1);

	reset_trace_state();
	kobj_ret = NULL;
	assert(mi_mempool_init() == -ENOMEM);
	assert(sysfs_calls == 0);
	assert(unreg_exec_calls == 1 && unreg_exit_calls == 1 && sync_calls == 1);

	reset_trace_state();
	sysfs_ret = -ENOMEM;
	assert(mi_mempool_init() == -ENOMEM);
	assert(kobj_put_calls == 1);
	assert(unreg_exec_calls == 1 && unreg_exit_calls == 1 && sync_calls == 1);

	reset_trace_state();
	assert(mi_mempool_init() == 0);
	assert(reg_exec_calls == 1 && reg_exit_calls == 1);
	assert(unreg_exec_calls == 0 && unreg_exit_calls == 0 && sync_calls == 0);

	/* --- control writes are refused until the group is fully published --- */
	reset_world();
	init_ready = false;
	enabled = false;
	owner_held = RESERVE_NONE;
	acquire_calls = release_calls = 0;
	kthread_run_calls = kthread_stop_calls = 0;
	concurrent_enable_result = 0;
	reset_trace_state();
	/* sysfs_create_group() publishes attributes one at a time. */
	sysfs_group_hook = concurrent_enable;
	sysfs_ret = -ENOMEM;
	assert(mi_mempool_init() == -ENOMEM);
	assert(concurrent_enable_result == -ENODEV);
	assert(!enabled && !init_ready);
	assert(acquire_calls == 0 && kthread_run_calls == 0);
	assert(unreg_exec_calls == 1 && unreg_exit_calls == 1 && sync_calls == 1);
	assert(kobj_put_calls == 1);
	/* The failed publication keeps refusing control writes. */
	assert(enabled_store(NULL, NULL, "1", 1) == -ENODEV);
	assert(pids_store(NULL, NULL, "4242", 4) == -ENODEV);
	assert(!enabled && acquire_calls == 0);

	/* --- a complete publication accepts control writes --- */
	reset_world();
	init_ready = false;
	enabled = false;
	owner_held = RESERVE_NONE;
	acquire_calls = release_calls = 0;
	kthread_run_calls = kthread_stop_calls = 0;
	reset_trace_state();
	assert(mi_mempool_init() == 0);
	assert(init_ready);
	assert(enabled_store(NULL, NULL, "1", 1) == 1);
	assert(enabled && acquire_calls == 1 && owner_held == RESERVE_C21);
	assert(kthread_run_calls == 1);
	assert(enabled_store(NULL, NULL, "1", 1) == 1);	/* no-op, still ready */
	assert(acquire_calls == 1);
	assert(enabled_store(NULL, NULL, "0", 1) == 1);
	assert(!enabled && release_calls == 1 && owner_held == RESERVE_NONE);
	assert(pids_store(NULL, NULL, "4242", 4) == -ENODEV);

	/* --- final reference conservation --- */
	reset_world();
	assert(c21_cache_used_locked() == 0 && c21_grants_used_locked() == 0);
	assert(pid_a.refs == 0 && pid_b.refs == 0);
	return 0;
}
'''

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(code)
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Werror', '-g',
                    '-fsanitize=' + os.environ.get('SANITIZERS', 'undefined'),
                    str(path / 'test.c'), '-o', str(path / 'test')],
                   check=True)
    subprocess.run([str(path / 'test')], check=True)
