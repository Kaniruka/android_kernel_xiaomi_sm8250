# dagu：界面与应用切换相关改动

本组改动基于 `android17-aptusitu` 的
`3ea6dc300146c4e3340413d0e1b04442d64598b1`。目标是修复并发与生命周期问题，
补齐已有调度接口，并提供可以分别验证的回收策略。没有测量到实机收益之前，
不把编译通过当作流畅度或功耗改善的证明。

## 默认构建中的行为

- **zram 统计**：修复数组泄漏、初始化失败清理、RCU 延迟释放、并发替换和计数。
  `time_list` 接受 1–64 个非负、严格递增的秒数，成功返回完整写入长度。
  平均值更新串行化，避免直接用纳秒时间乘页数。压缩算法选择不变。
- **zsmalloc**：handle 与 zspage slab 缓存由每个 pool 独立创建改为全局共享。
  保留原有分配标志及 pool 生命周期；内存收益取决于 pool 数和实际 slab 合并情况。
- **F2FS 统计**：I/O 字节更新按类型拆锁；app total/direct/buffered 使用同一把锁。
  周期、reset 与快照仍有控制锁。不同类型不是同一时刻的全局原子快照。
  iostat 默认开关保持原样；关闭统计时，这项改动不产生统计路径上的收益。
- **F2FS 顺序预读**：新增 `/sys/fs/f2fs/<disk>/seq_file_ra_mul`，范围 2–256，
  默认 2。只影响之后的 `POSIX_FADV_SEQUENTIAL` 调用，不改变所有文件的预读。
  保留 VFS 的参数检查和 DAX/noop 行为，并对 32 位窗口溢出进行饱和处理。
- **uclamp**：dagu 启用任务与任务组接口，在 WALT 和 PELT 的 schedtune 利用率
  处理之后应用 rq clamp。保留现有 schedtune 接口。其他 governor boost 仍可提频，
  uclamp 不是严格的频率上限；未自行设定前台、后台或游戏的 clamp 数值。
- **BPF**：回移 nospec 栈槽索引修复，保留原作者。核对的 86 个实现、头文件、
  ARM64 JIT、BPF trace/XDP 相关文件与 LOS `lineage-24.0` 的固定提交
  `6b34d24765327d746074c8e53b5ad0d24a51acc7` 一致。这不代表将整个网络栈替换成 LOS。
- **图形 Binder**：保留已有 RT 图形/相机发送者匹配范围，改为完整任务名匹配，
  并保护发送线程退出时的引用。仅匹配同步事务；不修改进程默认优先级、不调整
  Binder 队列顺序。接收者必须有 `CAP_SYS_NICE` 或至少 1 的 `RLIMIT_RTPRIO`；
  不覆盖已观察到的更高/相等 RT 优先级。旧优先级由 Binder 核心保存和恢复。
  因此原先依赖越过接收者 RT 限制的调用不再被强制提升。

## 默认关闭的可选回收策略

两项选项均默认关闭，不会因为构建本分支就启用。先分开 A/B 验证，再考虑组合；
普通内核 ZIP 保留默认配置。CI 会在单独的输出目录中检查开启后的对象编译。

### CONFIG_KSHRINK_SLABD

后台 kswapd 在较轻压力下可以提交一项 slab 回收任务。请求锁保护参数，持有 memcg
在线引用，完成后释放。提交者的 `reclaim_state` 不被修改。直接回收、`drop_caches`、
回收优先级低于 `DEF_PRIORITY - 2`、工作线程繁忙或未初始化时保持同步。
当前只支持 ARM64、MEMCG、非 NUMA、无内存热插拔配置。请求槽在锁内更新，工作线程使用自己的回收状态。

编译启用后，可写 `/sys/module/kshrink_slabd/parameters/enabled` 来关闭/开启新请求。
关闭时已接收的一项工作可能继续完成，不取消正在回收的 shrinker。

### CONFIG_MI_RMAP_EFFICIENCY

按 Xiaomi `mi_rmap_efficiency` 的思路适配 4.19 页接口，范围收窄到普通文件页：
不保护匿名页、swap-backed 页或 compound 页。默认 mapcount 门槛是 **20**；
较轻优先级下降时允许较低映射数的 KEEP 决策。KEEP 和 ACTIVATE 都计入同一预算。

保护规则同时受以下条件限制：

- 只有全局、非强制、order-0 回收可用；memcg 限额回收和 DMA/DMA32 分配回收绕过。
- 每批处理开始时，`si_mem_available()` 必须大于 **512 MiB**；严重优先级时退出。
- 每个 `scan_control` 上限为 **16 MiB**；压力加重时另有限制扫描计数的比例预算。
- sysfs 初始化失败时策略保持关闭。

编译启用后，控制位于 `/sys/kernel/mi_rmap_efficiency/`：

- `enabled`：布尔开关。
- `mapcount_thres`：2 到 INT_MAX，默认 20。

这些是本次保守适配的边界，不声称复现闭源日志中的全部策略或参数。

## 构建与检查

现有 `build_kernel.sh dagu ksu miui droidspaces` 用于完整 MIUI / KernelSU / DroidSpaces
构建。KernelSU 固定到 `34210a4dec297cb48ce4bfa1c5ba50a2f7d22641`，这是
[2026-10-02 成功构建](https://github.com/Kaniruka/android_kernel_xiaomi_sm8250/actions/runs/37026908722)
记录的 ReSukiSU 版本。当前移动的 main 会要求不同的 SUSFS API 并主动拒绝构建。
需要更换时用完整 SHA 设置 `RESUKISU_REF`，并重新验证 SUSFS 兼容性；构建不会绕过检查。

在完整源码根目录运行用户态回归：

```sh
python3 tools/testing/selftests/zram/test_miui_tracking_parser.py
python3 tools/testing/selftests/f2fs/test_seq_file_ra.py
python3 tools/testing/selftests/vm/test_dagu_reclaim.py
python3 tools/testing/selftests/binder/test_graphics_priority.py
```

这些测试提取实际函数并提供用户态替身，覆盖解析、分配回滚、预读窗口、回收决策、
并发提交、图形发送者匹配和 RT 约束。CI 运行 ASan + UBSan；不具备 ASan 地址空间
支持的旧内核环境可以使用默认的 UBSan。它们不替代实机启动、RCU、freezer、memcg
销毁、Binder 嵌套事务/恢复、lockdep、OOM 压力和续航测试。

首次设备验证应保留可恢复的旧内核，比较同一系统、同一操作与接近的温度/内存状态。
先测默认配置，再分别启用两个可选 MM 策略；记录内存 PSI、OOM、回收耗时、
MiuiHome/SystemUI 帧时间和功耗，以这些指标评估实际收益。

## 尚未实现或无法对应的条目

- “玄戒动态预读”和 FEAS：缺少可验证策略及 SM8250 的帧事件接入方案。新增的
  F2FS 倍数接口与它们的功能范围不同。
- MiuiHome 优先内存分配：待设计具有资源上限和低内存回退的分配策略。
- iorap hot_collect：基线没有对应源码。
- Chromium / zygote：缺少本机复现及根因证据，未宣称修复。
- EROFS、LZ4、ZSTD、LZO-RLE：基线已支持；用户运行时的 ZSTD 设置保持不变。

参考来源：

- [F2FS 顺序预读上游提交](https://github.com/torvalds/linux/commit/0f6b56ec958d49e2b3dc955cdac6b62702c04b72)
- [zsmalloc 全局缓存上游方案](https://kernel.googlesource.com/pub/scm/linux/kernel/git/akpm/25-new/+/1ca21ca1041887aea8659da877c8c927b8af1ec1/patches/old/zsmalloc-make-common-caches-global.patch)
- [BPF nospec 回移提交](https://github.com/LineageOS/android_kernel_xiaomi_sm8250/commit/06585934ac809ab4d440e973d384507ad44010a9)
- [WALT uclamp 接入参考](https://github.com/IzumiYouka/android_kernel_xiaomi_sm8250/commit/9673922ac27412b24aeeb19e2d1150eed4e2772f)
- [4.19 厂商异步 slab 导入与问题修正参考](https://github.com/IzumiYouka/android_kernel_xiaomi_sm8250/commit/8a7d23eb82a9475ea8fed23d78428a0e83d5340d)
- [Xiaomi 映射保护策略参考](https://github.com/nullptr-t-oss/kernel_xiaomi_spring/blob/340446f5efe4bfafea50d4b82ba682db9811f19f/drivers/staging/mi_rmap_efficiency/mi_rmap_efficiency.c)
